"""模型调用的用量账本：每次调用一行，可按轮次、功能、提供方加总。

没有 usage 时记 missing 而不是 0；只记数字，不存对话正文。
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from core.storage import MIGRATIONS, register_migration

SCHEMA_V6 = (
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        step INTEGER NOT NULL,
        stream INTEGER NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        usage_source TEXT NOT NULL,
        finish_reason TEXT,
        raw_usage TEXT,
        latency_ms INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_turn ON llm_usage (turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage (created_at)",
)

#: 第 123 步：把"这一轮用的是哪个版本的提示词"记进账本。
#: 用 ALTER 而不是重建表：账本里已经有真实调用记录，不能为了加一列把它们清掉。
def _add_prompt_version(conn: Any) -> None:
    """给账本加 `prompt_version` 列（幂等）。

    为什么不是一句 `ALTER TABLE`：SQLite 没有 `ADD COLUMN IF NOT EXISTS`。
    而"把 user_version 退回再重跑迁移"是这个项目**真有的**测试场景
    （`test_old_v2_table_shape_still_works`），一句裸 ALTER 会在那里撞
    `duplicate column name`。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    if "prompt_version" not in columns:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN prompt_version TEXT")


SCHEMA_V7: tuple[Any, ...] = (_add_prompt_version,)

#: 当前这一轮已经用掉的 token（按请求隔离，`contextvars` 在 asyncio 里是任务级的）
_turn_used: ContextVar[dict[str, int] | None] = ContextVar("turn_used", default=None)


def register() -> None:
    """幂等登记 v6（由 `ensure_schema()` 显式调用）。"""
    if 6 not in MIGRATIONS:
        register_migration(SCHEMA_V6, version=6)
    if 7 not in MIGRATIONS:
        register_migration(SCHEMA_V7, version=7)


register()


# ------------------------------------------------------------ 轮级累计


def reset_turn_tokens() -> None:
    _turn_used.set({"total": 0})


def add_turn_tokens(count: int) -> int:
    """把这次调用的 token 计入本轮，返回累计值。"""
    holder = _turn_used.get()
    if holder is None:
        holder = {"total": 0}
        _turn_used.set(holder)
    holder["total"] += max(0, int(count or 0))
    return holder["total"]


def turn_tokens() -> int:
    holder = _turn_used.get()
    return int(holder["total"]) if holder else 0


# ------------------------------------------------------------ 写入


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def day_start_utc(days_ago: int = 0) -> str:
    """本地时区里"往前第 N 天"的零点，换算成 UTC ISO（与库里的 created_at 对齐）。

    为什么不用 UTC 零点：这是**单机自用**的应用，界面写「今日」就该是**用户今天**。
    UTC 零点在东八区是早上 8 点 —— 账单会"一早上才换天"，那不是「今日」的意思
    （评价6 第 3 条）。三处（账本、接口、CLI）都用这一个 helper，避免各算各的。
    """
    local_now = datetime.now().astimezone()
    local_start = (local_now - timedelta(days=int(days_ago or 0))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_start.astimezone(timezone.utc).isoformat(timespec="seconds")


def daily_total(storage: Any, *, feature: str | None = None) -> int:
    """今天（**本地**日界）的 token 总量。`feature` 为空表示全部。"""
    if storage is None:
        return 0
    since = day_start_utc(0)
    clause = " WHERE created_at >= ?"
    params: list[Any] = [since]
    if feature:
        clause += " AND feature = ?"
        params.append(feature)
    row = storage.query_one(
        f"SELECT COALESCE(SUM(total_tokens), 0) AS total FROM llm_usage{clause}",
        tuple(params),
    )
    return int(row["total"] or 0) if row is not None else 0


#: 日告警的节流：同一个阈值每小时最多喊一次，否则每条请求都会刷一行
_last_daily_warn: dict[int, float] = {}
_DAILY_WARN_INTERVAL = 3600.0


def warn_if_over_daily(storage: Any, threshold: int) -> bool:
    """超过日阈值就记一条 WARNING（**只告警，不拒绝**）。返回是否真的喊了。

    为什么只告警：个人单用户，误伤正常对话的代价比多花几毛钱大。
    阈值默认 0（关闭），跑一周按 P95×2 再定 —— 那时候数字才有依据。
    """
    if storage is None or threshold <= 0:
        return False
    now = datetime.now(timezone.utc).timestamp()
    last = _last_daily_warn.get(threshold)
    if last is not None and now - last < _DAILY_WARN_INTERVAL:
        return False
    used = daily_total(storage)
    if used < threshold:
        return False
    _last_daily_warn[threshold] = now
    import logging

    logging.getLogger(__name__).warning(
        "今日用量 %s token 已超过告警阈值 %s（只提醒，不拦截）", used, threshold
    )
    return True


def record(
    storage: Any,
    *,
    provider: str,
    model: str,
    feature: str,
    session_id: str,
    turn_id: str,
    step: int,
    stream: bool,
    usage: dict[str, Any] | None,
    finish_reason: str | None = None,
    latency_ms: int | None = None,
    warn_at: int = 0,
    prompt_version: str = "",
) -> None:
    """记一次模型调用。`usage` 为 None/空时记 `usage_source=missing`。"""
    payload = usage if isinstance(usage, dict) else {}
    has_usage = bool(payload)
    prompt = payload.get("prompt_tokens") if has_usage else None
    completion = payload.get("completion_tokens") if has_usage else None
    total = payload.get("total_tokens") if has_usage else None

    # 轮级累计只用真实数字：缺 usage 的调用不参与预算判断（否则会算成 0 而放行）
    if has_usage and isinstance(total, int):
        add_turn_tokens(total)

    if storage is None:
        return
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO llm_usage (created_at, turn_id, session_id, feature, provider,"
            " model, step, stream, prompt_tokens, completion_tokens, total_tokens,"
            " usage_source, finish_reason, raw_usage, latency_ms, prompt_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                turn_id or "",
                session_id or "",
                feature or "?",
                provider or "",
                model or "",
                int(step or 0),
                1 if stream else 0,
                prompt,
                completion,
                total,
                "api" if has_usage else "missing",
                finish_reason,
                json.dumps(payload, ensure_ascii=False) if has_usage else None,
                latency_ms,
                prompt_version or "",
            ),
        )
    if warn_at:
        warn_if_over_daily(storage, warn_at)


# ------------------------------------------------------------ 查询


def summary(
    storage: Any,
    *,
    since: str | None = None,
    feature: str | None = None,
    prompt_version: str | None = None,
) -> dict:
    """按功能汇总。`since` 是 ISO 时间字符串（含）；其余为等值过滤。"""
    if storage is None:
        return {"since": since, "features": [], "total": {}}
    where = []
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if feature:
        where.append("feature = ?")
        params.append(feature)
    if prompt_version:
        where.append("prompt_version = ?")
        params.append(prompt_version)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    rows = storage.query(
        "SELECT feature, provider, model, COUNT(*) AS calls,"
        " SUM(COALESCE(prompt_tokens, 0)) AS prompt,"
        " SUM(COALESCE(completion_tokens, 0)) AS completion,"
        " SUM(COALESCE(total_tokens, 0)) AS total,"
        " SUM(CASE WHEN usage_source = 'missing' THEN 1 ELSE 0 END) AS missing"
        f" FROM llm_usage{clause}"
        " GROUP BY feature, provider, model ORDER BY total DESC",
        tuple(params),
    )
    features = [
        {
            "feature": row["feature"],
            "provider": row["provider"],
            "model": row["model"],
            "calls": int(row["calls"] or 0),
            "prompt": int(row["prompt"] or 0),
            "completion": int(row["completion"] or 0),
            "total": int(row["total"] or 0),
            "missing": int(row["missing"] or 0),
        }
        for row in rows
    ]
    total = {
        "calls": sum(item["calls"] for item in features),
        "prompt": sum(item["prompt"] for item in features),
        "completion": sum(item["completion"] for item in features),
        "total": sum(item["total"] for item in features),
        "missing": sum(item["missing"] for item in features),
    }
    return {"since": since, "features": features, "total": total}


def _percentile(values: list[int], q: float) -> int:
    """最近秩百分位（不插值）。样本很少时也不给出"中间没有的数"。

    为什么不用线性插值：我们的样本是几十次真实调用，插值出来的 p95 会是
    "两个真实值之间的假数"，写进文档反而像编的。最近秩永远取到真实样本之一。
    """
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    index = int(round((len(ordered) - 1) * float(q)))
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


def _distribution(values: list[int]) -> dict[str, int]:
    if not values:
        return {"n": 0, "min": 0, "p50": 0, "p90": 0, "p95": 0, "max": 0, "mean": 0}
    return {
        "n": len(values),
        "min": min(values),
        "p50": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "p95": _percentile(values, 0.95),
        "max": max(values),
        "mean": int(round(sum(values) / len(values))),
    }


def stats(storage: Any, *, days: int = 7, feature: str | None = None) -> dict:
    """账本的**分布**视图 —— 定 `turn_token_budget` / `daily_token_warn` 就靠它。

    为什么需要它：`summary()` 只给合计，而预算要的是"最坏能到多少"。合计 20 万
    token 说明不了任何阈值该定多少；单轮 P95 = 8K、最大 11.8K 才说明。

    三个口径分开算，因为它们对应三个不同的预算：
    - `per_call`：单次模型调用 —— 对应 `max_tokens`（单次输出）；
    - `per_turn`：一次用户提问的累计（`turn_id` 分组）—— 对应 `turn_token_budget`；
    - `per_day`：自然日合计（**本地**日界，与告警一致）—— 对应 `daily_token_warn`。
    """
    empty = {
        "days": int(days or 0),
        "feature": feature,
        "since": None,
        "calls": 0,
        "prompt_share": 0.0,
        "per_call": _distribution([]),
        "per_turn": _distribution([]),
        "per_day": [],
        "turns": 0,
        "max_day": 0,
        "max_day_date": "",
    }
    if storage is None or days <= 0:
        return empty

    since = day_start_utc(int(days) - 1)
    where = "created_at >= ?"
    params: list[Any] = [since]
    if feature:
        where += " AND feature = ?"
        params.append(feature)

    rows = storage.query(
        "SELECT created_at, turn_id, prompt_tokens, completion_tokens, total_tokens"
        f" FROM llm_usage WHERE {where}",
        tuple(params),
    )
    if not rows:
        result = dict(empty)
        result["since"] = since
        return result

    per_call = [int(row["total_tokens"] or 0) for row in rows]
    prompt = sum(int(row["prompt_tokens"] or 0) for row in rows)
    completion = sum(int(row["completion_tokens"] or 0) for row in rows)
    total = prompt + completion

    by_turn: dict[str, int] = {}
    by_day: dict[str, int] = {}
    day_calls: dict[str, int] = {}
    for row in rows:
        turn = str(row["turn_id"] or "")
        if turn:
            by_turn[turn] = by_turn.get(turn, 0) + int(row["total_tokens"] or 0)
        # created_at 存的是 UTC；按**本地**日期切天，否则早上 8 点才换天（评价6 第 3 条）
        local = datetime.fromisoformat(str(row["created_at"])).astimezone()
        day = local.date().isoformat()
        by_day[day] = by_day.get(day, 0) + int(row["total_tokens"] or 0)
        day_calls[day] = day_calls.get(day, 0) + 1

    per_day = [
        {"date": day, "total": by_day[day], "calls": day_calls[day]}
        for day in sorted(by_day)
    ]
    busiest = max(per_day, key=lambda item: item["total"])

    return {
        "days": int(days),
        "feature": feature,
        "since": since,
        "calls": len(rows),
        "prompt_share": round(prompt / total, 3) if total else 0.0,
        "per_call": _distribution(per_call),
        "per_turn": _distribution(list(by_turn.values())),
        "per_day": per_day,
        "turns": len(by_turn),
        "max_day": busiest["total"],
        "max_day_date": busiest["date"],
    }


def recent(storage: Any, limit: int = 20) -> list[dict]:
    if storage is None:
        return []
    rows = storage.query(
        "SELECT created_at, turn_id, feature, provider, model, step, stream,"
        " prompt_tokens, completion_tokens, total_tokens, usage_source, finish_reason,"
        " latency_ms, prompt_version FROM llm_usage ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
    return [
        {
            "created_at": row["created_at"],
            "turn_id": row["turn_id"],
            "feature": row["feature"],
            "provider": row["provider"],
            "model": row["model"],
            "step": int(row["step"] or 0),
            "stream": bool(row["stream"]),
            "prompt": row["prompt_tokens"],
            "completion": row["completion_tokens"],
            "total": row["total_tokens"],
            "usage_source": row["usage_source"],
            "finish_reason": row["finish_reason"],
            "latency_ms": row["latency_ms"],
            # 第 123 步：这一行是哪版提示词跑出来的
            "prompt_version": row["prompt_version"],
        }
        for row in rows
    ]
