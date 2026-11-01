"""最小可交付性：Bearer token、会话归属、限流、Origin 校验；默认不配 token 时行为不变。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from agents.agent import AgentReply  # noqa: E402
from api.routes import router  # noqa: E402
from core.config import Settings  # noqa: E402
from core.session import SessionStore  # noqa: E402
from tests.fakes import EchoTool  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402


class ScriptedAgent:
    def __init__(self, reply: AgentReply) -> None:
        self.reply = reply

    async def run(self, messages: list[Any]) -> AgentReply:
        return self.reply


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def build_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    if settings is not None:
        from api.auth import AuthGate

        app.add_middleware(AuthGate, settings=settings)
    app.include_router(router)
    registry = ToolRegistry()
    registry.register(EchoTool())
    app.state.registry = registry
    app.state.session_store = SessionStore(max_messages=40)
    app.state.agent_factory = lambda: ScriptedAgent(AgentReply(content="好"))
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


TOKENS = "alice:ta,bob:tb"


async def test_missing_or_wrong_token_is_401_but_valid_one_passes() -> None:
    app = build_app(make_settings(auth_tokens=TOKENS))
    async with make_client(app) as client:
        anonymous = await client.get("/api/session/s1")
        wrong = await client.get("/api/session/s1", headers={"Authorization": "Bearer nope"})
        ok = await client.get("/api/session/s1", headers={"Authorization": "Bearer ta"})

    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.headers.get("WWW-Authenticate") == "Bearer"
    assert anonymous.headers.get("X-Auth-Required") == "1", "前端靠这个头区分「缺令牌」与普通 401"
    assert wrong.status_code == 401
    assert ok.status_code == 200


async def test_two_tokens_cannot_read_each_others_history() -> None:
    """验收：A 写进 s1，B 用 s1 读到的是空；A 自己能读到。"""
    app = build_app(make_settings(auth_tokens=TOKENS))
    alice = {"Authorization": "Bearer ta"}
    bob = {"Authorization": "Bearer tb"}

    async with make_client(app) as client:
        sent = await client.post(
            "/api/chat", json={"message": "在吗", "session_id": "shared-id"}, headers=alice
        )
        assert sent.status_code == 200, sent.text

        a_view = await client.get("/api/session/shared-id", headers=alice)
        b_view = await client.get("/api/session/shared-id", headers=bob)

    assert [m["content"] for m in a_view.json()["messages"]][0] == "在吗"
    assert b_view.json()["messages"] == [], "B 不该读到 A 的会话"


async def test_cookie_token_is_accepted_for_image_requests() -> None:
    """`<img src>` 没法带 Authorization 头，所以媒体请求只能靠 cookie。

    这是第 255 步的真事故：开了 AUTH_TOKENS 后食谱缩略图与路线图都变 401，
    界面看起来像「图片丢了 / 地图没额度」。
    """
    from api.auth import AUTH_COOKIE_NAME

    app = build_app(make_settings(auth_tokens=TOKENS))
    async with make_client(app) as client:
        ok = await client.get(
            "/api/session/s1", headers={"Cookie": f"{AUTH_COOKIE_NAME}=ta"}
        )
        bad = await client.get(
            "/api/session/s1", headers={"Cookie": f"{AUTH_COOKIE_NAME}=nope"}
        )
        header_wins = await client.get(
            "/api/session/s1",
            headers={"Cookie": f"{AUTH_COOKIE_NAME}=nope", "Authorization": "Bearer ta"},
        )

    assert ok.status_code == 200
    assert bad.status_code == 401
    assert header_wins.status_code == 200, "头优先于 cookie（填了新 token 不必等 cookie 过期）"


async def test_reset_of_another_users_session_does_not_touch_it() -> None:
    app = build_app(make_settings(auth_tokens=TOKENS))
    alice = {"Authorization": "Bearer ta"}
    bob = {"Authorization": "Bearer tb"}

    async with make_client(app) as client:
        await client.post(
            "/api/chat", json={"message": "别删我", "session_id": "keep"}, headers=alice
        )
        deleted = await client.delete("/api/sessions/keep", headers=bob)
        still_there = await client.get("/api/session/keep", headers=alice)

    assert deleted.status_code == 200, "别人的会话对 B 来说是空的，删也删不到东西"
    assert [m["content"] for m in still_there.json()["messages"]][0] == "别删我"


async def test_rate_limit_returns_429_with_retry_after() -> None:
    app = build_app(make_settings(auth_tokens=TOKENS, auth_rate_limit=2))
    headers = {"Authorization": "Bearer ta"}

    async with make_client(app) as client:
        first = await client.get("/api/session/x", headers=headers)
        second = await client.get("/api/session/x", headers=headers)
        third = await client.get("/api/session/x", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429, third.text
    assert int(third.headers["Retry-After"]) >= 1

    # 换一个 token 不该被 alice 的限额牵连
    async with make_client(app) as client:
        other = await client.get("/api/session/x", headers={"Authorization": "Bearer tb"})
    assert other.status_code == 200


async def test_origin_check_rejects_unknown_browser_origin() -> None:
    app = build_app(
        make_settings(
            auth_tokens=TOKENS, auth_allowed_origins="http://localhost:8000"
        )
    )
    headers = {"Authorization": "Bearer ta"}

    async with make_client(app) as client:
        blocked = await client.get(
            "/api/session/x", headers={**headers, "Origin": "http://evil.example"}
        )
        allowed = await client.get(
            "/api/session/x", headers={**headers, "Origin": "http://localhost:8000"}
        )
        no_origin = await client.get("/api/session/x", headers=headers)

    assert blocked.status_code == 403, blocked.text
    assert allowed.status_code == 200
    assert no_origin.status_code == 200, "命令行/同源请求没有 Origin，不该被拦"


async def test_auth_is_off_by_default_and_behaviour_is_unchanged() -> None:
    """没配 AUTH_TOKENS 时就是现在的本机模式：没有头也能读。"""
    app = build_app(None)
    async with make_client(app) as client:
        response = await client.get("/api/session/s1")
    assert response.status_code == 200
