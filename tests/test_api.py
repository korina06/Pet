"""HTTP 接口层的测试。

不启动真实服务器，用 httpx.ASGITransport 直接把请求打进 FastAPI 应用，
也不需要真实的 DeepSeek key：Agent 的 llm 换成 FakeLLM。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agents.agent import Agent
from api.routes import router
from core.schemas import AgentReply, Message, ToolResult
from core.session import SessionStore
from fastapi import FastAPI
from tests.fakes import EchoTool, FakeLLM, assistant_text, assistant_tool_call
from tools.registry import ToolRegistry


class ScriptedAgent:
    """可预测的假 Agent，用来隔离测试 HTTP 层的逻辑。"""

    def __init__(self, reply: AgentReply) -> None:
        self.reply = reply
        self.seen_messages: list[list[Message]] = []

    async def run(self, messages: list[Message]) -> AgentReply:
        self.seen_messages.append(list(messages))
        return self.reply


def build_test_app(agent: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    registry = ToolRegistry()
    registry.register(EchoTool())

    app.state.registry = registry
    app.state.session_store = SessionStore(max_messages=40)
    app.state.agent_factory = lambda: agent
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health():
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_tools_endpoint_lists_registered_tools():
    """轮盘菜单的数据源：每项必须带 label 与 intent，前端才不用硬编码文案。"""
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.get("/api/tools")

    assert response.status_code == 200
    tools = response.json()["tools"]
    assert [t["name"] for t in tools] == ["echo"]
    assert tools[0]["description"]
    # echo 没有自定义 label，应回退到工具名，不能是空字符串
    assert tools[0]["label"] == "echo"
    # intent 必须非空，否则点了菜单项会发不出消息
    assert tools[0]["intent"]


async def test_tools_endpoint_uses_custom_label_and_intent():
    """工具自定义了 label/intent 时必须原样透出。"""

    class LabelledTool(EchoTool):
        name = "labelled"
        label = "查天气"
        intent_template = "帮我查一下北京的天气。"

    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    # 换掉整个注册表，避免和 build_test_app 预置的 echo 混在一起（列表是排序的）
    registry = ToolRegistry()
    registry.register(LabelledTool())
    app.state.registry = registry

    async with make_client(app) as client:
        response = await client.get("/api/tools")

    tool = response.json()["tools"][0]
    assert tool["name"] == "labelled"
    assert tool["label"] == "查天气"
    assert "北京" in tool["intent"]


async def test_tools_endpoint_only_returns_menu_items():
    """/api/tools 是「轮盘菜单项」接口：show_in_menu=False 的工具不该出现。

    第 24 步起，聊天框的胶囊改成"打开一个功能"，不再由工具驱动，
    所以这个接口回到单一职责，也不再需要第 23 步临时加的 menu 字段。
    默认工具集（天气/周边）都是 show_in_menu=False，返回空列表；
    它们仍注册在注册表里，靠对话自动调用。
    """
    from tools.registry import build_default_registry

    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    app.state.registry = build_default_registry()

    async with make_client(app) as client:
        response = await client.get("/api/tools")

    assert response.status_code == 200
    assert response.json()["tools"] == [], (
        f"轮盘里不该有工具项，实际得到 {response.json()['tools']}"
    )

    # 工具本身仍在注册表里，模型照常可用
    assert set(app.state.registry.names()) >= {"get_weather", "find_nearby_places"}


async def test_chat_returns_content_and_tool_invocations():
    reply = AgentReply(
        content="北京今天晴",
        tool_invocations=[
            ToolResult(
                ok=True,
                content="晴",
                tool_name="get_weather",
                arguments={"lat": 39.92},
            )
        ],
        steps=1,
    )
    app = build_test_app(ScriptedAgent(reply))

    async with make_client(app) as client:
        response = await client.post(
            "/api/chat",
            json={"session_id": "s1", "message": "北京天气"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "北京今天晴"
    assert body["session_id"] == "s1"
    assert body["steps"] == 1
    assert body["tool_invocations"][0]["tool_name"] == "get_weather"
    assert body["tool_invocations"][0]["ok"] is True


async def test_chat_persists_only_safe_messages_across_turns():
    """跨轮次持久化必须丢掉 tool 消息。

    带 tool_call_id 的 tool 消息一旦在历史截断后变成"孤儿"，
    OpenAI 兼容接口会直接返回 400。这里断言第二轮送给模型的历史里
    没有残留的 tool 消息。
    """
    echo = EchoTool()
    llm = FakeLLM(
        [
            assistant_tool_call("call_1", "echo", json.dumps({"text": "hi"})),
            assistant_text("第一轮回答"),
            assistant_text("第二轮回答"),
        ]
    )
    agent = Agent(llm=llm, registry=ToolRegistry())
    agent.registry.register(echo)

    app = build_test_app(agent)
    async with make_client(app) as client:
        first = await client.post(
            "/api/chat", json={"session_id": "s1", "message": "第一问"}
        )
        assert first.status_code == 200
        second = await client.post(
            "/api/chat", json={"session_id": "s1", "message": "第二问"}
        )
        assert second.status_code == 200

    # 第二轮送给模型的历史：不应包含任何 tool 消息
    messages_second_turn = llm.calls[-1]
    assert [m for m in messages_second_turn if m.role == "tool"] == []

    # 但应当包含第一轮的用户提问与助手回答，说明记忆生效了
    roles = [m.role for m in messages_second_turn]
    contents = [m.content for m in messages_second_turn]
    assert "第一问" in contents
    assert "第一轮回答" in contents
    assert roles.count("user") == 2


async def test_chat_rejects_empty_message():
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.post(
            "/api/chat", json={"session_id": "s1", "message": ""}
        )
    assert response.status_code == 422


async def test_chat_rejects_missing_session_id():
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.post("/api/chat", json={"message": "你好"})
    assert response.status_code == 422


async def test_chat_returns_500_without_leaking_traceback():
    class Exploding:
        async def run(self, messages):
            raise RuntimeError("内部细节不该暴露")

    app = build_test_app(Exploding())
    async with make_client(app) as client:
        response = await client.post(
            "/api/chat", json={"session_id": "s1", "message": "你好"}
        )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "RuntimeError" in detail
    # 不应把完整异常消息当成响应暴露出去
    assert "内部细节不该暴露" not in detail


async def test_session_reset_endpoint():
    """重置接口把该会话的历史清空（直接查 store，不依赖已删除的计数接口）。"""
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        await client.post("/api/chat", json={"session_id": "s1", "message": "你好"})
        assert await app.state.session_store.get_history("s1"), "发过消息后应有历史"

        response = await client.delete("/api/sessions/s1")
        assert response.status_code == 200

        assert await app.state.session_store.get_history("s1") == [], "重置后历史应为空"


@pytest.mark.parametrize("session_id", ["a" * 128])
async def test_session_id_at_length_limit_is_accepted(session_id):
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.post(
            "/api/chat", json={"session_id": session_id, "message": "你好"}
        )
    assert response.status_code == 200


async def test_session_id_over_length_limit_is_rejected():
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.post(
            "/api/chat", json={"session_id": "a" * 129, "message": "你好"}
        )
    assert response.status_code == 422


# ------------------------------ 聊天历史接口（第 93 步 / S3）


async def test_chat_history_endpoint_restores_the_conversation():
    """打开窗口时用它把之前的对话画回来。

    约定：**空会话返回 `[]` 而不是 404** —— "还没聊过"是正常状态。
    """
    app = build_test_app(ScriptedAgent(AgentReply(content="主人好")))
    async with make_client(app) as client:
        await client.post("/api/chat", json={"session_id": "s1", "message": "你好"})
        response = await client.get("/api/session/s1")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "你好"
    assert body["messages"][1]["content"] == "主人好"


async def test_chat_history_of_an_unknown_session_is_empty_not_404():
    app = build_test_app(ScriptedAgent(AgentReply(content="x")))
    async with make_client(app) as client:
        response = await client.get("/api/session/never-seen")

    assert response.status_code == 200
    assert response.json()["messages"] == []


async def test_chat_history_limit_is_clamped():
    """`limit` 夹在 1..200：传 0 也给一条，传天文数字也不超过上限。"""
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    db = Storage(":memory:")
    ensure_schema(db)
    app = build_test_app(ScriptedAgent(AgentReply(content="嗯")))
    app.state.session_store = SessionStore(max_messages=40, storage=db)

    async with make_client(app) as client:
        for i in range(3):
            await client.post(
                "/api/chat", json={"session_id": "s1", "message": f"第{i}句"}
            )
        wide = (await client.get("/api/session/s1", params={"limit": 9999})).json()
        tiny = (await client.get("/api/session/s1", params={"limit": 0})).json()

    assert len(wide["messages"]) == 6, "三轮问答 = 6 条"
    assert len(tiny["messages"]) == 1, "limit=0 也要夹到 1 条"


async def test_chat_history_survives_a_restart_through_the_endpoint():
    """新进程（新 store 实例 + 同一个库）打开后，接口能拿回上次的对话。"""
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    db = Storage(":memory:")
    ensure_schema(db)

    first = build_test_app(ScriptedAgent(AgentReply(content="记住了")))
    first.state.session_store = SessionStore(max_messages=40, storage=db)
    async with make_client(first) as client:
        await client.post("/api/chat", json={"session_id": "s1", "message": "我叫小明"})

    # 模拟重启：全新的 app + 全新的 store，只共享库
    second = build_test_app(ScriptedAgent(AgentReply(content="x")))
    second.state.session_store = SessionStore(max_messages=40, storage=db)
    async with make_client(second) as client:
        response = await client.get("/api/session/s1")

    assert [m["content"] for m in response.json()["messages"]] == ["我叫小明", "记住了"]

async def test_an_empty_session_returns_the_opening_line():
    """历史为空时带上开场白（第 143 步 / 人设.md 第九节）。

    只在空会话给 —— 有历史还念一遍的话，每次打开窗口都会多一句"你回来啦"。
    """
    from core.persona import chat_greeting

    app = build_test_app(ScriptedAgent(AgentReply(content="嗯")))

    async with make_client(app) as client:
        response = await client.get("/api/session/empty-session")

    body = response.json()
    assert body["messages"] == []
    assert body["greeting"] == chat_greeting()
    assert "karo" in body["greeting"]


async def test_a_session_with_history_has_no_opening_line():
    app = build_test_app(ScriptedAgent(AgentReply(content="嗯")))
    await app.state.session_store.append(
        "has-history", Message(role="user", content="早")
    )

    async with make_client(app) as client:
        response = await client.get("/api/session/has-history")

    body = response.json()
    assert body["messages"], "应当有历史"
    assert body["greeting"] == "", "有历史就不该再念开场白"
