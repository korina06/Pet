"""聊天流式接口：用户消息开始就落库、助手消息只在 final 后落库；正常流、工具调用、模型失败、断开、取消五类都要覆盖。
"""

from __future__ import annotations

import types

import asyncio

from agents.agent import AgentEvent
from agents.llm import StreamEvent  # noqa: F401  （便于对照协议）
from api.routes import ChatRequest, chat_stream
from core.schemas import ToolResult
from tests.test_api import build_test_app


class _StreamingAgent:
    """按预设事件序列吐流的假 Agent。"""

    def __init__(self, events: list[AgentEvent], *, raises: BaseException | None = None) -> None:
        self.events = events
        self.raises = raises
        self.calls = 0

    async def run_stream(self, messages):
        self.calls += 1
        for event in self.events:
            yield event
        if self.raises is not None:
            raise self.raises


class _StubRequest:
    def __init__(self, app, *, gone_after_checks: int = 99) -> None:
        self.app = app
        # 第 119 步起路由会往 request.state 上写 feature
        self.state = types.SimpleNamespace()
        self._checks = 0
        self._gone_after = gone_after_checks

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._gone_after


def _events(*items: AgentEvent) -> list[AgentEvent]:
    return list(items)


async def _collect(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def _payload(message: str = "你好") -> ChatRequest:
    return ChatRequest(session_id="s1", message=message)


async def test_a_normal_stream_emits_deltas_then_meta_and_persists_both_messages():
    agent = _StreamingAgent(
        _events(
            AgentEvent(type="delta", detail="主人"),
            AgentEvent(type="delta", detail="好"),
            AgentEvent(type="final", detail="主人好"),
        )
    )
    app = build_test_app(agent)
    response = await chat_stream(_payload(), _StubRequest(app))
    body = await _collect(response)

    assert body.count('"type": "delta"') == 2
    assert '"type": "meta"' in body
    assert '"content": "主人好"' in body

    history = await app.state.session_store.get_history("s1")
    assert [(m.role, m.content) for m in history] == [
        ("user", "你好"),
        ("assistant", "主人好"),
    ]


async def test_a_tool_step_emits_reset_so_the_bubble_does_not_keep_stale_text():
    agent = _StreamingAgent(
        _events(
            AgentEvent(type="delta", detail="我查查"),
            AgentEvent(type="reset"),
            AgentEvent(
                type="tool_result",
                result=ToolResult(tool_name="echo", ok=True, content="ok", arguments={}),
            ),
            AgentEvent(type="final", detail="查好了"),
        )
    )
    app = build_test_app(agent)
    body = await _collect(await chat_stream(_payload(), _StubRequest(app)))

    assert '"type": "reset"' in body, "这一轮要调工具，前端必须清掉刚才那半句"
    assert '"steps": 1' in body
    history = await app.state.session_store.get_history("s1")
    assert history[-1].content == "查好了", "历史里只该有最终回答"


async def test_a_model_failure_is_still_a_readable_message():
    agent = _StreamingAgent(
        _events(
            AgentEvent(type="error", detail="DeepSeek 限流（HTTP 429）"),
        )
    )
    app = build_test_app(agent)
    body = await _collect(await chat_stream(_payload(), _StubRequest(app)))

    assert '"type": "error"' in body
    assert "抱歉" in body, "错误也要是一句人话，不是栈"
    assert '"type": "meta"' in body, "失败也要收尾，否则气泡没有结论"
    history = await app.state.session_store.get_history("s1")
    assert history[-1].role == "assistant", "道歉也是一句完整的话，可以入历史"


async def test_a_client_that_leaves_gets_only_its_own_question_saved():
    agent = _StreamingAgent(
        _events(
            AgentEvent(type="delta", detail="半"),
            AgentEvent(type="delta", detail="句"),
            AgentEvent(type="final", detail="半句"),
        )
    )
    app = build_test_app(agent)
    request = _StubRequest(app, gone_after_checks=1)
    body = await _collect(await chat_stream(_payload(), request))

    assert '"type": "meta"' not in body, "断开后不该再发收尾"
    history = await app.state.session_store.get_history("s1")
    assert [(m.role, m.content) for m in history] == [("user", "你好")]


async def test_cancellation_does_not_write_a_half_answer():
    """外层取消（Starlette 在客户端断开时取消任务）也不能写半句回答。"""
    agent = _StreamingAgent(
        _events(AgentEvent(type="delta", detail="半")),
        raises=asyncio.CancelledError(),
    )
    app = build_test_app(agent)
    response = await chat_stream(_payload(), _StubRequest(app))

    chunks: list[str] = []
    try:
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    except asyncio.CancelledError:
        pass

    history = await app.state.session_store.get_history("s1")
    assert [(m.role, m.content) for m in history] == [("user", "你好")]
