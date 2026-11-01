"""请求与工具调用的观测缓冲：内存里留最近若干条工具 span，回答「刚才那次为什么慢」。

环形缓冲、重启即清空；长期证据在用量账本里，工具调用不落库。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

#: 各留 200 条：够定位"最近发生了什么"，又不会无限长
MAX_SPANS = 200
MAX_REQUESTS = 200

_tools: deque[dict[str, Any]] = deque(maxlen=MAX_SPANS)
_requests: deque[dict[str, Any]] = deque(maxlen=MAX_REQUESTS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ToolSpan:
    name: str
    ok: bool
    ms: int
    error: str = ""
    request_id: str = ""
    feature: str = ""
    session: str = ""
    turn: str = ""
    step: int = 0
    at: str = field(default_factory=_now)


@dataclass
class RequestSummary:
    request_id: str
    method: str
    path: str
    status: int
    ms: int
    feature: str = ""
    session: str = ""
    turn: str = ""
    at: str = field(default_factory=_now)


def record_tool(
    *,
    name: str,
    ok: bool,
    ms: int,
    error: str = "",
    request_id: str = "",
    feature: str = "",
    session: str = "",
    turn: str = "",
    step: int = 0,
) -> None:
    _tools.append(
        asdict(
            ToolSpan(
                name=name,
                ok=ok,
                ms=ms,
                error=error,
                request_id=request_id,
                feature=feature,
                session=session,
                turn=turn,
                step=step,
            )
        )
    )


def record_request(
    *,
    request_id: str,
    method: str,
    path: str,
    status: int,
    ms: int,
    feature: str = "",
    session: str = "",
    turn: str = "",
) -> None:
    _requests.append(
        asdict(
            RequestSummary(
                request_id=request_id,
                method=method,
                path=path,
                status=status,
                ms=ms,
                feature=feature,
                session=session,
                turn=turn,
            )
        )
    )


def recent_tools(limit: int = 50) -> list[dict[str, Any]]:
    """最近的工具调用，**最新的在前**。"""
    items = list(_tools)[-max(1, limit):]
    return list(reversed(items))


def recent_requests(limit: int = 50) -> list[dict[str, Any]]:
    items = list(_requests)[-max(1, limit):]
    return list(reversed(items))


def slow_tools(threshold_ms: int = 1000, limit: int = 20) -> list[dict[str, Any]]:
    """慢工具：排障时第一眼要看的东西。"""
    picked = [span for span in _tools if int(span["ms"]) >= threshold_ms]
    picked.sort(key=lambda span: int(span["ms"]), reverse=True)
    return picked[:limit]


def failed_tools(limit: int = 20) -> list[dict[str, Any]]:
    picked = [span for span in _tools if not span["ok"]]
    return list(reversed(picked))[:limit]


def clear() -> None:
    """清空（测试用）。"""
    _tools.clear()
    _requests.clear()


#: 路径前缀 -> 功能名。**为什么不用 `request.state` 或 contextvar**：
#: 路由跑在中间件的**子任务**里，它改的 contextvar / state 传不回中间件
#: （Starlette 1.6 实测）。而"这个请求属于哪个功能"其实从路径就能确定 ——
#: 用一张显式的表，比依赖执行顺序可靠，也能单独测。
_FEATURE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("/api/recipe/", "recipe"),
    ("/api/chat", "chat"),
    ("/api/model/probe", "probe"),
    ("/api/model", "model"),
    ("/api/trip", "travel"),
    ("/api/checkin", "checkin"),
    ("/api/mail", "mail"),
    ("/api/usage", "usage"),
    ("/api/diagnostics", "diagnostics"),
    ("/api/session", "chat"),
)


def feature_for_path(path: str) -> str:
    """把请求路径映射到功能名（认不出来就返回空串）。"""
    target = str(path or "")
    for prefix, feature in _FEATURE_BY_PREFIX:
        if target.startswith(prefix):
            return feature
    return ""


def monotonic_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
