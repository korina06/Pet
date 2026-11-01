"""request_id 与本地诊断：每个请求都带 X-Request-ID、能串起日志与工具 span、`/api/diagnostics` 看得到最近情况。
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from api.diagnostics_routes import router as diagnostics_router
from core import log_context, observability
from core.schemas import Message, ToolResult
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _Echo(BaseTool):
    name = "echo"
    description = "回声"
    show_in_menu = False
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **arguments) -> ToolResult:
        await asyncio.sleep(0)
        return ToolResult(
            tool_name=self.name, ok=True, content=str(arguments.get("text") or "")
        )


class _Boom(BaseTool):
    name = "boom"
    description = "会炸"
    show_in_menu = False

    async def run(self, **arguments) -> ToolResult:
        raise RuntimeError("工具内部炸了")


def _app() -> FastAPI:
    """只装诊断路由 + 一个带中间件的简化 app（照 main.py 的写法）。"""
    from core import observability as obs

    app = FastAPI()

    @app.middleware("http")
    async def request_context(request, call_next):  # pragma: no cover - 与 main 同构
        from uuid import uuid4

        request_id = (request.headers.get("X-Request-ID") or uuid4().hex[:12])[:64]
        log_context.set_request_id(request_id)
        started = __import__("time").monotonic()
        response = await call_next(request)
        ms = obs.monotonic_ms(started)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            feature, session = log_context.current()
            obs.record_request(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=ms,
                feature=feature,
                session=session,
                turn=log_context.turn_id(),
            )
        return response

    app.include_router(diagnostics_router)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_every_response_carries_a_request_id():
    observability.clear()
    app = _app()

    async def run() -> httpx.Response:
        async with _client(app) as client:
            return await client.get("/api/diagnostics")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID"), "响应必须带 X-Request-ID"
    # 诊断接口自己会把 id 回显在 body 里，两处必须一致
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_a_client_supplied_request_id_is_reused():
    app = _app()

    async def run() -> httpx.Response:
        async with _client(app) as client:
            return await client.get(
                "/api/diagnostics", headers={"X-Request-ID": "trace-abc-123"}
            )

    response = asyncio.run(run())
    assert response.headers["X-Request-ID"] == "trace-abc-123"
    assert response.json()["request_id"] == "trace-abc-123"


def test_tool_spans_are_recorded_with_the_request_context():
    """工具 span 要带上 request_id / feature / turn / step —— 这是"串起来"的关键。"""
    from agents.agent import Agent
    from tests.fakes import assistant_text, assistant_tool_call

    class _OneToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return assistant_tool_call("c1", "echo", '{"text": "hi"}')
            return assistant_text("好了")

    observability.clear()
    registry = ToolRegistry()
    registry.register(_Echo())
    agent = Agent(llm=_OneToolLLM(), registry=registry, max_steps=2)

    log_context.set_context("chat", "sess-1", "turn-1")
    log_context.set_request_id("req-1")
    asyncio.run(agent.run([Message(role="user", content="hi")]))

    spans = observability.recent_tools(5)
    assert spans, "工具调用必须留下 span"
    span = spans[0]
    assert span["name"] == "echo"
    assert span["ok"] is True
    assert span["request_id"] == "req-1"
    assert span["feature"] == "chat"
    assert span["turn"] == "turn-1"
    assert span["step"] == 1
    assert isinstance(span["ms"], int)


def test_a_failing_tool_shows_up_in_failed_tools():
    from agents.agent import Agent
    from tests.fakes import assistant_text, assistant_tool_call

    class _BoomLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return assistant_tool_call("c1", "boom", "{}")
            return assistant_text("那个工具用不了")

    observability.clear()
    registry = ToolRegistry()
    registry.register(_Boom())
    agent = Agent(llm=_BoomLLM(), registry=registry, max_steps=2)

    asyncio.run(agent.run([Message(role="user", content="hi")]))

    failed = observability.failed_tools()
    assert [span["name"] for span in failed] == ["boom"]
    assert "炸" in failed[0]["error"]


def test_diagnostics_lists_recent_requests_and_tools():
    observability.clear()
    app = _app()

    async def run() -> dict:
        async with _client(app) as client:
            await client.get("/api/diagnostics")
            response = await client.get("/api/diagnostics?limit=5")
            return response.json()

    body = asyncio.run(run())

    assert [item["path"] for item in body["requests"]], "至少要记到诊断接口自己"
    assert body["requests"][0]["status"] == 200
    assert isinstance(body["tools"], list)
    assert isinstance(body["slow_tools"], list)
    assert isinstance(body["failed_tools"], list)
    # 没有 storage 时不能 500，用量部分给空壳
    assert "usage_today" in body
    assert body["usage_today"]["total"] == {}


def test_the_buffer_is_bounded():
    """环形缓冲要有上限：诊断数据不该把内存吃光。"""
    observability.clear()
    for index in range(observability.MAX_SPANS + 50):
        observability.record_tool(name=f"t{index}", ok=True, ms=1)

    assert len(observability.recent_tools(observability.MAX_SPANS * 2)) == observability.MAX_SPANS
    newest = observability.recent_tools(1)[0]
    assert newest["name"] == f"t{observability.MAX_SPANS + 49}", "最新的在最前"


def test_feature_is_derived_from_the_path():
    """请求摘要里的 feature 用**路径**判定（第 119 步）。

    试过别的两种写法，都不行：
    - contextvar：路由跑在中间件的**子任务**里，改不回父任务；
    - `request.state`：Starlette 1.6 的中间件链里同样读不到。
    路径是确定的、不依赖执行顺序，也能单独测。
    """
    from core.observability import feature_for_path

    assert feature_for_path("/api/chat") == "chat"
    assert feature_for_path("/api/chat/stream") == "chat"
    assert feature_for_path("/api/session/abc") == "chat"
    assert feature_for_path("/api/recipe/ask/stream") == "recipe"
    assert feature_for_path("/api/model/probe") == "probe"
    assert feature_for_path("/api/trip") == "travel"
    assert feature_for_path("/api/usage/summary") == "usage"
    assert feature_for_path("/static/app.js") == ""
    assert feature_for_path("/") == ""


def test_travel_pipeline_tools_leave_spans():
    """出行不走 Agent，是流水线直接调工具 —— 它们也必须留下 span（第 124 步）。

    评价8 指出的观测盲区：原来只有 Agent 的 `_run_tool` 记 span，
    而出行那四个调用（地理编码/天气/周边/路线）在诊断页上是空白。
    """
    import asyncio
    import sys
    from pathlib import Path

    import httpx

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

    from core import observability

    import smoke_browser   # 复用烟测里的"会失败的地理编码"组合

    observability.clear()
    app = smoke_browser._build_app()
    start, end = smoke_browser._future_dates()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            await client.post(
                "/api/trip",
                json={
                    "city": "北京",
                    "start_date": start,
                    "end_date": end,
                    "transport": "公共交通",
                    "stay": "不住宿",
                    "preferences": [],
                },
            )

    asyncio.run(run())

    spans = observability.recent_tools(10)
    assert spans, "出行工具调用没有留下任何 span"
    assert spans[0]["name"] == "geocode_city", spans
    assert spans[0]["ok"] is False, "失败应当如实记成 ok=False"
    assert spans[0]["feature"] == "travel", spans[0]
    assert "不可用" in spans[0]["error"] or spans[0]["error"], spans[0]
