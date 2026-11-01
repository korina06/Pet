"""食谱「新建会话」：清干净对话与画像快照，但不动忌口、菜谱库和会话 id。
"""

from __future__ import annotations

import asyncio

from core.history import SCOPE_RECIPE, count_messages, load_messages
from core.recipe_library import ensure_schema, list_aversions, set_aversions
from core.recipe_session_meta import load_meta
from core.recipes import RecipeSessionStore
from core.schemas import Message
from core.storage import Storage


def _store() -> tuple[RecipeSessionStore, Storage]:
    db = Storage(":memory:")
    ensure_schema(db)
    return RecipeSessionStore(storage=db), db


def test_reset_clears_the_conversation_everywhere():
    store, db = _store()
    session = store.ensure("s1")
    session.recent_tags = ["辣"]
    session.aversions_snapshot = ["香菜"]
    store.save_meta(session)
    store.append(
        "s1",
        Message(role="user", content="想吃鱼"),
        Message(role="bot", content="给你找了三道"),
    )

    assert count_messages(db, SCOPE_RECIPE, "s1") == 2
    assert load_meta(db, "s1") == (["辣"], ["香菜"])

    store.reset("s1")

    assert store.get("s1") is None, "内存里的会话没清（reset 应当把它移除）"
    assert count_messages(db, SCOPE_RECIPE, "s1") == 0, "库里的历史没清（重启会冒出来）"
    # `load_meta` 缺行时返回空元组（不是 None）—— 清空后应当回到这个形态
    assert load_meta(db, "s1") == ([], []), "会话小状态没清"


def test_reset_does_not_touch_long_term_aversions():
    """忌口是长期偏好，不该被"新建会话"顺手清掉。"""
    store, db = _store()
    set_aversions(db, ["香菜", "花生"])
    store.append("s1", Message(role="user", content="随便"))

    store.reset("s1")

    assert list_aversions(db) == ["香菜", "花生"], "长期忌口被误删了"


def test_reset_of_an_unknown_session_is_harmless():
    store, _db = _store()

    store.reset("never-existed")           # 不该抛
    assert store.get("never-existed") is None


def test_the_route_returns_cleared_and_the_session_is_empty():
    import httpx
    from fastapi import FastAPI

    from api.recipe_routes import router

    store, _db = _store()
    app = FastAPI()
    app.include_router(router)
    app.state.recipe_store = store
    store.append("s1", Message(role="user", content="旧问题"))

    async def run() -> tuple[dict, dict]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            reset = await client.post("/api/recipe/reset", json={"session_id": "s1"})
            restored = await client.get("/api/recipe/session", params={"session_id": "s1"})
            return reset.json(), restored.json()

    reset_payload, session_payload = asyncio.run(run())

    assert reset_payload["status"] == "cleared"
    assert reset_payload["session_id"] == "s1", "会话 id 不该变"
    assert session_payload["messages"] == []


def test_the_history_endpoint_does_not_remember_old_messages():
    """reset 之后重新读历史必须是空的 —— 这是"新建会话"最直接的验收。"""
    store, db = _store()
    store.append("s1", Message(role="user", content="上一段对话"))

    store.reset("s1")

    assert load_messages(db, SCOPE_RECIPE, "s1", 50) == []
