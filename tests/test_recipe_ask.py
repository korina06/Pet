"""对话式检索 `/api/recipe/ask`：卡片里的菜必须回库取，模型只决定「选哪几道」。
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from agents.llm import LLMError
from api.recipe_routes import router
from core.recipe_library import create_recipe, ensure_schema
from core.recipes import RecipeSessionStore
from core.schemas import Message, ToolResult
from core.storage import Storage
from tests.fakes import FakeLLM, assistant_text, assistant_tool_call
from tools.base import BaseTool
from tools.registry import build_default_registry


def memory_db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="清蒸鲈鱼",
        ingredients=["鲈鱼 1 条（约 500g）", "姜 3 片"],
        steps=["鱼身划两刀抹盐腌 10 分钟", "水开后上锅蒸 8 分钟"],
        tags=["清淡", "蒸", "高蛋白"],
        time_minutes=20,
        source="seed",
    )
    create_recipe(
        db,
        name="番茄炒蛋",
        ingredients=["番茄 2 个", "鸡蛋 3 个"],
        steps=["番茄切块", "鸡蛋炒凝固后下番茄炒匀"],
        tags=["下饭", "炒"],
        time_minutes=10,
        source="seed",
    )
    # 第 55 步：强约束用例需要"辣的鸡肉"这类**能同时满足口味+主料**的菜
    create_recipe(
        db,
        name="香辣鸡丁",
        ingredients=["鸡胸肉 300g", "干辣椒 10g"],
        steps=["鸡丁腌 10 分钟", "爆香辣椒下鸡丁炒熟"],
        tags=["辣", "炒", "下饭"],
        time_minutes=15,
        source="seed",
    )
    # 第 56 步：忌口注入用例需要"含香菜"的菜；历史标签用例需要两道鱼
    create_recipe(
        db,
        name="香菜拌牛肉",
        ingredients=["卤牛肉 200g", "香菜 1 把"],
        steps=["牛肉切片", "加香菜和调料拌匀"],
        tags=["凉拌", "下饭"],
        time_minutes=10,
        source="seed",
    )
    create_recipe(
        db,
        name="水煮鱼",
        ingredients=["草鱼 1 条", "豆瓣酱 2 勺"],
        steps=["鱼片腌 10 分钟", "下锅煮熟浇热油"],
        tags=["川菜", "辣", "汤"],
        time_minutes=30,
        source="seed",
    )
    return db


def build_app(*, llm, db: Storage | None = None, with_deps: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.llm = llm
    app.state.recipe_store = RecipeSessionStore()
    if with_deps:
        storage = db if db is not None else memory_db()
        app.state.storage = storage
        app.state.registry = build_default_registry(storage=storage)
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def tool_call_payload(*, keyword: str = "鱼") -> Message:
    import json

    return assistant_tool_call(
        "call-1", "search_recipes", json.dumps({"keyword": keyword}, ensure_ascii=False)
    )


# ------------------------------------------------------------------ 基本链路


async def test_ask_missing_dependencies_is_a_readable_503():
    app = build_app(llm=FakeLLM([]), with_deps=False)
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )
    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]


async def test_ask_runs_the_search_tool_and_returns_real_cards():
    """核心用例：模型调工具 → 卡片来自库，不是模型的话。"""
    llm = FakeLLM(
        [
            tool_call_payload(keyword="鱼"),
            assistant_text("库里鱼的菜只有清蒸鲈鱼，20 分钟蒸出来挺清淡的。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭想吃鱼"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "库里鱼的菜只有清蒸鲈鱼，20 分钟蒸出来挺清淡的。"
    assert body["tool_calls"] == ["search_recipes"], "必须真的走了工具，而不是凭记忆"

    names = [recipe["name"] for recipe in body["recipes"]]
    assert "清蒸鲈鱼" in names, names
    # 卡片字段来自数据库（这些内容模型从没说过）
    card = body["recipes"][0]
    assert card["ingredients"] == ["鲈鱼 1 条（约 500g）", "姜 3 片"]
    assert card["steps"][1] == "水开后上锅蒸 8 分钟"
    assert card["time_minutes"] == 20
    assert card["source"] == "seed"


async def test_ask_without_tool_call_returns_no_cards():
    """模型没查库就直接答：不该凭空出现卡片。"""
    llm = FakeLLM([assistant_text("我不知道，你先告诉我你有什么食材吧。")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "随便聊聊"}
        )

    body = response.json()
    assert response.status_code == 200
    assert body["recipes"] == []
    assert body["tool_calls"] == []


async def test_ask_when_library_has_no_match_returns_no_invented_cards():
    """/ask 的 id 契约：工具回 0 条 → **一张卡片都不能有**。

    第 52 步把"瞎推 nearby"删掉了（用户实测：问"辣鸡肉"时塞了「台湾牛肉面 205 分钟」）。
    现在查不到就是 0 张卡，模型只能如实说没有——这就是我们想要的行为。
    """
    import json

    llm = FakeLLM(
        [
            assistant_tool_call("call-1", "search_recipes", json.dumps({"keyword": "松露"})),
            assistant_text("库里没有松露的菜。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "想吃松露"}
        )

    body = response.json()
    assert "没有松露" in body["reply"]
    assert body["tool_calls"] == ["search_recipes"]
    assert body["recipes"] == [], "库里没有的东西不能变成卡片"


async def test_ask_still_returns_cards_when_the_tool_relaxes_conditions():
    """放宽链的结果仍然是真的菜，所以照样出卡片（但要如实说放宽了什么）。"""
    import json

    llm = FakeLLM(
        [
            assistant_tool_call(
                "call-1",
                "search_recipes",
                json.dumps({"tags": ["清淡"], "ingredients": ["鱼"], "max_minutes": 1}),
            ),
            assistant_text("没有 1 分钟内能做好的鱼，我放宽了时间。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "1 分钟做个鱼"}
        )

    body = response.json()
    assert body["recipes"], "放宽后命中的菜应当出卡片"
    assert "放宽" in body["reply"]


async def test_ask_reports_model_failure_as_a_readable_reply():
    """模型挂了：Agent 会把失败转成一句人话（不抛栈、不给用户看空白气泡）。

    注意这里**不是 502**：Agent 内部把 LLMError 收敛成 `抱歉…` 的回复
    （`agents/agent.py` 的既有行为），对话里显示"抱歉，我这边出了点问题"
    比弹一个错误框更符合聊天场景。
    """

    class BoomLLM:
        async def complete(self, messages, tools=None):
            raise LLMError("DeepSeek 限流（HTTP 429）")

    app = build_app(llm=BoomLLM())
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )

    assert response.status_code == 200
    body = response.json()
    assert "抱歉" in body["reply"]
    assert body["recipes"] == []


async def test_ask_keeps_history_for_followups():
    """第二轮要把第一轮的话带上（"那还有别的吗"这种追问才接得住）。"""
    llm = FakeLLM(
        [
            tool_call_payload(keyword="鱼"),
            assistant_text("只有清蒸鲈鱼。"),
            assistant_text("那换成鸡蛋吧，番茄炒蛋怎么样。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        await client.post("/api/recipe/ask", json={"session_id": "s1", "message": "想吃鱼"})
        second = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "那还有别的吗"}
        )

    assert second.status_code == 200
    # 模型这一轮收到的历史里应当有第一轮的用户话与助手话
    history_text = " ".join(
        message.content for message in app.state.llm.calls[-1] if message.role != "system"
    )
    assert "想吃鱼" in history_text
    assert "只有清蒸鲈鱼" in history_text
    assert "那还有别的吗" in history_text


async def test_ask_rejects_empty_message():
    app = build_app(llm=FakeLLM([]))
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": ""}
        )
    assert response.status_code == 422


def _asked_text(messages) -> str:
    """这一轮发给模型的**全部文字**（系统提示 + 本轮条件 + 问题）。

    第 212 步把「本轮条件」从系统提示里拆成独立消息（为了前缀缓存），
    所以「模型看得到 X」的断言不能再只看 messages[0]。
    """
    return "\n".join(
        message.content for message in messages if isinstance(message.content, str)
    )


async def test_ask_uses_recipe_prompt_not_the_pet_persona():
    """食谱窗口用的是「菜谱助手」提示词：不该出现九九那套"主人/尾巴甩甩"。"""
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post("/api/recipe/ask", json={"session_id": "s1", "message": "吃什么"})

    messages = app.state.llm.calls[-1]
    system = messages[0]
    assert system.role == "system"
    assert "菜谱助手" in system.content
    assert "search_recipes" in system.content
    assert "主人" not in system.content, "这个窗口不该用桌宠人设"
    # 第一句才提醒可以勾标签（第 212 步起这句在本轮条件消息里）
    assert "勾标签" in _asked_text(messages)
    # 系统提示本身必须**逐字稳定**：它是可缓存的前缀，不能塞每轮会变的东西
    assert "勾标签" not in system.content, "每轮条件不许再拼回系统提示尾部"


# ------------------------------------------- 右栏「这次想要」接入（50e）


async def test_wanted_tags_are_translated_into_tool_instructions():
    """右栏勾的标签要变成**工具参数指令**，不能只是在回复里附和一句。

    这一步是把"点了没反应"补上的关键：`15 分钟内` → `max_minutes=15`
    的映射写在后端（确定的事不交给模型猜）。
    """
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={
                "session_id": "s1",
                "message": "晚饭吃什么",
                "wanted": ["清淡", "川菜", "30 分钟内"],
            },
        )

    text = _asked_text(app.state.llm.calls[-1])
    assert "对方这次在右栏勾了" in text
    assert "清淡" in text and "川菜" in text
    # 时间标签被翻译成明确参数，而不是原样丢给模型猜
    assert "max_minutes=30" in text
    # 必须强调"体现在工具参数里"，否则模型可能只在回复里提一句
    assert "工具参数" in text


async def test_slow_mode_maps_to_min_minutes():
    """「30 分钟以上」是**下限**，要翻成 min_minutes，不能翻成 max。"""
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "有时间慢慢做", "wanted": ["30 分钟以上"]},
        )

    text = _asked_text(app.state.llm.calls[-1])
    assert "min_minutes=30" in text
    assert "max_minutes=30" not in text, "别把下限翻成上限"


async def test_wanted_manner_preferences_are_explained():
    """「怎么吃」不是标签而是口径：要告诉模型外卖/自己做分别意味着什么。"""
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "吃什么", "wanted": ["外卖"]},
        )

    text = _asked_text(app.state.llm.calls[-1])
    assert "外带" in text or "外卖" in text


async def test_no_wanted_means_no_extra_block():
    """没勾任何标签时不该出现那一整块，免得白占上下文。"""
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post("/api/recipe/ask", json={"session_id": "s1", "message": "吃什么"})

    text = _asked_text(app.state.llm.calls[-1])
    assert "对方这次在右栏勾了" not in text
    # 但"第一句可以勾标签"的提醒还在（那是 first_turn 的职责，与 wanted 无关）
    assert "勾标签" in text


async def test_no_tag_hint_when_the_user_already_ticked_something():
    """用户已经勾了标签，就**别再劝他"去右边勾标签"**。

    实测出现过的尴尬：勾了"清淡 + 30 分钟内"，回复末尾还是那句
    "想要更合口味的话，可以在右边勾标签"——等于让人再做一遍刚做过的事。
    """
    llm = FakeLLM([assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "晚饭吃什么", "wanted": ["清淡"]},
        )

    text = _asked_text(app.state.llm.calls[-1])
    assert "别在回复里再劝他去勾标签" in text or "绝对不要再说" in text


# ------------------------------------- 勾选是强约束，不靠模型转述（55 P0-2）


async def test_ticked_tags_are_enforced_even_when_the_model_forgets_them():
    """**核心回归**：模型漏传参数时，勾选仍然生效。

    第 50e 那版把勾选写进提示词，模型忘了传就静默失效——而且测试只能断言
    "提示词里有这句话"。现在约束在工具边界注入，这里用一个**故意只传空参数**
    的假模型，断言结果依然按勾选过滤。
    """
    import json

    llm = FakeLLM(
        [
            # 模型"忘了"把辣传进去，只传了个空对象
            assistant_tool_call("call-1", "search_recipes", json.dumps({})),
            assistant_text("给你找了几道。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "随便来点", "wanted": ["辣"]},
        )

    body = response.json()
    assert body["recipes"], "应当有结果"
    for recipe in body["recipes"]:
        assert "辣" in recipe["tags"] or "微辣" in recipe["tags"], (
            f"{recipe['name']} 不辣，但用户勾了「辣」——约束没生效"
        )


async def test_time_tick_overrides_whatever_the_model_passed():
    """勾了「15 分钟内」，模型却传 max_minutes=300：以勾选为准。"""
    import json

    llm = FakeLLM(
        [
            assistant_tool_call(
                "call-1", "search_recipes", json.dumps({"max_minutes": 300})
            ),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "随便", "wanted": ["15 分钟内"]},
        )

    body = response.json()
    assert body["recipes"]
    for recipe in body["recipes"]:
        if recipe["time_minutes"] is not None:
            assert recipe["time_minutes"] <= 15, (
                f"{recipe['name']} 用了 {recipe['time_minutes']} 分钟，超过勾选的 15 分钟"
            )


async def test_model_can_still_add_conditions_on_top():
    """强约束不能把模型的手脚捆死：它仍然可以追加主料。"""
    import json

    llm = FakeLLM(
        [
            assistant_tool_call(
                "call-1",
                "search_recipes",
                json.dumps({"ingredients": ["鸡肉"]}),
            ),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "想吃鸡", "wanted": ["辣"]},
        )

    body = response.json()
    assert body["recipes"]
    for recipe in body["recipes"]:
        assert "辣" in recipe["tags"] or "微辣" in recipe["tags"]
        text = recipe["name"] + " ".join(recipe["ingredients"])
        assert "鸡" in text


async def test_user_tick_wins_over_the_model_on_the_same_dimension():
    """同一维度冲突时**以用户勾的为准**，不能并集。

    实测出现过：用户勾「辣」，模型从"来个清淡的"里推出 tags=['清淡']，
    并集之后同时返回清淡鱼和辣鱼，卡片自相矛盾。
    """
    import json

    llm = FakeLLM(
        [
            assistant_tool_call(
                "call-1", "search_recipes", json.dumps({"tags": ["清淡"]})
            ),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "来个清淡的", "wanted": ["辣"]},
        )

    body = response.json()
    assert body["recipes"], "库里辣菜应当能查到"
    for recipe in body["recipes"]:
        assert "辣" in recipe["tags"] or "微辣" in recipe["tags"], (
            f"{recipe['name']} 不辣：用户勾的辣被模型传的清淡盖掉了"
        )


async def test_remembered_aversions_actually_filter_the_search():
    """**长期忌口必须真的进检索**——这是第 56 步修的安全缺陷。

    修之前：忌口存进库、按钮一直亮着，但 `/ask` 根本没把它放进 `exclude`，
    含香菜的菜照常返回（实测：一道含香菜的菜照样出现在结果里）。
    现在服务端每轮都注入，模型忘不忘都不影响。
    """
    from core.recipe_library import set_aversions

    import json

    llm = FakeLLM(
        [
            assistant_tool_call("call-1", "search_recipes", json.dumps({})),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)
    set_aversions(app.state.storage, ["香菜"])

    async with make_client(app) as client:
        body = (await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "随便来点"},
        )).json()

    names = [recipe["name"] for recipe in body["recipes"]]
    assert names, "应当有结果"
    assert "香菜拌牛肉" not in names, (
        f"忌口「香菜」没生效：{names}"
    )
    for recipe in body["recipes"]:
        assert not any("香菜" in item for item in recipe["ingredients"])


async def test_a_tag_from_the_previous_turn_is_dropped_when_no_longer_asked_for():
    """用户规则：第三轮取消勾选后，不该再参照前两轮的关键词。

    模拟：第一轮勾「川菜」问鱼 → 第二轮取消勾选，只问"鱼"，
    模型从对话历史里又补了「川菜」——服务端必须把它丢掉。
    """
    import json

    llm = FakeLLM(
        [
            # 第一轮：勾了川菜
            assistant_tool_call(
                "call-1", "search_recipes", json.dumps({"tags": ["川菜"]})
            ),
            assistant_text("给你川菜。"),
            # 第二轮：没勾任何标签，模型却"记得"上一轮的川菜
            assistant_tool_call(
                "call-2",
                "search_recipes",
                json.dumps({"tags": ["川菜"], "ingredients": ["鱼"]}),
            ),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "想吃鱼", "wanted": ["川菜"]},
        )
        second = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "鱼", "wanted": []},
        )

    names = [recipe["name"] for recipe in second.json()["recipes"]]
    # 第二轮只该按「鱼」查：粤菜的清蒸鲈鱼和川菜的水煮鱼都是鱼，**两道都在**
    # 才说明「川菜」这个标签真的被丢掉了（只剩川菜的话就只能看到水煮鱼）。
    assert "清蒸鲈鱼" in names, (
        f"上一轮的「川菜」被带进了这一轮：{names}"
    )
    assert "水煮鱼" in names, names


async def test_a_mentioned_tag_in_this_turn_is_kept():
    """反向：这一句里**明确说了**川菜，就该保留（不是历史泄漏）。"""
    import json

    llm = FakeLLM(
        [
            assistant_tool_call(
                "call-1",
                "search_recipes",
                json.dumps({"tags": ["川菜"], "ingredients": ["鱼"]}),
            ),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)

    async with make_client(app) as client:
        body = (await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "想吃川菜的鱼", "wanted": []},
        )).json()

    names = [recipe["name"] for recipe in body["recipes"]]
    assert "水煮鱼" in names, f"这一句明确说了川菜，应当保留：{names}"


async def test_clearing_an_aversion_tells_the_model_the_old_policy_is_void():
    """**用户实测的 bug**：取消「不吃奶」后，模型仍然说"奶是你记着的忌口"。

    服务端数据是对的（库里已清空），但模型拿自己上一轮说过的话当事实。
    只改系统提示不够——必须在对话里插一条**比旧话更新**的说明。
    """
    import json

    from core.recipe_library import set_aversions

    llm = FakeLLM(
        [
            # 第一轮：有忌口
            assistant_tool_call("call-1", "search_recipes", json.dumps({})),
            assistant_text("你对奶忌口，我不给你找奶。"),
            # 第二轮（用户取消后）：模型应当被那条系统更新纠正
            assistant_tool_call("call-2", "search_recipes", json.dumps({})),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)
    set_aversions(app.state.storage, ["奶"])

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "我想喝奶"}
        )
        # 用户取消了勾选
        set_aversions(app.state.storage, [])
        await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "奶还是忌口吗"}
        )

    messages = app.state.llm.calls[-1]
    notes = [
        message.content for message in messages
        if isinstance(message.content, str) and "系统更新" in message.content
    ]
    assert notes, "忌口变了却没告诉模型，它会继续按旧话说"
    assert "没有" in notes[-1], notes[-1]
    assert "作废" in notes[-1], "要明确说旧说法作废，否则模型会两边都想遵守"

    # 系统提示里也要明说"没有忌口"，并禁止从历史推断
    text = _asked_text(messages)
    assert "当前忌口：没有" in text
    assert "不要根据对话历史推断忌口" in text


async def test_unchanged_aversions_do_not_spam_the_conversation():
    """忌口没变就不插说明，免得每轮都多一条噪音。"""
    import json

    from core.recipe_library import set_aversions

    llm = FakeLLM(
        [
            assistant_tool_call("call-1", "search_recipes", json.dumps({})),
            assistant_text("好。"),
            assistant_tool_call("call-2", "search_recipes", json.dumps({})),
            assistant_text("好。"),
        ]
    )
    app = build_app(llm=llm)
    set_aversions(app.state.storage, ["奶"])

    async with make_client(app) as client:
        await client.post("/api/recipe/ask", json={"session_id": "s1", "message": "想吃点东西"})
        await client.post("/api/recipe/ask", json={"session_id": "s1", "message": "再来点"})

    messages = app.state.llm.calls[-1]
    notes = [
        message.content for message in messages
        if isinstance(message.content, str) and "系统更新" in message.content
    ]
    # 第一轮是开场（对话里还没有旧说法），不需要纠正；第二轮忌口没变，也不该插。
    # 所以整个过程一条"系统更新"都不该有——这正是"别制造噪音"的意思。
    assert notes == [], notes


async def test_the_global_registry_is_not_mutated_by_a_request():
    """约束是**本次请求**的：不能污染应用级的注册表。"""
    import json

    app = build_app(
        llm=FakeLLM(
            [
                assistant_tool_call("call-1", "search_recipes", json.dumps({})),
                assistant_text("好。"),
            ]
        )
    )
    original = app.state.registry.get("search_recipes")

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "随便", "wanted": ["辣"]},
        )

    assert app.state.registry.get("search_recipes") is original, (
        "单次请求的约束不该替换掉全局工具实例"
    )


# ---------------------------------------- 口语里的过敏原落库（第 63 步）


async def test_a_stated_allergen_is_persisted_from_the_conversation():
    """用户在对话里说出的过敏原要**真的落库**，不能只靠模型自觉。

    这是第 63 步要的那条不变量：说出口 → 进 `recipe_aversions` → 之后每轮都由
    服务端注入工具层。删除仍然只走右栏 PUT 的整份替换，所以右栏取消勾选就能删掉。
    """
    from core.recipe_library import list_aversions

    llm = FakeLLM([assistant_text("记下了：花生。")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "我对花生过敏"},
        )

    assert response.status_code == 200
    assert "花生" in list_aversions(app.state.storage), "说出来的过敏原必须落库"
    # 「库里的忌口真的会过滤搜索」那半边由 test_remembered_aversions_actually_filter_the_search
    # 覆盖。这里**不**断言工具返回里出现忌口词——工具返回的是过滤后的结果，本来就不回显它
    # （我第一版就是在这里写错的，留个记号）。


async def test_a_relaxed_chat_does_not_pollute_the_aversion_library():
    """普通聊天**不许**往忌口库里塞东西——否则几轮之后菜全被过滤没了。"""
    from core.recipe_library import list_aversions

    llm = FakeLLM([assistant_text("想吃点什么？")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "今晚想吃点清淡的菜，有没有推荐"},
        )

    assert list_aversions(app.state.storage) == [], "没有忌口说法就不该记任何词"


async def test_recipe_agent_takes_max_steps_from_settings(monkeypatch):
    """食谱那条路的 Agent 必须**真的**从配置取 max_steps。

    这条测试是拿真事立的（第 73 步补）：第 69 步我"改"了这行，但 PowerShell 替换
    把注释和 `max_steps=` 挤进了同一行，整行变成注释 —— Agent 一直用默认值 4，
    而**没有任何测试发现**：默认值恰好也是 4，行为看起来完全没变。
    教训：只改一行配置接线时，"行为不变"恰恰是最坏的验证信号。
    """
    from core.config import Settings
    from core.schemas import AgentReply

    seen: dict = {}

    class SpyAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, messages):
            return AgentReply(content="查到了", tool_invocations=[], steps=1)

    monkeypatch.setattr("api.recipe_routes.Agent", SpyAgent)
    monkeypatch.setattr(
        "api.recipe_routes.get_settings",
        lambda: Settings(_env_file=None, agent_max_steps=7),
    )

    app = build_app(llm=FakeLLM([assistant_text("查到了")]))
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )

    assert response.status_code == 200
    assert "max_steps" in seen, "Agent 必须收到 max_steps 参数（不是靠默认值）"
    assert seen["max_steps"] == 7, (
        f"要取配置里的值 7，实际 {seen.get('max_steps')!r} —— 说明它被写死或没传进去"
    )


# ------------------------------------------------ 真流式（第 73 步）


def _sse_events(text: str) -> list[dict]:
    """把 SSE 响应体拆回一个个 JSON 事件。"""
    import json as _json

    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(_json.loads(line[6:]))
    return out


class ScriptedStreamLLM:
    """按脚本流式作答的假 LLM：每一步 = (要吐的若干段, 最终消息)。"""

    def __init__(self, script):
        from agents.llm import StreamEvent  # noqa: F401  确认依赖存在

        self.script = list(script)
        self.calls: list[list] = []

    async def complete_stream(self, messages, tools=None):
        from agents.llm import StreamEvent

        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("脚本用尽了，模型却又被调了一次")
        chunks, final = self.script.pop(0)
        for chunk in chunks:
            yield StreamEvent(kind="delta", text=chunk)
        yield StreamEvent(kind="done", message=final)


async def test_ask_stream_sends_deltas_then_a_meta_with_the_same_contract():
    """流式：先若干 delta，最后一条 meta 与 /ask 的响应体**同构**。"""
    llm = ScriptedStreamLLM([
        (["清蒸", "鲈鱼", "挺清淡的"], assistant_text("清蒸鲈鱼挺清淡的")),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask/stream",
            json={"session_id": "s1", "message": "晚饭想吃鱼"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    kinds = [event["type"] for event in events]
    assert kinds == ["delta", "delta", "delta", "meta"], kinds
    assert "".join(
        event["text"] for event in events if event["type"] == "delta"
    ) == "清蒸鲈鱼挺清淡的"

    payload = events[-1]["payload"]
    assert payload["reply"] == "清蒸鲈鱼挺清淡的"
    assert payload["session_id"] == "s1"
    # 契约与 /ask 一致：同样的字段，前端两套代码可以共用渲染
    assert set(payload) == {"session_id", "reply", "recipes", "tool_calls"}


async def test_ask_stream_resets_partial_text_when_the_step_turns_out_to_need_a_tool():
    """这一轮其实要调工具 → 刚才流的字不是答案，必须发 reset。

    不 reset 的话，界面上会先冒一句「我查一下」然后被工具结果顶掉，
    或者更糟：那句废话被当成回答留在气泡里。
    """
    step_one = assistant_tool_call("c1", "search_recipes", '{"keyword": "鱼"}')
    step_one.content = "我查一下"
    llm = ScriptedStreamLLM([
        (["我查一下"], step_one),
        (["清蒸鲈鱼"], assistant_text("库里只有清蒸鲈鱼。")),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask/stream",
            json={"session_id": "s1", "message": "晚饭想吃鱼"},
        )

    events = _sse_events(response.text)
    kinds = [event["type"] for event in events]

    assert "reset" in kinds, f"工具步必须先 reset 掉废话，实际事件：{kinds}"
    # reset 出现在工具导致的"断开"处：第一段 delta 之后
    assert kinds.index("reset") > kinds.index("delta")
    assert kinds.index("reset") < kinds.index("meta")
    # 最终回答里**不能**带着那句废话
    assert events[-1]["payload"]["reply"] == "库里只有清蒸鲈鱼。"
    assert "我查一下" not in events[-1]["payload"]["reply"]


async def test_ask_stream_reports_model_failure_and_still_records_history():
    """流式下模型挂了：发一条 error + 照样收尾（meta 里是人话），历史也要落。"""

    class FailingStreamLLM:
        async def complete_stream(self, messages, tools=None):
            from agents.llm import LLMError

            raise LLMError("DeepSeek 限流（HTTP 429）")
            yield  # pragma: no cover - 让它成为异步生成器

    app = build_app(llm=FailingStreamLLM())

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask/stream",
            json={"session_id": "s1", "message": "晚饭吃什么"},
        )
        follow = await client.get(
            "/api/recipe/session", params={"session_id": "s1"}
        )

    events = _sse_events(response.text)
    kinds = [event["type"] for event in events]
    assert "error" in kinds, kinds
    assert kinds[-1] == "meta", f"出错也要收尾，实际 {kinds}"

    payload = events[-1]["payload"]
    assert "抱歉" in payload["reply"], "对用户仍然只说人话"
    assert payload["recipes"] == []

    # 历史记上了，下一轮才有上下文
    assert follow.status_code == 200
    messages = follow.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


async def test_the_turn_negative_taste_is_injected_at_the_tool_boundary(monkeypatch):
    """路由层：从这句话里抽出的否定口味要真的送进搜索工具（第 77 步）。

    这条钉的是**接线**（抽取 → with_enforced_constraints），
    不是模型听不听话 —— 实测模型不传这个参数。
    """
    import tools.recipe_search_tool as rst

    real = rst.with_enforced_constraints
    seen: dict = {}

    def spy(tool, wanted, **kwargs):
        seen.update(kwargs)
        return real(tool, wanted, **kwargs)

    monkeypatch.setattr("tools.recipe_search_tool.with_enforced_constraints", spy)

    llm = FakeLLM([
        assistant_tool_call("call-1", "search_recipes", '{"tags": ["川菜"]}'),
        assistant_text("川菜里不辣的有鱼香肉丝。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "川菜里不辣的有吗"},
        )

    assert response.status_code == 200
    assert seen.get("exclude_tags") == ["辣"], (
        f"否定口味要交给工具边界，实际 {seen.get('exclude_tags')!r}"
    )


async def test_a_positive_spicy_request_does_not_get_excluded(monkeypatch):
    """反证：「想吃辣的」不能被当成否定条件，否则整个功能就是反的。"""
    import tools.recipe_search_tool as rst

    real = rst.with_enforced_constraints
    seen: dict = {}

    def spy(tool, wanted, **kwargs):
        seen.update(kwargs)
        return real(tool, wanted, **kwargs)

    monkeypatch.setattr("tools.recipe_search_tool.with_enforced_constraints", spy)

    llm = FakeLLM([
        assistant_tool_call("call-1", "search_recipes", '{"tags": ["辣"]}'),
        assistant_text("给你辣的。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "今天想吃辣的"}
        )

    assert seen.get("exclude_tags") == [], (
        f"正向诉求不该变成排除条件，实际 {seen.get('exclude_tags')!r}"
    )


async def test_asking_about_aversions_does_not_record_one():
    """问「我的忌口是什么」不该往长期忌口里写东西（第 78 步）。

    这条是端到端版本：用户看到的就是右栏里多出一个莫名其妙的词条。
    """
    from core.recipe_library import list_aversions

    llm = FakeLLM([assistant_text("你还没说过忌口。")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "我的忌口是什么"},
        )

    assert response.status_code == 200
    assert list_aversions(app.state.storage) == [], (
        f"问句不该落库，实际 {list_aversions(app.state.storage)}"
    )


async def test_partial_recognition_is_told_to_the_user_through_the_model():
    """只认出一部分时，要让模型请用户确认（第 79 步）。

    用户说「花生和巧克力蛋糕」——花生能记下，巧克力蛋糕记不下。
    这时如果一声不吭，用户会以为两样都生效了。
    """
    from core.recipe_library import list_aversions

    llm = FakeLLM([assistant_text("我记下了花生；巧克力蛋糕这个我没认出来，麻烦你确认。")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "我对花生和巧克力蛋糕过敏"},
        )

    assert response.status_code == 200
    stored = list_aversions(app.state.storage)
    assert "花生" in stored, f"认出来的那部分必须落库，实际 {stored}"
    assert "巧克力蛋糕" not in stored, "没认出来的不许瞎记"

    # 模型收到的那批消息里必须有一条提示它去请用户确认
    sent = " ".join(str(getattr(m, "content", "")) for m in llm.calls[-1])
    assert "巧克力蛋糕" in sent, "要把没识别出来的原话交给模型去回显"
    assert "右栏" in sent or "确认" in sent, "提示要给出下一步怎么做"


# ------------------------- 卡片只画"回答里点过名"的菜（第 82 步）


async def test_cards_only_include_the_dishes_the_answer_actually_names():
    """查了两次时，卡片不该把两次结果**全**画出来。

    模型有时先窄后宽（先查"鱼"、再查"番茄"），然后只在回答里推荐后者。
    以前卡片会把两次的结果都收进来，界面上的卡比回答里说的多。
    """
    llm = FakeLLM([
        assistant_tool_call("call-1", "search_recipes", '{"keyword": "鱼"}'),
        assistant_tool_call("call-2", "search_recipes", '{"keyword": "番茄"}'),
        assistant_text("番茄炒蛋更快，十分钟就能好。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "快的菜"}
        )

    names = [recipe["name"] for recipe in response.json()["recipes"]]
    assert names == ["番茄炒蛋"], f"只该画回答里点名的那道，实际 {names}"


async def test_cards_fall_back_to_everything_when_the_answer_names_nothing():
    """反证：模型只说"上面这几道"时，卡片**不能**凭空消失。

    过滤太紧会把正常回答的卡片全抹掉 —— 那比多一张卡糟得多。
    """
    llm = FakeLLM([
        assistant_tool_call("call-1", "search_recipes", '{"keyword": "鱼"}'),
        assistant_tool_call("call-2", "search_recipes", '{"keyword": "番茄"}'),
        assistant_text("就上面这几道，你挑一个吧。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "快的菜"}
        )

    names = [recipe["name"] for recipe in response.json()["recipes"]]
    assert "清蒸鲈鱼" in names and "番茄炒蛋" in names, (
        f"一个都没点名时应当退回全集，实际 {names}"
    )


async def test_the_recipe_agent_deliberately_keeps_the_weather_tool(monkeypatch):
    """食谱 Agent 拿的是**全量** registry（含天气）——这是用户拍板的决定。

    原话：「p3-1 允许食谱拿到天气查询工具，但不做任何表现，用户在问时能默默调用」。
    评价2 建议"食谱窗口最好只给 search_recipes"，但那会改掉一个**已经定过**的
    产品决定，而不是修 bug。

    这条测试的作用：将来有人想"收紧安全面"时，会先在这里看到为什么不能静默动它。
    真要改，是一起改决定 + 改这条测试，而不是悄悄删掉一个工具。
    """
    from core.schemas import AgentReply

    seen: dict = {}

    class SpyAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, messages):
            return AgentReply(content="好", tool_invocations=[], steps=1)

    monkeypatch.setattr("api.recipe_routes.Agent", SpyAgent)
    app = build_app(llm=FakeLLM([assistant_text("好")]))

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )

    assert response.status_code == 200
    registry = seen.get("registry")
    assert registry is not None, "Agent 必须拿到 registry"
    assert registry.get("get_weather") is not None, (
        "天气工具是**故意**保留给食谱的（用户决定），不要静默摘掉"
    )
    assert registry.get("search_recipes") is not None


# --------------------------- 食谱链路的总预算（第 83 步）


async def test_ask_times_out_with_a_readable_504(monkeypatch):
    """整条链路超时要给 504 + 人话，而不是让用户干等（第 83 步）。

    以前只有前端那道 45 秒 abort —— 那只是**客户端放弃**，服务端还在跑，
    用户也看不到任何可读的收尾。
    """
    import asyncio

    from core.config import Settings
    from core.schemas import AgentReply

    class SlowAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, messages):
            await asyncio.sleep(1.0)
            return AgentReply(content="不该走到这里", tool_invocations=[], steps=1)

    monkeypatch.setattr("api.recipe_routes.Agent", SlowAgent)
    monkeypatch.setattr(
        "api.recipe_routes.get_settings",
        lambda: Settings(_env_file=None, recipe_deadline_seconds=0.05),
    )

    app = build_app(llm=FakeLLM([assistant_text("x")]))
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )

    assert response.status_code == 504
    detail = response.json()["detail"]
    assert "秒" in detail and "再" in detail, detail


async def test_ask_stream_still_finishes_with_error_and_meta_on_timeout(monkeypatch):
    """流式超时也**必须收尾**：发 error + 终止的 meta，不能把连接吊着。

    这是流式特有的风险：非流式挂了还能返回 504，流式如果只是停止产出，
    前端会一直等（它的 45 秒 abort 是最后一道，但不该是第一道）。
    """
    import asyncio

    from core.config import Settings

    class SlowStreamAgent:
        def __init__(self, **kwargs):
            pass

        async def run_stream(self, messages):
            await asyncio.sleep(1.0)
            yield None          # 永远走不到

    monkeypatch.setattr("api.recipe_routes.Agent", SlowStreamAgent)
    monkeypatch.setattr(
        "api.recipe_routes.get_settings",
        lambda: Settings(_env_file=None, recipe_deadline_seconds=0.05),
    )

    app = build_app(llm=FakeLLM([assistant_text("x")]))
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask/stream",
            json={"session_id": "s1", "message": "晚饭吃什么"},
        )

    events = _sse_events(response.text)
    kinds = [event["type"] for event in events]

    assert "error" in kinds, f"超时要发 error，实际 {kinds}"
    assert kinds[-1] == "meta", f"最后一个事件必须是 meta（收尾），实际 {kinds}"
    assert "秒" in events[-1]["payload"]["reply"], events[-1]["payload"]["reply"]


# ------------------- 天气 + 食谱的组合链路（第 84 步 / 评价3 第 4 步）


class FakeWeatherTool(BaseTool):
    """可注入的天气工具：记录被调用时收到了什么参数。"""

    name = "get_weather"
    description = "假的实时天气，仅用于测试"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _soup_db() -> Storage:
    """带一道汤的库，用来验"冷 → 查天气 → 推荐汤"这条链。"""
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="番茄蛋汤",
        ingredients=["番茄 2 个", "鸡蛋 2 个"],
        steps=["番茄切块煮软", "打蛋花"],
        tags=["汤", "清淡"],
        time_minutes=15,
        source="seed",
    )
    return db


async def test_cold_weather_leads_to_weather_then_recipe():
    """「今天很冷，想喝汤」→ 先查天气、再查库，最后出真实卡片。

    这是**端到端**那条：以前天气、食谱各自有单测，但没有一条把它们串起来。
    """
    llm = FakeLLM([
        assistant_tool_call("c1", "get_weather", "{}"),
        assistant_tool_call("c2", "search_recipes", '{"keyword": "汤"}'),
        assistant_text("今天挺冷，来碗番茄蛋汤吧。"),
    ])
    weather = FakeWeatherTool(
        ToolResult(ok=True, content="北京 8°C 晴", tool_name="get_weather",
                   data={"temp": 8, "text": "晴"})
    )
    app = build_app(llm=llm, db=_soup_db())
    app.state.registry.replace(weather)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "今天很冷，想喝汤"},
        )

    body = response.json()
    assert weather.calls == [{}], f"天气工具该被调一次且不带坐标，实际 {weather.calls}"
    assert body["tool_calls"] == ["get_weather", "search_recipes"], body["tool_calls"]
    assert [recipe["name"] for recipe in body["recipes"]] == ["番茄蛋汤"]


async def test_weather_failure_does_not_block_the_recipe():
    """天气挂了也必须照常出菜：天气是**附加信息**，不是前置条件。

    这条是"降级"的核心：外部服务抖动时，本地菜谱库明明还能用。
    """
    llm = FakeLLM([
        assistant_tool_call("c1", "get_weather", "{}"),
        assistant_tool_call("c2", "search_recipes", '{"keyword": "汤"}'),
        assistant_text("这次没拿到天气，不过汤还是能给你安排。番茄蛋汤就行。"),
    ])
    weather = FakeWeatherTool(
        ToolResult(ok=False, error="天气服务暂时不可用", tool_name="get_weather")
    )
    app = build_app(llm=llm, db=_soup_db())
    app.state.registry.replace(weather)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "今天很冷，想喝汤"},
        )

    body = response.json()
    assert weather.calls, "天气仍然被尝试过"
    assert [recipe["name"] for recipe in body["recipes"]] == ["番茄蛋汤"], (
        "天气失败不该让卡片消失"
    )


async def test_a_plain_recipe_request_never_touches_the_weather_tool():
    """普通「想吃鱼」不查天气 —— 少一次外部调用、少一个失败面。"""
    llm = FakeLLM([
        assistant_tool_call("c1", "search_recipes", '{"keyword": "汤"}'),
        assistant_text("番茄蛋汤，十五分钟。"),
    ])
    weather = FakeWeatherTool(
        ToolResult(ok=True, content="不该被调用", tool_name="get_weather", data={})
    )
    app = build_app(llm=llm, db=_soup_db())
    app.state.registry.replace(weather)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭想吃鱼"}
        )

    assert response.status_code == 200
    assert weather.calls == [], "服务端不该自己悄悄查天气"


class FakeGeocodeTool(BaseTool):
    """可注入的地理编码：把城市换成坐标（固定值，不联网）。"""

    name = "geocode_city"
    description = "假的地理编码，仅用于测试"
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }

    def __init__(self, city: str, lat: float, lon: float) -> None:
        self.city = city
        self.lat = lat
        self.lon = lon
        self.calls: list[dict] = []

    async def run(self, city: str = "", **kwargs):
        self.calls.append({"city": city, **kwargs})
        return ToolResult(
            ok=True,
            content=f"{self.city} {self.lat},{self.lon}",
            tool_name=self.name,
            arguments={"city": city},
            data={"city": self.city, "lat": self.lat, "lon": self.lon},
        )


async def test_a_named_city_goes_through_geocode_before_weather():
    """用户说「杭州今天冷」：先 geocode 拿坐标，再用**查到的**坐标查天气。

    这条钉的是第 83 步那句提示词"坐标不要靠记忆写"——它得真的可执行：
    工具在、链路通、坐标流得下去。
    """
    llm = FakeLLM([
        assistant_tool_call("c1", "geocode_city", '{"city": "杭州"}'),
        assistant_tool_call("c2", "get_weather", '{"lat": 30.25, "lon": 120.15}'),
        assistant_tool_call("c3", "search_recipes", '{"keyword": "汤"}'),
        assistant_text("杭州今天冷，来碗番茄蛋汤。"),
    ])
    geocode = FakeGeocodeTool("杭州", 30.25, 120.15)
    weather = FakeWeatherTool(
        ToolResult(ok=True, content="杭州 9°C 阴", tool_name="get_weather",
                   data={"temp": 9, "text": "阴"})
    )
    app = build_app(llm=llm, db=_soup_db())
    app.state.registry.replace(geocode)
    app.state.registry.replace(weather)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "杭州今天冷，想喝汤"},
        )

    body = response.json()
    assert geocode.calls and geocode.calls[0]["city"] == "杭州"
    assert weather.calls == [{"lat": 30.25, "lon": 120.15}], (
        f"天气要用 geocode 查到的坐标，实际 {weather.calls}"
    )
    assert body["tool_calls"] == ["geocode_city", "get_weather", "search_recipes"]

async def test_recipe_uses_its_own_feature_registry_when_present(monkeypatch):
    """食谱优先用 `app.state.recipe_registry`（第 85 步）。

    生产里 `main.py` 挂的是**按功能装配**的那套（不含 find_nearby_places），
    `app.state.registry` 留给聊天。这里验证路由真的优先取前者 ——
    否则拆了 registry 也白拆。
    """
    from core.schemas import AgentReply
    from tools.registry import RECIPE_TOOLS, build_feature_registry

    seen: dict = {}

    class SpyAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def run(self, messages):
            return AgentReply(content="好", tool_invocations=[], steps=1)

    monkeypatch.setattr("api.recipe_routes.Agent", SpyAgent)

    app = build_app(llm=FakeLLM([assistant_text("好")]))
    app.state.recipe_registry = build_feature_registry(
        app.state.registry, RECIPE_TOOLS, feature="食谱"
    )

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "吃什么"}
        )

    assert response.status_code == 200
    registry = seen["registry"]
    assert registry.get("search_recipes") is not None
    assert registry.get("find_nearby_places") is None, "食谱不该拿到周边推荐"


# --------------------------- A1：模型挂了时的本地兜底（第 86 步）


class _BoomLLM:
    async def complete(self, messages, tools=None):
        from agents.llm import LLMError

        raise LLMError("DeepSeek 限流（HTTP 429）")


async def test_llm_failure_falls_back_to_local_search_when_conditions_exist():
    """模型挂了、但右栏勾了条件 → 用**已注入约束**的检索兜底出卡片。

    这是"本地库明明还能用"那条：模型不可用不该让整条链路白等。
    关键安全点：兜底走的是同一个已注入约束的工具，所以忌口照旧生效。
    """
    from core.recipe_library import list_aversions, set_aversions

    app = build_app(llm=_BoomLLM())
    # 设一条长期忌口，用来证明兜底**不会**绕过它
    set_aversions(app.state.storage, ["香菜"])

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "晚饭吃什么", "wanted": ["清淡"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["recipes"], "有可判定条件时应该兜底出卡片，而不是空手而归"
    assert "连不上" in body["reply"] or "按你勾" in body["reply"], body["reply"]
    assert body["tool_calls"] == ["search_recipes"], "兜底也要留下工具调用痕迹"
    assert "香菜" in list_aversions(app.state.storage), "忌口还在（没被兜底破坏）"


async def test_llm_failure_without_conditions_still_returns_no_cards():
    """没有任何可判定条件时**保持原样**：只道歉，不给"库里随便几道"。

    给随机几道菜比一句道歉更糟 —— 那和"我懂你想吃什么"很难区分。
    """
    app = build_app(llm=_BoomLLM())

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask", json={"session_id": "s1", "message": "晚饭吃什么"}
        )

    body = response.json()
    assert body["recipes"] == []
    assert "抱歉" in body["reply"]


async def test_streaming_llm_failure_also_falls_back_before_meta():
    """流式兜底：**在 meta 之前**替换内容，因为前端是拿 meta 覆盖气泡的。"""
    app = build_app(llm=_BoomLLM())

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask/stream",
            json={"session_id": "s1", "message": "晚饭吃什么", "wanted": ["清淡"]},
        )

    events = _sse_events(response.text)
    assert events[-1]["type"] == "meta"
    payload = events[-1]["payload"]
    assert payload["recipes"], "流式的兜底也要出卡片"
    assert "连不上" in payload["reply"] or "按你勾" in payload["reply"]


async def test_a_turn_level_exclusion_is_injected_but_not_persisted(monkeypatch):
    """「别放葱」：本次请求的 exclude 里要有葱，但**长期库里不能多一条**（第 90 步）。

    用户拍板的边界：这一轮的要求不该变成永久忌口 ——
    否则下次他说"想吃葱油拌面"会被无声拦住，右栏还多出一个没点过的词条。
    """
    import tools.recipe_search_tool as rst
    from core.recipe_library import list_aversions

    real = rst.with_enforced_constraints
    seen: dict = {}

    def spy(tool, wanted, **kwargs):
        seen.update(kwargs)
        return real(tool, wanted, **kwargs)

    monkeypatch.setattr("tools.recipe_search_tool.with_enforced_constraints", spy)

    llm = FakeLLM([
        assistant_tool_call("c1", "search_recipes", '{"keyword": "面"}'),
        assistant_text("好，不放葱。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "别放葱"},
        )

    assert response.status_code == 200
    assert seen.get("turn_exclude") == ["葱"], f"当轮排除要注入，实际 {seen.get('turn_exclude')!r}"
    assert list_aversions(app.state.storage) == [], "当轮要求**不许**落库"


async def test_a_long_term_aversion_is_still_persisted(monkeypatch):
    """反证：真正陈述式的「我对花生过敏」仍然要落库（别把 A2 做过头）。"""
    from core.recipe_library import list_aversions

    llm = FakeLLM([assistant_text("记下了。")])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "我对花生过敏"},
        )

    assert "花生" in list_aversions(app.state.storage)


# ------------------------------- 卡片不消失 / 能恢复（第 137 步，用户实测报的）


async def test_a_reply_that_names_a_dish_still_gets_cards_without_a_tool_call():
    """模型**不调工具**、凭上下文直接回答时，卡片也必须回来。

    用户实测：同一类问题问第二遍，模型会直接复述答案、不再查库，
    于是一张卡片都没有 —— 界面上只剩文字。修法是"回答里点了名、库里也有，
    就按名字补卡片"，事实仍然只来自数据库。
    """
    llm = FakeLLM([
        # 完全没有 tool_call，正文里点名了库里的菜
        assistant_text("给你清蒸鲈鱼，二十分钟就好。"),
    ])
    app = build_app(llm=llm)

    async with make_client(app) as client:
        body = (await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "来点清淡的"},
        )).json()

    names = [recipe["name"] for recipe in body["recipes"]]
    assert names, "回答里点名了库里的菜，卡片不该为空"
    assert "清蒸鲈鱼" in names, names


async def test_the_session_keeps_recipe_ids_so_cards_can_be_restored():
    """关窗重开后要能把卡片画回来：会话里的每条答复都带当时的菜 id。"""
    import json as _json

    llm = FakeLLM([
        assistant_tool_call(
            "call-1", "search_recipes", _json.dumps({"ingredients": ["鲈鱼"]})
        ),
        assistant_text("清蒸鲈鱼最省事。"),
    ])
    app = build_app(llm=llm)
    # 恢复卡片要用 library 的 by-ids 接口（第 137 步），这里把它一起挂上
    from api.recipe_library_routes import router as library_router

    app.include_router(library_router)

    async with make_client(app) as client:
        asked = (await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "想吃鲈鱼"},
        )).json()
        assert asked["recipes"], "先决条件：这一轮应当有卡片"

        session = (await client.get(
            "/api/recipe/session", params={"session_id": "s1"}
        )).json()

        ids = [recipe["id"] for recipe in asked["recipes"]]
        bot = [m for m in session["messages"] if m["role"] == "assistant"]
        assert bot and bot[-1]["recipes"] == ids, "答复里要存下这一轮的菜 id"

        restored = (await client.get(
            "/api/recipe/library/by-ids", params={"ids": ",".join(map(str, ids))}
        )).json()

    assert [row["id"] for row in restored] == ids, "按 id 要能原样取回（保序）"
    assert [row["name"] for row in restored] == [r["name"] for r in asked["recipes"]]


# ------------------------------- 前缀缓存：系统提示必须逐字不变（第 212 步）

async def test_system_prompt_is_byte_identical_when_the_ticked_tags_change():
    """**缓存回归**：勾选变了，系统提示一个字都不许变。

    DeepSeek 的提示缓存按前缀匹配。原来勾选/忌口拼在系统提示末尾，
    等于每轮都在前缀里插一个分歧点，后面的历史全部重算 ——
    7 天实测命中率只有 68.6%，漏掉的就是这部分。

    所以这条测试断言的是**字节相等**：只要有人以后又往系统提示里塞
    "这一轮的条件"，这里立刻会红。
    """
    llm = FakeLLM([assistant_text("好"), assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "吃饭", "wanted": ["清淡"]},
        )
        first = app.state.llm.calls[-1][0].content

        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "换一个", "wanted": ["辣", "30 分钟内"]},
        )
        second = app.state.llm.calls[-1][0].content

    assert first == second, "系统提示变了，前缀缓存就废了"

    # 而条件本身必须还在，并且是**并进这一轮的提问**（第 213 步）——
    # 单独一条消息时它不进历史，下一轮前缀立刻分歧，命中只剩系统提示。
    last = app.state.llm.calls[-1]
    assert "【本轮条件】" in last[-1].content, "条件要跟着这一轮的提问走"
    assert last[-1].role == "user"
    assert "换一个" in last[-1].content, "用户原话要还在最前面"
    assert "辣" in last[-1].content and "max_minutes=30" in last[-1].content


async def test_turn_conditions_are_not_persisted_into_the_session():
    """本轮条件**不进库**：下一轮再读历史时，不能看见上一轮的旧条件。

    存进去的后果很具体：用户在第二轮取消勾选，第三轮模型会从历史里
    读到"勾了清淡"，又开始按旧条件答（第 57 步踩过的同一个坑换个位置）。
    """
    llm = FakeLLM([assistant_text("好"), assistant_text("好")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post(
            "/api/recipe/ask",
            json={"session_id": "s1", "message": "吃饭", "wanted": ["清淡"]},
        )
        session = (await client.get(
            "/api/recipe/session", params={"session_id": "s1"}
        )).json()

    texts = [message["content"] for message in session["messages"]]
    assert not any("【本轮条件】" in text for text in texts), "条件消息被存进历史了"
    assert texts == ["吃饭", "好"], texts
