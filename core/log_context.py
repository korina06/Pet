"""给日志与用量账本带上当前的功能、会话、轮次与步骤，用 contextvar 传递而不层层传参。
"""

from __future__ import annotations

from contextvars import ContextVar

_feature: ContextVar[str] = ContextVar("log_feature", default="?")
_session_short: ContextVar[str] = ContextVar("log_session_short", default="")
_session_full: ContextVar[str] = ContextVar("log_session_full", default="")
_turn: ContextVar[str] = ContextVar("log_turn", default="")
_request_id: ContextVar[str] = ContextVar("log_request", default="")
_prompt_version: ContextVar[str] = ContextVar("log_prompt_version", default="")
_step: ContextVar[int] = ContextVar("log_step", default=0)

#: 日志里的会话 id 只留前 8 位：够对上号，又不把整串写进日志
SESSION_PREFIX = 8


def set_context(feature: str, session_id: str = "", turn_id: str = "") -> None:
    """在请求入口调用一次，之后同一条链路里的日志与账本都会带上它。"""
    _feature.set(str(feature or "?"))
    _session_short.set(str(session_id or "")[:SESSION_PREFIX])
    _session_full.set(str(session_id or ""))
    _turn.set(str(turn_id or ""))


def set_step(step: int) -> None:
    """Agent 循环里每步开头调一次 —— 账本里就能看到"第几步用了多少"。"""
    _step.set(int(step or 0))


def current() -> tuple[str, str]:
    """`(feature, session 前缀)`；没设置过时是 `("?", "")`。给日志用。"""
    return _feature.get(), _session_short.get()


def full_session() -> str:
    """完整会话 id。给账本用（账本是本地库，按会话聚合需要完整 id）。"""
    return _session_full.get()


def turn_id() -> str:
    return _turn.get()


def set_request_id(request_id: str) -> None:
    """由 HTTP 中间件在每个请求入口设置（第 119 步）。

    它是**串起一条请求**的那根线：日志、工具 span、账本都能带它，
    于是"这次为什么慢/为什么失败"可以从一个 id 追到底。
    """
    _request_id.set(str(request_id or ""))


def request_id() -> str:
    return _request_id.get()


def set_prompt_version(version: str) -> None:
    """这一轮用的是哪个版本的提示词（第 123 步）。

    由 Agent 在每轮开始时设置 —— 它才知道自己用的是哪段 system prompt。
    """
    _prompt_version.set(str(version or ""))


def prompt_version() -> str:
    return _prompt_version.get()


def step() -> int:
    return int(_step.get())
