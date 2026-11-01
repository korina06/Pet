"""Agent 循环与工具抽象的测试。

全部使用 fake，不触网。覆盖的是这个项目最核心的几条链路：
模型要求调工具 → 工具执行 → 结果回灌 → 模型给出最终回答。
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import time

import pytest

from agents.agent import Agent, build_recipe_prompt, build_system_prompt
from core.schemas import Message, ToolCall, ToolResult
from tests.fakes import (
    BoomTool,
    EchoTool,
    FailingLLM,
    FakeLLM,
    assistant_text,
    assistant_tool_call,
)
from tools.base import BaseTool
from tools.registry import ToolRegistry, build_default_registry


def make_registry(*tools) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


# --------------------------------------------------------------------------
# 工具抽象
# --------------------------------------------------------------------------


def test_tool_schema_is_openai_compatible():
    tool = EchoTool()
    schema = tool.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"]["required"] == ["text"]


def test_registry_rejects_duplicate_names():
    registry = make_registry(EchoTool())
    with pytest.raises(ValueError, match="工具名重复"):
        registry.register(EchoTool())


def test_registry_rejects_tool_without_name():
    class Nameless(EchoTool):
        name = ""

    with pytest.raises(ValueError, match="未定义 name"):
        ToolRegistry().register(Nameless())


def test_tool_display_label_falls_back_to_name():
    """没写 label 的工具也要能在轮盘上显示，不能是空白按钮。"""
    assert EchoTool().display_label == "echo"


def test_tool_label_and_intent_are_used_when_provided():
    class Labelled(EchoTool):
        name = "labelled"
        label = "查天气"
        intent_template = "帮我用{label}看看。"

    tool = Labelled()
    assert tool.display_label == "查天气"
    assert tool.intent_text() == "帮我用查天气看看。"


def test_weather_tool_provides_menu_metadata():
    """天气工具必须提供轮盘要用的中文名和意图。"""
    from tools.weather_tool import WeatherTool

    tool = WeatherTool()
    assert tool.display_label == "查天气"
    assert "天气" in tool.intent_text()


async def test_safe_run_reports_missing_required_argument():
    """缺必填参数时应返回可读错误，而不是抛 KeyError。"""
    result = await EchoTool().safe_run({})
    assert result.ok is False
    assert "text" in (result.error or "")


async def test_safe_run_converts_unexpected_exception_to_result():
    """工具内部抛异常必须被兜底成 ToolResult，不能让异常冒到 Agent。"""

    class Exploding(EchoTool):
        name = "exploding"

        async def run(self, **kwargs):
            raise RuntimeError("内部炸了")

    result = await Exploding().safe_run({"text": "hi"})
    assert result.ok is False
    assert "内部炸了" in (result.error or "")


async def test_registry_execute_unknown_tool_is_graceful():
    """模型幻觉出不存在的工具名时，要返回可读错误而不是崩掉。"""
    registry = make_registry(EchoTool())
    result = await registry.execute("nonexistent_tool", {})
    assert result.ok is False
    assert "nonexistent_tool" in (result.error or "")
    assert "echo" in (result.error or "")


async def test_default_registry_contains_weather_tool():
    """默认注册表必须包含天气工具——这是当前的真实工具之一。"""
    registry = build_default_registry()
    assert "get_weather" in registry.names()
    schemas = registry.schemas()
    weather = next(s for s in schemas if s["function"]["name"] == "get_weather")
    # 第 83 步：坐标改成**可选**。以前 required 逼着模型给数字，而它不知道
    # 配置里的默认值 —— 只能凭记忆编一个。坐标是事实，不该由模型猜。
    required = weather["function"]["parameters"]["required"]
    assert required == [], f"lat/lon 不该再是必填，实际 {required}"
    description = weather["function"]["parameters"]["properties"]["lat"]["description"]
    assert "不要传" in description or "不要凭记忆" in description, (
        "描述里要明确告诉模型：不知道就别传，不要编"
    )


def test_default_registry_contains_poi_tool():
    """默认注册表也要包含周边推荐工具。"""
    registry = build_default_registry()
    assert "find_nearby_places" in registry.names()
    names = [s["function"]["name"] for s in registry.schemas()]
    assert "find_nearby_places" in names


def test_menu_tools_are_separate_from_registered_tools():
    """菜单项与已注册工具是两回事（第 15 步的核心）。

    天气与周边推荐：**注册给模型用，但不在菜单里露出手动入口**。
    若有人把 show_in_menu 改回 True，菜单会重新冒出两个按钮；
    若有人把工具从注册表删掉，输入"北京"就查不到天气了。
    这两种错误方向都要被这条断言挡住。
    """
    registry = build_default_registry()

    # 注册表里有全部工具（模型的能力）
    assert set(registry.names()) >= {"get_weather", "find_nearby_places"}

    # 但菜单里一个都不该出现
    assert registry.menu_names() == [], (
        f"菜单应只有前端内置的「对话」，实际有 {registry.menu_names()}"
    )


def test_tools_can_be_flagged_out_of_menu():
    """show_in_menu=False 的工具要能在注册表里保留、但不进菜单。"""

    class HiddenTool(EchoTool):
        name = "hidden"
        show_in_menu = False

    registry = make_registry(EchoTool(), HiddenTool())

    assert "hidden" in registry.names(), "隐藏的仅是菜单项，工具本身要保留"
    assert registry.menu_names() == ["echo"]


# --------------------------------------------------------------------------
# Agent 循环
# --------------------------------------------------------------------------


async def test_agent_returns_text_without_tool_call():
    llm = FakeLLM([assistant_text("你好呀，我是小咪")])
    registry = make_registry(EchoTool())
    agent = Agent(llm=llm, registry=registry)

    reply = await agent.run([Message(role="user", content="你好")])

    assert reply.content == "你好呀，我是小咪"
    assert reply.tool_invocations == []
    assert reply.steps == 0
    # 没有可用工具时不应把空 tools 列表传给模型
    assert llm.tools_seen[0] is not None  # echo 存在，所以应传 schema


async def test_agent_executes_tool_then_answers():
    """核心链路：模型要调工具 → 执行 → 结果回灌 → 模型给出最终回答。"""
    llm = FakeLLM(
        [
            assistant_tool_call("call_1", "echo", json.dumps({"text": "北京"})),
            assistant_text("北京我查到了！"),
        ]
    )
    echo = EchoTool()
    agent = Agent(llm=llm, registry=make_registry(echo))

    reply = await agent.run([Message(role="user", content="查北京")])

    assert reply.content == "北京我查到了！"
    assert reply.steps == 1
    assert len(reply.tool_invocations) == 1
    assert reply.tool_invocations[0].tool_name == "echo"

    # 工具真的被调用了，且参数正确解析
    assert echo.calls == [{"text": "北京"}]

    # 第二次调用模型时，历史里必须包含 tool 消息，且 tool_call_id 对应
    second_call = llm.calls[1]
    tool_messages = [m for m in second_call if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_1"
    assert "echo: 北京" in tool_messages[0].content


async def test_tool_failure_is_fed_back_to_model_not_raised():
    """工具失败要把原因回灌给模型，让模型自己组织语言，而不是抛给用户。"""
    llm = FakeLLM(
        [
            assistant_tool_call("call_1", "boom", json.dumps({"reason": "test"})),
            assistant_text("天气服务暂时连不上，稍后再试好吗？"),
        ]
    )
    boom = BoomTool()
    agent = Agent(llm=llm, registry=make_registry(boom))

    reply = await agent.run([Message(role="user", content="查天气")])

    assert reply.content == "天气服务暂时连不上，稍后再试好吗？"
    assert reply.tool_invocations[0].ok is False

    tool_messages = [m for m in llm.calls[1] if m.role == "tool"]
    assert "外部服务不可用" in tool_messages[0].content


async def test_agent_stops_at_max_steps_when_model_never_converges():
    """模型反复要求调工具时，必须在 max_steps 处停下，不能死循环。"""
    # 脚本给足 10 步，但 Agent 只允许 2 步
    llm = FakeLLM(
        [
            assistant_tool_call(f"call_{i}", "echo", json.dumps({"text": str(i)}))
            for i in range(10)
        ]
    )
    agent = Agent(llm=llm, registry=make_registry(EchoTool()), max_steps=2)

    reply = await agent.run([Message(role="user", content="无限循环测试")])

    assert reply.steps == 2
    assert len(llm.calls) == 2
    assert "2 步" in reply.content


async def test_agent_handles_multiple_tool_calls_in_one_turn():
    """模型一轮里要求调用多个工具时，每个都要执行并分别回灌。"""
    llm = FakeLLM(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    assistant_tool_call("c1", "echo", json.dumps({"text": "a"})).tool_calls[0],
                    assistant_tool_call("c2", "echo", json.dumps({"text": "b"})).tool_calls[0],
                ],
            ),
            assistant_text("两个都查好了"),
        ]
    )
    echo = EchoTool()
    agent = Agent(llm=llm, registry=make_registry(echo))

    reply = await agent.run([Message(role="user", content="都要查")])

    assert reply.steps == 2
    assert echo.calls == [{"text": "a"}, {"text": "b"}]
    tool_messages = [m for m in llm.calls[1] if m.role == "tool"]
    assert {m.tool_call_id for m in tool_messages} == {"c1", "c2"}


async def test_agent_surfaces_llm_error_as_friendly_message():
    agent = Agent(llm=FailingLLM("key 无效"), registry=make_registry(EchoTool()))
    reply = await agent.run([Message(role="user", content="你好")])

    assert "key 无效" in reply.content
    assert reply.tool_invocations == []


async def test_llm_failure_is_flagged_so_it_is_not_mistaken_for_a_normal_apology():
    """失败要能和"模型正常道歉"区分开（第 66 步）。

    改之前：模型挂了 → `content` 是「抱歉，我这边出了点问题…」，HTTP 200，
    和"模型自己写了一句道歉"**长得一模一样**，监控无法区分。
    现在 `reply.error` 非空就是挂了；正常回答时为 None。
    """
    failing = Agent(llm=FailingLLM("DeepSeek 限流（HTTP 429）"), registry=make_registry(EchoTool()))
    failed = await failing.run([Message(role="user", content="你好")])

    assert failed.error is not None, "失败必须留下可区分的标记"
    assert "429" in (failed.error or "")
    assert "抱歉" in failed.content, "对用户仍然只说人话"

    ok = Agent(llm=FakeLLM([assistant_text("好的主人")]), registry=make_registry(EchoTool()))
    normal = await ok.run([Message(role="user", content="你好")])

    assert normal.error is None, "正常回答不该被标成失败"


async def test_system_prompt_is_injected_once():
    llm = FakeLLM([assistant_text("喵")])
    agent = Agent(
        llm=llm,
        registry=make_registry(EchoTool()),
        system_prompt="你是小咪",
    )

    await agent.run([Message(role="user", content="你好")])

    first_call = llm.calls[0]
    assert first_call[0].role == "system"
    assert first_call[0].content == "你是小咪"
    assert sum(1 for m in first_call if m.role == "system") == 1


async def test_system_prompt_not_duplicated_if_already_present():
    llm = FakeLLM([assistant_text("喵")])
    agent = Agent(llm=llm, registry=make_registry(EchoTool()), system_prompt="你是小咪")

    await agent.run(
        [
            Message(role="system", content="已有的系统提示"),
            Message(role="user", content="你好"),
        ]
    )

    systems = [m for m in llm.calls[0] if m.role == "system"]
    assert len(systems) == 1
    assert systems[0].content == "已有的系统提示"


async def test_no_tools_passed_when_registry_is_empty():
    """注册表为空时不应传 tools 字段（传空列表有些模型会报错）。"""
    llm = FakeLLM([assistant_text("我没有工具可用")])
    agent = Agent(llm=llm, registry=ToolRegistry())

    await agent.run([Message(role="user", content="你好")])

    assert llm.tools_seen[0] is None


# --------------------------------------------------------------------------
# 消息序列化
# --------------------------------------------------------------------------


def test_message_to_api_includes_tool_calls_and_raw_arguments():
    message = assistant_tool_call("call_9", "echo", '{"text": "原样"}')
    payload = message.to_api()

    assert payload["role"] == "assistant"
    assert payload["tool_calls"][0]["id"] == "call_9"
    # 回灌时必须原样传回原始 JSON 字符串
    assert payload["tool_calls"][0]["function"]["arguments"] == '{"text": "原样"}'


def test_tool_message_to_api_has_tool_call_id():
    payload = Message(role="tool", content="结果", tool_call_id="c1", name="echo").to_api()
    assert payload["tool_call_id"] == "c1"
    assert payload["content"] == "结果"


def test_tool_result_as_tool_content_marks_failure():
    ok = ToolResult(ok=True, content="晴")
    bad = ToolResult(ok=False, error="超时")
    assert ok.as_tool_content() == "晴"
    assert "超时" in bad.as_tool_content()


def test_build_system_prompt_lists_tools():
    prompt = build_system_prompt(["get_weather", "search_news"])
    assert "get_weather" in prompt
    assert "search_news" in prompt
    assert build_system_prompt([]) != ""


def test_recipe_prompt_does_not_deny_forecasts():
    """第 237 步：get_weather 已支持逐日预报，食谱提示词不能还教它说「只有实况」。"""
    prompt = build_recipe_prompt()

    assert "只有实况" not in prompt
    assert "逐日预报" in prompt
    assert "end_date" in prompt or "days" in prompt, "要告诉模型怎么要预报"


def test_system_prompt_contains_the_new_persona():
    """人设换成 karo 之后，关键细节必须都在（第 143 步 / 人设.md）。

    这些字段一旦漏掉，模型就会退回"通用助手"的口吻 —— 妹妹感全靠它们撑着。
    """
    prompt = build_system_prompt([])

    # 名字 / 外观
    assert "karo" in prompt
    assert "妹妹" in prompt
    assert "浅奶茶" in prompt and "灰蓝" in prompt
    assert "围裙" in prompt
    # 耳朵与尾巴保留，但明确写成"小特征"而不是角色核心
    assert "小特征" in prompt
    assert "软耳朵" in prompt and "尾巴" in prompt
    assert "不是角色核心" in prompt

    # 性格关键词
    for trait in ("元气", "黏人", "害羞", "小倔"):
        assert trait in prompt, f"人设缺少性格关键词：{trait}"


def test_system_prompt_contains_the_little_habits():
    """小习惯要写进去（紧张/开心/困了各一个）。"""
    prompt = build_system_prompt([])

    assert "揪裙角" in prompt
    assert "脚尖" in prompt
    assert "抱着膝盖" in prompt


def test_system_prompt_bans_animal_onomatopoeia():
    """人设里写明：耳朵尾巴只是小特征，不用「喵」「嗷呜」这类拟声词。"""
    prompt = build_system_prompt([])

    assert "喵" in prompt, "应当明确点名禁用的拟声词（否则模型会自己加）"
    assert "拟声词" in prompt


def test_system_prompt_takes_the_address_from_its_argument():
    """称呼来自参数（`PET_ADDRESS`），默认「哥哥」，传进来就换。"""
    default = build_system_prompt([])
    sister = build_system_prompt([], address="姐姐")
    nickname = build_system_prompt([], address="小星")

    assert "「哥哥」" in default
    assert "「姐姐」" in sister and "「哥哥」" not in sister
    assert "「小星」" in nickname
    # 禁止的称呼要始终在
    for prompt in (default, sister, nickname):
        assert "不要用「您」" in prompt
        assert "「用户」" in prompt


def test_system_prompt_keeps_the_safety_and_honesty_rules():
    """安全与诚实条款不能少（第 143 步新增，来自人设.md 第七节）。"""
    prompt = build_system_prompt([])

    # 危机信号：要认真对待并鼓励求助现实资源
    assert "自伤" in prompt and "热线" in prompt
    # 被认真问身份时不撒谎
    assert "不撒谎" in prompt and "AI" in prompt
    # 不编造共同回忆
    assert "编造共同回忆" in prompt
    # 不做情感绑架 / 不替代现实社交
    assert "你不理我我就难过" in prompt
    assert "现实" in prompt


def test_system_prompt_keeps_the_honesty_and_brevity_rules():
    """新提示词里，旧版那几条底线必须还在。"""
    prompt = build_system_prompt([])

    assert "不要凭记忆编造事实" in prompt, "应保留'不许编造事实'约束"
    assert "简短" in prompt, "应保留'回答简短'约束"
    assert "不知道" in prompt, "不知道就要说不知道"
    assert "动作描写" in prompt, "动作描写要允许但要限制"
    assert "markdown" in prompt, "对话框是纯文本，不能渲染 markdown"


def test_system_prompt_limits_action_descriptions():
    """允许动作描写，但必须限制住，避免整条回复只有括号动作没有内容。"""
    prompt = build_system_prompt([])
    assert "动作描写" in prompt
    assert "不能" in prompt or "不要" in prompt


def test_system_prompt_does_not_leak_previous_personas():
    """旧人设（小咪 / 九九 / 小狐狸）不应残留，避免模型混淆身份。"""
    prompt = build_system_prompt([])
    for name in ("小咪", "小猫", "九九", "小狐狸", "幼狐", "主人"):
        assert name not in prompt, f"旧人设残留：{name}"


def test_system_prompt_sends_travel_and_recipe_questions_to_their_windows():
    """聊天不答出行/菜谱：遇到这两类就请主人去对应窗口（第 85 步）。

    用户第 85 步的决定：「聊天回答一些别的问题，出行/食谱固定在那两个小功能里」。
    所以这段提示词的作用是**划边界**，而不是教它怎么查天气、怎么报菜名。

    为什么这条必须和 registry 的允许列表一起改：提示词若还写着"必须调用
    search_recipes"，而聊天手里已经没有这个工具了，模型就会对着不存在的工具下指令。
    """
    prompt = build_system_prompt([])

    # 要明确指出两个窗口
    assert "出行" in prompt and "食谱" in prompt
    assert "窗口" in prompt
    # 要明确禁止自己去查/编
    assert "不要自己查天气" in prompt
    assert "不要凭记忆编" in prompt or "不要凭记忆编造" in prompt
    # 旧的"查天气/查菜谱"教程必须整段消失
    for gone in ("什么时候该查天气", "什么时候该查菜谱", "回答天气问题"):
        assert gone not in prompt, f"旧的『{gone}』段落应已删除"
def test_system_prompt_no_longer_coaches_nearby_recommendations():
    """聊天不再教模型"推荐附近好玩的地方"（第 85 步）。

    那条能力属于出行窗口。留在聊天提示词里会引导模型编店名/评分 ——
    这正是边界改掉的原因。
    """
    prompt = build_system_prompt([])

    assert "find_nearby_places" not in prompt, "不该再提这个工具"
    assert "附近好玩" not in prompt
    # 但"不许编具体信息"这条底线要留着
    assert "编" in prompt
def test_weather_tool_description_mentions_place_names():
    """工具描述也要说明"只给地名"算查天气，否则模型可能不选它。"""
    from tools.weather_tool import WeatherTool

    description = WeatherTool.description
    assert "地名" in description
    assert "上海" in description, "应给出换算经纬度的例子"
    # 提示词与工具描述都要把默认值/换算规则说清
    assert "默认" in description



# ------------------------------------ 同一轮的多个工具调用并发执行（第 72 步）


class SlowTool(BaseTool):
    """可以设定延迟的假工具，用来验证"并发 vs 串行"。"""

    description = "慢工具，仅用于并发测试"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self, name: str, delay: float = 0.12) -> None:
        self.name = name
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(self.delay)
        self.calls.append(kwargs)
        return ToolResult(
            ok=True,
            content=f"{self.name}: {kwargs.get('text')}",
            tool_name=self.name,
            arguments=kwargs,
        )


def _two_call_reply(delays: tuple[str, str] = ("c1", "c2")) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(id=delays[0], name="slow_a",
                     arguments={"text": "A"}, raw_arguments='{"text":"A"}'),
            ToolCall(id=delays[1], name="slow_b",
                     arguments={"text": "B"}, raw_arguments='{"text":"B"}'),
        ],
    )


async def test_multiple_tool_calls_in_one_turn_run_concurrently():
    """同一轮的多个工具调用要并发，不是排队。

    改之前是 `for call in reply.tool_calls: await ...` ——
    「查天气 + 查附近地点」这种组合，用户要等两个往返**相加**。
    """
    slow_a = SlowTool("slow_a", delay=0.12)
    slow_b = SlowTool("slow_b", delay=0.12)
    llm = FakeLLM([_two_call_reply(), assistant_text("都查完了")])
    agent = Agent(llm=llm, registry=make_registry(slow_a, slow_b))

    started = time.perf_counter()
    reply = await agent.run([Message(role="user", content="查两件事")])
    elapsed = time.perf_counter() - started

    assert len(reply.tool_invocations) == 2
    assert len(slow_a.calls) == 1 and len(slow_b.calls) == 1, "两个工具都要真的被调用"
    # 串行约 0.24s，并发约 0.12s；给足余量，只要明显小于相加就算并发
    assert elapsed < 0.22, (
        f"两个 0.12s 的工具应当并发执行（约 0.12s），实际 {elapsed:.3f}s"
    )


async def test_tool_results_are_fed_back_in_the_requested_order_even_when_slow():
    """并发执行，但**回灌顺序必须按模型给的 tool_calls 原序**。

    这里的 a 故意比 b 慢：如果实现是"谁先返回谁先回灌"，
    模型就会把 a 的结果当成 b 的答案 —— 而且是静默错，不会报错。
    """
    slow_a = SlowTool("slow_a", delay=0.15)   # 慢的排在前面
    slow_b = SlowTool("slow_b", delay=0.01)
    llm = FakeLLM([_two_call_reply(), assistant_text("好了")])
    agent = Agent(llm=llm, registry=make_registry(slow_a, slow_b))

    reply = await agent.run([Message(role="user", content="查两件事")])

    # 回灌给模型的 tool 消息：顺序与 tool_call_id 都要对上
    tool_messages = [m for m in llm.calls[1] if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"], (
        f"回灌顺序错了：{[m.tool_call_id for m in tool_messages]}"
    )
    assert tool_messages[0].content.startswith("slow_a:"), (
        f"第一个 tool 消息应是 slow_a 的结果，实际 {tool_messages[0].content!r}"
    )
    assert tool_messages[1].content.startswith("slow_b:")

    # 对外暴露的调用记录也按原序
    assert [inv.tool_name for inv in reply.tool_invocations] == ["slow_a", "slow_b"]


async def test_a_failing_tool_does_not_cancel_the_others():
    """一个工具失败（ok=False 或抛异常）不能把同轮其它工具的结果带走。"""
    slow_a = SlowTool("slow_a", delay=0.02)
    boom = BoomTool()
    llm = FakeLLM([
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c1", name="slow_a",
                         arguments={"text": "A"}, raw_arguments='{"text":"A"}'),
                ToolCall(id="c2", name="boom",
                         arguments={"reason": "x"}, raw_arguments='{"reason":"x"}'),
            ],
        ),
        assistant_text("一部分失败"),
    ])
    agent = Agent(llm=llm, registry=make_registry(slow_a, boom))

    reply = await agent.run([Message(role="user", content="查两件事")])

    assert [inv.ok for inv in reply.tool_invocations] == [True, False]
    assert slow_a.calls == [{"text": "A"}], "成功的那个照样执行完了"



def test_wanted_minute_tables_have_a_single_source():
    """时间映射与"怎么吃"口径只允许有**一份**（第 81 步）。

    以前 `agents/agent.py` 里有一份 `_WANTED_MAX_MINUTES`，`core/recipes.py` 里有
    一份内容相同、名字不同的 `WANTED_MAX_MINUTES`。改一个标签就要记得改两处，
    漏一处就是"提示词说 15 分钟、强制注入按 30 分钟"这种静默错位。

    用 `is` 而不是 `==`：要的是**同一个对象**（引用），不是"恰好内容一样"。
    """
    import agents.agent as agent_module
    import core.recipes as recipes_module

    assert agent_module._WANTED_MAX_MINUTES is recipes_module.WANTED_MAX_MINUTES
    assert agent_module._WANTED_MIN_MINUTES is recipes_module.WANTED_MIN_MINUTES
    assert agent_module._WANTED_MANNER is recipes_module.WANTED_MANNER


def test_system_prompt_tells_the_truth_about_remembering():
    """提示词要说清"同一段对话你是记得的"（第 110 步）。

    真事：端到端跑出来的回复里有「桌面这里没有能存东西的地方」——
    提示词里**根本没这句**，是模型自己编的自我描述。而实际历史会一起带上
    （最近 40 条，重启后也在），所以那句话是不准确的。
    """
    prompt = build_system_prompt([])

    assert "同一段对话里的事你是知道的" in prompt
    assert "不要说「我这里存不了东西」" in prompt
    assert "清空过的" in prompt, "也要说清什么情况下它确实不知道"


async def test_turn_token_budget_stops_before_the_next_call():
    """轮级软预算：超了就不再发起下一次模型调用（第 112 步 / 评价5 方案 C'）。

    这是"拦得住"的护栏 —— 判断发生在**调用之前**。与 `max_steps`（次数上限）、
    `max_output_tokens`（单次输出）、deadline（时间）是三件不同的事。
    """
    from core import usage

    class _SpendyLLM:
        """每步都要求调工具、且每步烧掉 15K token。"""

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            usage.add_turn_tokens(15000)
            return assistant_tool_call(f"c{self.calls}", "echo", '{"text": "x"}')

    usage.reset_turn_tokens()
    llm = _SpendyLLM()
    agent = Agent(
        llm=llm,
        registry=make_registry(EchoTool()),
        max_steps=6,
        # 每步烧 15K，预算 15K —— 第一步之后就该停下
        turn_token_budget=15000,
    )

    events = [event async for event in agent.run_stream([Message(role="user", content="hi")])]

    final = [event for event in events if event.type == "final"]
    assert final, "必须给出一个 final 事件，不能静默断掉"
    assert "token 预算" in str(final[-1].detail)
    assert "调大" in str(final[-1].detail), "要说清用户能做什么，而不是只说额度用完"
    assert llm.calls == 1, f"第二次调用就不该发生，实际调用了 {llm.calls} 次"


async def test_without_a_budget_the_loop_still_uses_max_steps():
    """不设预算（0）时行为与以前完全一致 —— 别让新护栏顺手改了老行为。"""
    usage_free = FakeLLM([assistant_tool_call("c1", "echo", '{"text": "x"}')] * 3)
    agent = Agent(llm=usage_free, registry=make_registry(EchoTool()), max_steps=2)

    events = [event async for event in agent.run_stream([Message(role="user", content="hi")])]

    assert any(event.type == "final" for event in events)
    assert len(usage_free.calls) == 2, "max_steps=2 就该只调用 2 次"
