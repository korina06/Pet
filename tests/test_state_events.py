"""Agent 循环的状态事件：断言走过的状态序列，并钉住"状态事件不上 SSE"这条边界。
"""

from __future__ import annotations

import asyncio

from agents.agent import Agent
from core.schemas import Message, ToolCall, ToolResult
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _Echo(BaseTool):
    name = "echo"
    description = "回声"
    show_in_menu = False
    parameters = {"type": "object", "properties": {}}

    async def run(self, **arguments) -> ToolResult:
        return ToolResult(tool_name=self.name, ok=True, content="ok")


def _agent(llm, *, max_steps: int = 3, budget: int = 0) -> Agent:
    registry = ToolRegistry()
    registry.register(_Echo())
    return Agent(
        llm=llm, registry=registry, max_steps=max_steps, turn_token_budget=budget
    )


class _AnswersDirectly:
    async def complete(self, messages, tools=None):
        return Message(role="assistant", content="好了")


class _UsesToolOnce:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="echo", arguments={}, raw_arguments="{}")
                ],
            )
        return Message(role="assistant", content="查好了")


class _AlwaysWantsTools:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        return Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id=f"c{self.calls}", name="echo", arguments={}, raw_arguments="{}")
            ],
        )


class _Fails:
    async def complete(self, messages, tools=None):
        from agents.llm import LLMError

        raise LLMError("DeepSeek 限流（HTTP 429）")


class _BurnsBudget:
    """第一步就把额度烧光，第二步起应当被拦住。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None):
        from core import usage as usage_store

        self.calls += 1
        usage_store.add_turn_tokens(50000)
        return Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id=f"c{self.calls}", name="echo", arguments={}, raw_arguments="{}")
            ],
        )


def _run(agent: Agent) -> tuple[str, ...]:
    from core import usage as usage_store

    usage_store.reset_turn_tokens()
    reply = asyncio.run(agent.run([Message(role="user", content="hi")]))
    return reply.states


def test_no_tool_needed_is_plan_then_answer():
    assert _run(_agent(_AnswersDirectly())) == ("plan", "answer")


def test_one_tool_round_is_plan_act_observe_plan_answer():
    assert _run(_agent(_UsesToolOnce())) == (
        "plan",
        "act",
        "observe",
        "plan",
        "answer",
    )


def test_a_model_failure_is_plan_then_degrade():
    assert _run(_agent(_Fails())) == ("plan", "degrade")


def test_step_exhaustion_ends_with_degrade():
    states = _run(_agent(_AlwaysWantsTools(), max_steps=2))

    assert states.count("observe") == 2, states
    assert states[-1] == "degrade", states
    assert states.count("plan") == 2, states


def test_budget_stop_is_plan_act_observe_plan_degrade():
    states = _run(_agent(_BurnsBudget(), max_steps=5, budget=1000))

    assert states == ("plan", "act", "observe", "plan", "degrade"), states


def test_states_do_not_change_the_visible_behaviour():
    """加状态只是"多记一笔"：内容、步数、工具结果都不能变。"""
    reply = asyncio.run(
        _agent(_UsesToolOnce()).run([Message(role="user", content="hi")])
    )

    assert reply.content == "查好了"
    assert reply.steps == 1
    assert [inv.tool_name for inv in reply.tool_invocations] == ["echo"]
    assert reply.error is None


def test_state_events_are_not_sent_over_sse():
    """状态事件**不进 SSE**：前端不消费它，多一个协议类型只会多三处维护。"""
    from fastapi import FastAPI

    from agents.agent import AgentEvent
    from api.routes import chat_stream
    from api.routes import router as chat_router
    from core.session import SessionStore
    from tests.test_chat_stream import _StubRequest, _payload

    class _StateThenDelta:
        async def run_stream(self, messages):
            yield AgentEvent(type="state", detail="plan", step=1)
            yield AgentEvent(type="delta", detail="好")
            yield AgentEvent(type="state", detail="answer", step=1)
            yield AgentEvent(type="final", detail="好了")

    app = FastAPI()
    app.include_router(chat_router)
    app.state.session_store = SessionStore(max_messages=40)
    app.state.agent_factory = lambda: _StateThenDelta()
    app.state.registry = ToolRegistry()

    async def run() -> str:
        response = await chat_stream(_payload(), _StubRequest(app))
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(run())

    assert '"type": "delta"' in body
    assert '"state"' not in body, "状态事件不该出现在 SSE 协议里"
