"""会话记忆：有 storage 时以 SQLite 为唯一真相，没有时退回进程内字典。

喂给模型的条数（`max_messages`）与界面能翻到的条数（`MAX_ROWS_PER_SESSION`）是两个上限。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from core import history
from core.schemas import Message


@dataclass
class Session:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, max_messages: int = 40, storage: object | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._storage = storage
        # 保留的最近消息条数（喂给模型的上下文上限）
        self.max_messages = max_messages

    async def get_history(self, session_id: str) -> list[Message]:
        """返回该会话的历史消息（模型上下文用，最近 `max_messages` 条）。"""
        async with self._lock:
            if self._storage is not None:
                return history.load_messages(
                    self._storage, history.SCOPE_CHAT, session_id, self.max_messages
                )
            session = self._sessions.get(session_id)
            return list(session.messages) if session is not None else []

    async def history_for_display(
        self, session_id: str, limit: int = history.MAX_ROWS_PER_SESSION
    ) -> list[Message]:
        """界面要翻的历史：比上下文宽得多，但仍有上限。"""
        async with self._lock:
            if self._storage is not None:
                return history.load_messages(
                    self._storage, history.SCOPE_CHAT, session_id, limit
                )
            return list(self._sessions.get(session_id, Session(session_id)).messages)

    async def append(self, session_id: str, *messages: Message) -> None:
        async with self._lock:
            if self._storage is not None:
                history.append_messages(
                    self._storage, history.SCOPE_CHAT, session_id, messages
                )
                return
            session = self._sessions.setdefault(session_id, Session(session_id))
            session.messages.extend(messages)
            session.updated_at = time.time()
            # 只保留最近 N 条，防止上下文无限增长
            if len(session.messages) > self.max_messages:
                overflow = len(session.messages) - self.max_messages
                del session.messages[:overflow]

    async def reset(self, session_id: str) -> None:
        async with self._lock:
            if self._storage is not None:
                history.clear_messages(self._storage, history.SCOPE_CHAT, session_id)
                return
            self._sessions.pop(session_id, None)

    async def count(self, session_id: str) -> int:
        """该会话落库的消息条数（日志/测试用）。"""
        if self._storage is not None:
            return history.count_messages(
                self._storage, history.SCOPE_CHAT, session_id
            )
        return len(self._sessions.get(session_id, Session(session_id)).messages)
