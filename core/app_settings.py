"""应用级小配置的键值存储，目前只记用户上次选了哪个模型。

切换是运行时行为，不是部署配置，所以不写进 `.env`。
"""

from __future__ import annotations

from typing import Any

from core.storage import MIGRATIONS, register_migration

SCHEMA_V4 = (
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

#: 主动模型（提供方 name）。第 98 步
KEY_ACTIVE_PROVIDER = "active_provider"


def register() -> None:
    """幂等登记 v4（由 `ensure_schema()` 显式调用，见第 91 步的教训）。"""
    if 4 not in MIGRATIONS:
        register_migration(SCHEMA_V4, version=4)


register()


def get_setting(storage: Any, key: str, default: str = "") -> str:
    if storage is None:
        return default
    row = storage.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return str(row["value"]) if row is not None else default


def set_setting(storage: Any, key: str, value: str) -> None:
    if storage is None:
        return
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
