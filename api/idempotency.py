"""幂等键：让带 `Idempotency-Key` 的请求在重发时拿到同一份结果。

只挂在耗时且消耗外部配额的接口上；不带这个头时行为与以前完全一致。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

#: 缓存多久：够覆盖"客户端/代理重试"的时间窗，又不会让旧响应卡住下一次真实请求
DEFAULT_TTL_SECONDS = 600.0
#: inflight 记录最长挂多久。没有这个上限时，一次被取消的请求会让同一个 key
#: **一直 409 到进程重启**（评价9 P2）。
INFLIGHT_TTL_SECONDS = 120.0

#: 路由可以用这个响应头说"这次别缓存"（例如行程生成返回 status=failed）。
#: 中间件不认识业务语义，让路由来表态更干净。
NO_STORE_HEADER = b"idempotency-store"
#: 最多记多少条（按 LRU 淘汰）：一个人用不到这么多，但别让它无限长
DEFAULT_MAX_ENTRIES = 128


@dataclass
class _Entry:
    state: str = "inflight"           # inflight | done
    at: float = field(default_factory=time.monotonic)
    status: int = 200
    body: bytes = b""


class IdempotencyMiddleware:
    """给指定路径加"同一个 key 只干一次"。

    `paths` 是**精确路径**集合（不做前缀匹配：前缀会误伤 `/api/trip/text` 这种纯函数接口）。
    """

    def __init__(
        self,
        app: Any,
        *,
        paths: set[str],
        ttl: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.app = app
        self.paths = {str(path) for path in paths}
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._entries: dict[tuple[str, str], _Entry] = {}

    # ---------------------------------------------------------------- ASGI

    async def __call__(self, scope, receive, send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        key = self._key_of(scope)
        if not key:
            await self.app(scope, receive, send)
            return

        slot = (scope["path"], key)
        self._evict_expired()
        entry = self._entries.get(slot)
        if entry is not None:
            if entry.state == "inflight":
                await self._reply(
                    send,
                    409,
                    {"detail": "同一个请求正在处理中，请稍等再试。"},
                    extra_headers=[(b"retry-after", b"5"), (b"idempotent-replay", b"false")],
                )
                return
            await self._reply(
                send,
                entry.status,
                None,               # 原样回放字节，不重新序列化
                raw_body=entry.body,
                extra_headers=[(b"idempotent-replay", b"true")],
            )
            return

        entry = _Entry(state="inflight")
        self._entries[slot] = entry
        self._trim()

        captured = bytearray()
        status_holder: dict[str, int] = {}
        store_allowed = True

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal store_allowed
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                for name, value in message.get("headers") or []:
                    if name.lower() == NO_STORE_HEADER and value.strip().lower() in (b"no", b"0", b"false"):
                        store_allowed = False
            elif message["type"] == "http.response.body":
                captured.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            # 出错**或**被取消都要撤掉登记：否则这个 key 会一直卡在 inflight。
            # 评价9 P2 指出的正是这里 —— `except Exception` 抓不到 asyncio.CancelledError
            # （它继承 BaseException），于是被取消的请求会留下一条永不消失的 inflight。
            self._entries.pop(slot, None)
            raise

        status = status_holder.get("status", 500)
        if status == 200 and store_allowed:
            entry.state = "done"
            entry.status = status
            entry.body = bytes(captured)
        else:
            # 只缓存成功：失败要让调用方重试（否则一个临时故障会被缓存十分钟）
            self._entries.pop(slot, None)

    # -------------------------------------------------------------- 工具

    def _applies(self, scope) -> bool:
        return (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() == "POST"
            and str(scope.get("path", "")) in self.paths
        )

    def _key_of(self, scope) -> str:
        for name, value in scope.get("headers") or []:
            if name.decode("latin-1").lower() == "idempotency-key":
                return value.decode("latin-1").strip()[:128]
        return ""

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for slot, entry in list(self._entries.items()):
            # 已完成的按 ttl 过期；**inflight 也要有过期** ——
            # 否则一个被强行掐掉的请求会让同一个 key 一直 409（评价9 P2）
            limit = self.ttl if entry.state == "done" else INFLIGHT_TTL_SECONDS
            if now - entry.at > limit:
                self._entries.pop(slot, None)

    def _trim(self) -> None:
        if len(self._entries) <= self.max_entries:
            return
        # 第 156 步（评价11 P3-2）：**只淘汰已完成的条目**。
        # 以前"都还在处理中就把最老的扔了"，会把 inflight 逐出 ——
        # 于是同 key 的第二个请求不再拿到 409，而是被当成全新请求真实重放
        # （烧一次出行流水线 + 外部配额），恰好破坏了这个中间件存在的理由。
        done = [
            (slot, entry)
            for slot, entry in self._entries.items()
            if entry.state == "done"
        ]
        overflow = len(self._entries) - self.max_entries
        for slot, _entry in sorted(done, key=lambda item: item[1].at)[:overflow]:
            self._entries.pop(slot, None)

    async def _reply(
        self,
        send,
        status: int,
        payload: dict[str, Any] | None,
        *,
        raw_body: bytes | None = None,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = raw_body if raw_body is not None else json.dumps(
            payload or {}, ensure_ascii=False
        ).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8")]
        headers.extend(extra_headers or [])
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
