"""把食谱会话的 `recent_tags` 与忌口快照单独落库。

没有它，重启后「取消勾选就不再参考旧条件」这条规则会失效。
"""

from __future__ import annotations

import json
from typing import Any

from core.storage import MIGRATIONS, register_migration

SCHEMA_V5 = (
    """
    CREATE TABLE IF NOT EXISTS recipe_session_meta (
        session_id TEXT PRIMARY KEY,
        recent_tags TEXT NOT NULL,
        aversions_snapshot TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def register() -> None:
    """幂等登记 v5（由 `ensure_schema()` 显式调用）。"""
    if 5 not in MIGRATIONS:
        register_migration(SCHEMA_V5, version=5)


register()


def _load_json(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def load_meta(storage: Any, session_id: str) -> tuple[list[str], list[str]]:
    """返回 `(recent_tags, aversions_snapshot)`；没有记录时是空列表。"""
    if storage is None:
        return [], []
    row = storage.query_one(
        "SELECT recent_tags, aversions_snapshot FROM recipe_session_meta"
        " WHERE session_id = ?",
        (session_id,),
    )
    if row is None:
        return [], []
    return _load_json(row["recent_tags"]), _load_json(row["aversions_snapshot"])


def save_meta(
    storage: Any,
    session_id: str,
    recent_tags: list[str],
    aversions_snapshot: list[str],
) -> None:
    if storage is None:
        return
    from datetime import datetime, timezone

    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO recipe_session_meta"
            " (session_id, recent_tags, aversions_snapshot, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            "   recent_tags = excluded.recent_tags,"
            "   aversions_snapshot = excluded.aversions_snapshot,"
            "   updated_at = excluded.updated_at",
            (
                session_id,
                json.dumps(list(recent_tags), ensure_ascii=False),
                json.dumps(list(aversions_snapshot), ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def clear_meta(storage: Any, session_id: str) -> None:
    if storage is None:
        return
    with storage.transaction() as conn:
        conn.execute(
            "DELETE FROM recipe_session_meta WHERE session_id = ?", (session_id,)
        )
