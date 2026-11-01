"""聊天与食谱的消息落库：一张表按 scope 区分，只存用户提问与助手最终回答。

有 storage 时读也读库、写也写库，内存里不留副本；显示上限与喂给模型的上限是两件事。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from core.schemas import Message
from core.storage import MIGRATIONS, register_migration

logger = logging.getLogger(__name__)

#: 会话归属：聊天窗口 / 食谱窗口
SCOPE_CHAT = "chat"
SCOPE_RECIPE = "recipe"

#: 每个会话在库里最多留多少行（= 界面能翻到的上限）。
#: **不是**上下文上限，见模块说明。
MAX_ROWS_PER_SESSION = 200

SCHEMA_V3 = (
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (scope, session_id, id)
    """,
)

def register() -> None:
    """把 v3 登记进迁移表（**幂等**）。

    为什么要有这个函数，而不是只在 import 时登记一次：
    迁移表靠 import 触发器登记，于是"注册表里有没有 v3"取决于谁先 import 过。
    实测踩过：只导入 `core.recipe_library` 而没导入本模块时，真库停在 v2、
    `chat_messages` 根本没建出来，写消息直接 `no such table`（启动日志一句异常都没有）。

    所以 `ensure_schema()` 会**显式**调它。幂等，重复调用无害。
    """
    if 3 not in MIGRATIONS:
        register_migration(SCHEMA_V3, version=3)


#: 只加不删：已发布的迁移不能改形状，否则老库（user_version 已到 2 的）不会重跑。
register()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_messages(
    storage: Any,
    scope: str,
    session_id: str,
    messages: Sequence[Message],
) -> None:
    """追加若干条消息，并把该会话修剪到 `MAX_ROWS_PER_SESSION` 行以内。"""
    if storage is None or not messages:
        return
    stamp = _now()
    with storage.transaction() as conn:
        for message in messages:
            conn.execute(
                "INSERT INTO chat_messages"
                " (scope, session_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    scope,
                    session_id,
                    str(getattr(message, "role", "") or ""),
                    str(getattr(message, "content", "") or ""),
                    stamp,
                ),
            )
        # 只留最近 N 行：按 id（插入顺序）倒排
        conn.execute(
            "DELETE FROM chat_messages"
            " WHERE scope = ? AND session_id = ? AND id NOT IN ("
            "   SELECT id FROM chat_messages WHERE scope = ? AND session_id = ?"
            "   ORDER BY id DESC LIMIT ?"
            " )",
            (scope, session_id, scope, session_id, MAX_ROWS_PER_SESSION),
        )


def load_messages(
    storage: Any,
    scope: str,
    session_id: str,
    limit: int,
) -> list[Message]:
    """取该会话最近 `limit` 条，按时间正序返回（最老的在前）。"""
    if storage is None or limit <= 0:
        return []
    rows = storage.query(
        "SELECT role, content FROM chat_messages"
        " WHERE scope = ? AND session_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (scope, session_id, int(limit)),
    )
    return [
        Message(role=str(row["role"]), content=str(row["content"]))
        for row in reversed(rows)
    ]


def clear_messages(storage: Any, scope: str, session_id: str) -> int:
    """删掉该会话的全部消息，返回删除的行数。"""
    if storage is None:
        return 0
    with storage.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM chat_messages WHERE scope = ? AND session_id = ?",
            (scope, session_id),
        )
        return int(cursor.rowcount or 0)


def count_messages(storage: Any, scope: str, session_id: str) -> int:
    if storage is None:
        return 0
    row = storage.query_one(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE scope = ? AND session_id = ?",
        (scope, session_id),
    )
    return int(row["n"]) if row is not None else 0
