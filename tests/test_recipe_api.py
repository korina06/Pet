"""食谱接口的剩余四个：路由清单、schema 文案、会话恢复、重置（`/ask` 的测试在 test_recipe_ask.py）。
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from api.recipe_routes import router
from core.recipes import DISCLAIMER, GREETING, SAMPLE_MESSAGE, RecipeSessionStore
from core.schemas import Message


def build_app(store: RecipeSessionStore | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.recipe_store = store if store is not None else RecipeSessionStore()
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_all_recipe_routes_are_registered():
    """只有这几个接口：删掉的 /message 与 /plan **不能再被注册回来**。

    第 73 步加了 `/ask/stream`（真流式）——它是 `/ask` 的同构版本，
    契约完全一样（收尾的 meta 里就是 /ask 的响应体），所以不算"多一条功能线"。
    """
    paths = {route.path for route in router.routes}
    assert paths == {
        "/api/recipe/schema",
        "/api/recipe/ask",
        "/api/recipe/ask/stream",
        "/api/recipe/session",
        "/api/recipe/reset",
    }


def test_the_removed_endpoints_are_really_gone():
    """反转断言：多天菜单与需求收集这条路已经被拆掉了。"""
    paths = {route.path for route in router.routes}
    assert "/api/recipe/message" not in paths
    assert "/api/recipe/plan" not in paths


async def test_schema_is_self_contained_so_the_frontend_hardcodes_nothing():
    """开场白 / 示例句 / 免责声明都由后端给。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.get("/api/recipe/schema")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "greeting": GREETING,
        "example": SAMPLE_MESSAGE,
        "disclaimer": DISCLAIMER,
    }


async def test_schema_no_longer_ships_the_multi_day_structure():
    """反转断言：13 个需求维度、天数范围、快捷问题都不该再返回。

    前端已经按 `example` 一个字段渲染 placeholder，多返回只会让两边继续脱节。
    """
    app = build_app()
    async with make_client(app) as client:
        body = (await client.get("/api/recipe/schema")).json()

    for removed in ("fields", "groups", "days", "quick_prompts", "meals"):
        assert removed not in body, f"schema 不该再返回 {removed}"


async def test_session_returns_history_messages_only():
    store = RecipeSessionStore()
    store.append(
        "s1",
        Message(role="user", content="晚饭想吃鱼"),
        Message(role="assistant", content="清蒸鲈鱼 20 分钟"),
    )
    app = build_app(store)

    async with make_client(app) as client:
        body = (await client.get("/api/recipe/session", params={"session_id": "s1"})).json()

    assert body == {
        "session_id": "s1",
        "messages": [
            # recipes 是第 137 步加的：关窗重开后靠它把卡片画回来
            {"role": "user", "content": "晚饭想吃鱼", "recipes": []},
            {"role": "assistant", "content": "清蒸鲈鱼 20 分钟", "recipes": []},
        ],
    }


async def test_session_no_longer_carries_profile_or_plan():
    """反转断言：画像与菜单记录随多天功能一起删了。"""
    app = build_app()
    async with make_client(app) as client:
        body = (await client.get("/api/recipe/session", params={"session_id": "x"})).json()

    for removed in ("profile", "plan", "plan_count", "groups", "missing", "ready"):
        assert removed not in body


async def test_unknown_session_is_empty_not_an_error():
    """没聊过是正常状态，不该 404。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.get("/api/recipe/session", params={"session_id": "没聊过"})

    assert response.status_code == 200
    assert response.json()["messages"] == []


async def test_reset_clears_the_session():
    store = RecipeSessionStore()
    store.append("s1", Message(role="user", content="x"))
    app = build_app(store)

    async with make_client(app) as client:
        body = (await client.post(
            "/api/recipe/reset", json={"session_id": "s1"}
        )).json()

    assert body == {"status": "cleared", "session_id": "s1"}
    assert store.get("s1") is None


async def test_reset_on_an_unknown_session_is_still_ok():
    """幂等：清一个不存在的会话不该报错（前端"新建会话"可能重复点）。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/recipe/reset", json={"session_id": "没聊过"})

    assert response.status_code == 200
    assert response.json()["status"] == "cleared"


async def test_missing_store_is_a_readable_503():
    """没挂 store 时明说"未初始化"，而不是抛一个 500 栈。"""
    app = FastAPI()          # 不挂 router 之外的任何 state
    app.include_router(router)
    async with make_client(app) as client:
        response = await client.get("/api/recipe/session", params={"session_id": "s1"})

    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]
