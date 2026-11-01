"""可选的多用户鉴权：Bearer token、按 token 限流、Origin 校验与会话归属隔离。

`AUTH_TOKENS` 为空时保持本机模式，行为不变；配了之后所有 `/api/*` 都要带 token。
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from core.config import Settings, get_settings

#: 不需要 token 的路径（存活探针：部署平台要能探活）
PUBLIC_PATHS = frozenset({"/api/health"})

#: 限流窗口（秒）
RATE_WINDOW_SECONDS = 60

#: 给 `<img src>` 用的 cookie 名（第 255 步）。
#: 图片标签没法带 Authorization 头，只能靠浏览器自动携带的同源 cookie。
AUTH_COOKIE_NAME = "karo_auth"


def parse_tokens(raw: str) -> dict[str, str]:
    """``alice:ta,bob:tb`` → ``{"ta": "alice", "tb": "bob"}``。

    格式错（缺冒号 / 空名字 / 空 token / 重复 token）直接抛 ValueError：
    鉴权配置写错时必须启动失败，而不是悄悄退化成「没有鉴权」。
    """
    tokens: dict[str, str] = {}
    seen_names: set[str] = set()
    for part in str(raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        name, sep, token = item.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name or not token:
            raise ValueError(
                f"AUTH_TOKENS 格式应为 name:token,name2:token2，收到 {item!r}"
            )
        if token in tokens:
            raise ValueError(f"AUTH_TOKENS 里有重复的 token（owner={name}）")
        if name in seen_names:
            raise ValueError(f"AUTH_TOKENS 里有重复的 owner 名：{name}")
        tokens[token] = name
        seen_names.add(name)
    return tokens


def scoped_session_id(session_id: str, owner: str) -> str:
    """把 owner 拼进会话 id。本机模式（owner=local）**原样返回**，不迁移旧数据。"""
    safe_owner = str(owner or "").strip() or "local"
    if safe_owner == "local":
        return str(session_id)
    return f"u:{safe_owner}/{session_id}"


def owner_of(request: Any) -> str:
    """从 request.state 取 owner；没有中间件（单测/本机）时是 local。"""
    try:
        value = getattr(request.state, "owner", None)
    except (AttributeError, KeyError):
        return "local"
    return str(value or "local")


class AuthGate:
    """纯 ASGI 中间件：不读 body，所以对 POST/SSE 都没有副作用。"""

    def __init__(self, app: Any, settings: Settings | None = None) -> None:
        self.app = app
        self.settings = settings or get_settings()
        self.tokens = parse_tokens(getattr(self.settings, "auth_tokens", ""))
        self.rate_limit = max(0, int(getattr(self.settings, "auth_rate_limit", 0) or 0))
        origins = getattr(self.settings, "auth_allowed_origins", "") or ""
        self.origins = {item.strip() for item in str(origins).split(",") if item.strip()}
        self._hits: dict[str, deque[float]] = {}
        self._retry_after = 1

    # ------------------------------------------------------------------ ASGI

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        path = scope.get("path", "")

        if self.origins:
            origin = headers.get("origin")
            if origin and origin not in self.origins:
                await _send_json(
                    send, 403, "Origin 不在允许列表里", extra_headers=None
                )
                return

        if not self.tokens or not path.startswith("/api/") or path in PUBLIC_PATHS:
            state["owner"] = "local"
            await self.app(scope, receive, send)
            return

        token = _bearer_token(headers.get("authorization", "")) or _cookie_token(
            headers.get("cookie", "")
        )
        owner = self.tokens.get(token) if token else None
        if owner is None:
            await _send_json(
                send,
                401,
                "缺少或无效的 Bearer token",
                extra_headers={
                    "WWW-Authenticate": "Bearer",
                    # 第 254 步：前端只在这个头出现时弹 token 输入框；
                    # 别的 401（比如邮箱登录失败）不能被误当成「需要令牌」。
                    "X-Auth-Required": "1",
                },
            )
            return

        if self.rate_limit and not self._allow(owner):
            await _send_json(
                send,
                429,
                f"请求太频繁（每 {RATE_WINDOW_SECONDS} 秒最多 {self.rate_limit} 次）",
                extra_headers={"Retry-After": str(self._retry_after)},
            )
            return

        state["owner"] = owner
        await self.app(scope, receive, send)

    # ------------------------------------------------------------- 限流实现

    def _allow(self, owner: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(owner, deque())
        while bucket and now - bucket[0] >= RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= self.rate_limit:
            self._retry_after = max(
                1, int(RATE_WINDOW_SECONDS - (now - bucket[0])) + 1
            )
            return False
        bucket.append(now)
        return True


def _bearer_token(header: str) -> str:
    value = str(header or "").strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def _cookie_token(header: str) -> str:
    """从 `Cookie` 头里取 `karo_auth`；没有就返回空串。"""
    from urllib.parse import unquote

    for part in str(header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == AUTH_COOKIE_NAME and value:
            return unquote(value.strip())
    return ""


async def _send_json(
    send: Any,
    status: int,
    detail: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for key, value in (extra_headers or {}).items():
        headers.append((key.lower().encode("latin-1"), str(value).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
