"""流式取消语义：客户端断开就停跑模型、不再发事件、历史里只留用户那句。
"""

from __future__ import annotations

import types

from agents.llm import StreamEvent
from api.recipe_routes import AskRequest, ask_stream
from core.schemas import Message
from tests.test_recipe_ask import build_app


class _StreamingLLM:
    """会吐若干个 delta 再给最终消息的假模型（够 Agent 循环用）。"""

    def __init__(self, pieces: tuple[str, ...] = ("清", "蒸", "鲈", "鱼")) -> None:
        self.pieces = pieces
        self.calls = 0

    async def complete(self, messages, tools=None) -> Message:
        self.calls += 1
        return Message(role="assistant", content="".join(self.pieces))

    async def complete_stream(self, messages, tools=None):
        self.calls += 1
        for piece in self.pieces:
            yield StreamEvent(kind="delta", text=piece)
        yield StreamEvent(
            kind="done", message=Message(role="assistant", content="".join(self.pieces))
        )


class _StubRequest:
    """只提供端点用到的两样：`app.state` 与 `is_disconnected()`。"""

    def __init__(self, app, *, gone_after_checks: int) -> None:
        self.app = app
        # 第 119 步起路由会往 request.state 上写 feature
        self.state = types.SimpleNamespace()
        self._checks = 0
        self._gone_after = gone_after_checks

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._gone_after


async def _collect(response) -> list[str]:
    return [chunk async for chunk in response.body_iterator]


async def test_client_disconnect_stops_the_stream_and_keeps_only_the_question():
    llm = _StreamingLLM()
    app = build_app(llm=llm)
    payload = AskRequest(session_id="s1", message="想吃鱼", wanted=[])
    # 前两次检查说"还在"，之后就说"走了"。
    # 为什么是 2 而不是 1：第 136 步起流的第一条是 `state`(plan)，
    # 它不会变成任何 SSE 事件；所以要让**至少一个 delta** 真正发出去，
    # 得放过前两条（state + 第一个 delta）。
    request = _StubRequest(app, gone_after_checks=2)

    response = await ask_stream(payload, request)
    chunks = await _collect(response)
    body = "".join(chunks)

    # 第 136 步起，流的第一条是 `state`(plan) 而不是 delta ——
    # 所以判据改成"断开前已经发出过至少一条事件"，意图不变：
    # 断开之后不该再有任何事件。
    assert chunks, "断开前应当已经发出过事件"
    assert '"type": "meta"' not in body, "断开后不该再发收尾事件"
    assert llm.calls == 1, "断开后不该再发起新的模型调用"

    messages = app.state.recipe_store.ensure("s1").messages
    assert [m.role for m in messages] == ["user"], (
        f"历史里只该有用户那句，实际：{[(m.role, m.content) for m in messages]}"
    )
    assert messages[0].content == "想吃鱼"


async def test_a_client_that_disconnects_before_the_first_event_gets_nothing():
    llm = _StreamingLLM()
    app = build_app(llm=llm)
    payload = AskRequest(session_id="s1", message="想吃鱼", wanted=[])
    request = _StubRequest(app, gone_after_checks=0)     # 一开始就不在了

    response = await ask_stream(payload, request)
    chunks = await _collect(response)

    assert chunks == [], "没人在听就不该往外写"
    messages = app.state.recipe_store.ensure("s1").messages
    assert [m.role for m in messages] == ["user"], "提问仍然要留在历史里"


async def test_a_normal_stream_is_untouched_by_the_cancel_logic():
    """没断开时行为必须与以前完全一致（回归保护）。"""
    llm = _StreamingLLM()
    app = build_app(llm=llm)
    payload = AskRequest(session_id="s1", message="想吃鱼", wanted=[])
    request = _StubRequest(app, gone_after_checks=99)

    response = await ask_stream(payload, request)
    body = "".join(await _collect(response))

    assert '"type": "delta"' in body
    assert '"type": "meta"' in body, "正常收尾必须有 meta"
    messages = app.state.recipe_store.ensure("s1").messages
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "清蒸鲈鱼"
