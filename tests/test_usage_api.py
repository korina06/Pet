"""用量账本的查询接口：`/api/usage/summary` 与 `/api/usage/recent`。

重点是按功能分得开、没有 usage 的调用单独计数。
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from api.usage_routes import router as usage_router
from core import usage
from core.recipe_library import ensure_schema
from core.storage import Storage


def _db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    return db


def _app(db: Storage) -> FastAPI:
    app = FastAPI()
    app.include_router(usage_router)
    app.state.storage = db
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _record(db, **overrides):
    base = dict(
        provider="deepseek",
        model="deepseek-chat",
        feature="chat",
        session_id="s1",
        turn_id="t1",
        step=1,
        stream=False,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        finish_reason="stop",
        latency_ms=500,
    )
    base.update(overrides)
    usage.record(db, **base)


async def test_summary_groups_by_feature_and_skips_probe_noise() -> None:
    db = _db()
    _record(db, feature="chat")
    _record(db, feature="recipe", turn_id="t2")
    _record(
        db, feature="probe", provider="glm", model="glm-4-flash",
        usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    )

    async with _client(_app(db)) as client:
        response = await client.get("/api/usage/summary")

    assert response.status_code == 200
    body = response.json()
    features = {row["feature"]: row for row in body["features"]}
    assert set(features) == {"chat", "recipe", "probe"}
    assert features["probe"]["provider"] == "glm"
    assert body["total"]["calls"] == 3
    assert body["total"]["total"] == 252


async def test_summary_can_filter_one_feature() -> None:
    db = _db()
    _record(db, feature="chat")
    _record(db, feature="recipe")

    async with _client(_app(db)) as client:
        response = await client.get("/api/usage/summary?feature=recipe")

    rows = response.json()["features"]
    assert [row["feature"] for row in rows] == ["recipe"]


async def test_summary_reports_missing_usage_separately() -> None:
    """没有 usage 的调用要单独计数 —— 它不等于"花了 0"。"""
    db = _db()
    _record(db, usage=None)

    async with _client(_app(db)) as client:
        body = (await client.get("/api/usage/summary")).json()

    assert body["total"]["missing"] == 1
    assert body["total"]["total"] == 0


async def test_recent_lists_calls_newest_first_with_meta() -> None:
    db = _db()
    _record(db, step=1, turn_id="t1")
    _record(db, step=2, turn_id="t1", finish_reason="length")

    async with _client(_app(db)) as client:
        body = (await client.get("/api/usage/recent?limit=5")).json()

    calls = body["calls"]
    assert [row["step"] for row in calls] == [2, 1]
    assert calls[0]["finish_reason"] == "length"
    assert calls[0]["usage_source"] == "api"


async def test_the_endpoints_say_503_without_a_database() -> None:
    app = FastAPI()
    app.include_router(usage_router)
    async with _client(app) as client:
        response = await client.get("/api/usage/summary")

    assert response.status_code == 503


async def test_the_probe_route_attributes_its_own_usage_to_probe() -> None:
    """真实点「测一下」时，账本里必须是 feature=probe（第 114 步 / 评价6 第 1 条）。

    之前只有"手工写一条 feature=probe"的测试 —— 那证明不了**路由自己**有没有设。
    这条走真路由：ModelRouter + 假 HTTP 客户端 + 真 SQLite。
    """
    import httpx
    from fastapi import FastAPI

    from agents.model_router import ModelRouter
    from api.model_routes import router as model_router
    from core import usage
    from core.config import Settings
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    class _Reply:
        is_success = True

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "可用"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            }

        @property
        def text(self):
            return ""

    class _Client:
        async def post(self, *_a, **_k):
            return _Reply()

    db = Storage(":memory:")
    ensure_schema(db)
    router_obj = ModelRouter(Settings(deepseek_api_key="k"), client=_Client(), storage=db)
    app = FastAPI()
    app.include_router(model_router)
    app.state.model_router = router_obj

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/model/probe")

    assert response.status_code == 200
    assert response.json()["ok"] is True

    rows = usage.recent(db, 5)
    assert rows, "探活也要落账"
    assert rows[0]["feature"] == "probe", f"实际是 {rows[0]['feature']!r}"
    assert rows[0]["turn_id"], "探活也要有自己的 turn（别混进真实对话）"
    assert rows[0]["total"] == 9
