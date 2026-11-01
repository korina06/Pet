"""用量账本：回答「这一轮、这个功能、今天」各多少，所以断言都围绕能否正确加总。

约定：`turn_id` 把多步调用并成一轮、`feature` 分开 chat/recipe/probe、没有 usage 时记
`usage_source=missing` 而不是 0、`provider` 与 `model` 分开存。
"""

from __future__ import annotations

from core import usage
from core.recipe_library import ensure_schema
from core.storage import Storage


def _db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    return db


def _record(db, **overrides):
    base = dict(
        provider="deepseek",
        model="deepseek-chat",
        feature="chat",
        session_id="s1",
        turn_id="t1",
        step=1,
        stream=False,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        finish_reason="stop",
        latency_ms=800,
    )
    base.update(overrides)
    usage.record(db, **base)


def test_migration_adds_the_ledger_table() -> None:
    db = _db()

    assert db.user_version() >= 6
    columns = {row["name"] for row in db.query("PRAGMA table_info(llm_usage)")}
    assert {
        "turn_id", "session_id", "feature", "provider", "model",
        "step", "stream", "prompt_tokens", "completion_tokens", "total_tokens",
        "usage_source", "finish_reason", "raw_usage", "latency_ms",
    } <= columns


def test_rows_of_one_turn_can_be_added_up() -> None:
    """Agent 一轮 4 步 = 4 行；`turn_id` 让它们能加总成"这一轮"。"""
    db = _db()
    for step in (1, 2, 3, 4):
        _record(
            db,
            step=step,
            usage={"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050},
        )

    data = usage.summary(db)
    assert data["total"]["calls"] == 4
    assert data["total"]["total"] == 4200
    assert data["features"][0]["feature"] == "chat"


def test_features_and_providers_are_kept_apart() -> None:
    db = _db()
    _record(db, feature="chat", provider="deepseek", model="deepseek-chat")
    _record(db, feature="recipe", provider="deepseek", model="deepseek-chat")
    _record(db, feature="probe", provider="glm", model="glm-4-flash",
            usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})

    data = usage.summary(db)
    by_feature = {row["feature"]: row for row in data["features"]}

    assert set(by_feature) == {"chat", "recipe", "probe"}
    assert by_feature["probe"]["provider"] == "glm", "探活用的是另一家，不能混"
    # 只看一个功能时，别的功能不参与
    only_recipe = usage.summary(db, feature="recipe")
    assert [row["feature"] for row in only_recipe["features"]] == ["recipe"]
    assert only_recipe["total"]["calls"] == 1


def test_missing_usage_is_recorded_as_missing_not_zero() -> None:
    """没有 usage ≠ 花了 0。前者是"我们不知道"，后者会被当成免费。"""
    db = _db()
    _record(db, usage=None)

    row = usage.recent(db, 1)[0]
    assert row["usage_source"] == "missing"
    assert row["total"] is None

    data = usage.summary(db)
    assert data["total"]["missing"] == 1, "缺 usage 的调用要单独计数"
    assert data["total"]["total"] == 0, "但别把它算进 token 总数（那是 0 的语义）"


def test_recent_returns_newest_first_and_carries_finish_reason() -> None:
    db = _db()
    _record(db, finish_reason="stop", step=1)
    _record(db, finish_reason="length", step=2)

    rows = usage.recent(db, 10)

    assert rows[0]["step"] == 2, "最新的在最前面"
    assert rows[0]["finish_reason"] == "length", "被截断的调用要看得出来"
    assert rows[1]["finish_reason"] == "stop"


def test_turn_token_counter_accumulates_and_resets() -> None:
    """轮级预算要用这个计数器，所以它必须能加、能清零。"""
    usage.reset_turn_tokens()
    assert usage.turn_tokens() == 0

    usage.add_turn_tokens(1200)
    usage.add_turn_tokens(800)
    assert usage.turn_tokens() == 2000

    usage.reset_turn_tokens()
    assert usage.turn_tokens() == 0, "下一轮必须从 0 开始（否则预算会被上一轮吃光）"


def test_record_also_feeds_the_turn_counter() -> None:
    db = _db()
    usage.reset_turn_tokens()
    _record(db, usage={"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330})

    assert usage.turn_tokens() == 330, "记一次账就该计入本轮用量"


def test_a_missing_usage_row_does_not_feed_the_counter() -> None:
    """缺 usage 的调用不能参与预算判断 —— 否则会算成 0 而放行。"""
    db = _db()
    usage.reset_turn_tokens()
    _record(db, usage=None)

    assert usage.turn_tokens() == 0


def test_daily_warning_fires_once_and_only_warns() -> None:
    """日用量超阈值时记一条 WARNING，并且**不拦截**（第 112 步）。

    只告警不拒绝是刻意的：个人单用户，误伤正常对话的代价比多花几毛钱大。
    节流也是必需的 —— 否则每条请求都会刷一行。
    """
    import logging

    db = _db()
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                records.append(record.getMessage())

    logger = logging.getLogger("core.usage")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        _record(db, usage={"prompt_tokens": 60, "completion_tokens": 0, "total_tokens": 60},
                warn_at=100)
        assert not records, "还没超阈值就不该喊"

        _record(db, usage={"prompt_tokens": 60, "completion_tokens": 0, "total_tokens": 60},
                warn_at=100)
        assert len(records) == 1, records
        assert "告警阈值" in records[0]

        # 节流：同一个阈值再记一次也不重复喊
        _record(db, usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
                warn_at=100)
        assert len(records) == 1, "同一小时内只喊一次"
    finally:
        logger.removeHandler(handler)
        usage._last_daily_warn.clear()


def test_daily_warning_is_off_by_default() -> None:
    db = _db()
    _record(db, usage={"prompt_tokens": 999999, "completion_tokens": 0,
                       "total_tokens": 999999}, warn_at=0)
    assert usage.daily_total(db) == 999999, "阈值 0 = 关闭，但用量照样记"


def test_the_day_boundary_is_local_midnight_not_utc() -> None:
    """"今日"要按**本地**零点切（第 114 步 / 评价6 第 3 条）。

    UTC 零点在东八区是早上 8 点 —— 账单会"一早上才换天"，那不像"今日"。
    """
    from datetime import datetime

    from core.usage import day_start_utc

    start = day_start_utc(0)
    local = datetime.now().astimezone()
    expected = local.replace(hour=0, minute=0, second=0, microsecond=0)

    parsed = datetime.fromisoformat(start)
    assert abs((parsed - expected).total_seconds()) < 2, (
        f"本地零点应当是 {expected.isoformat()}，实际 {start}"
    )


def test_yesterday_starts_one_local_day_before_today() -> None:
    from datetime import datetime

    from core.usage import day_start_utc

    today = datetime.fromisoformat(day_start_utc(0))
    yesterday = datetime.fromisoformat(day_start_utc(1))

    assert (today - yesterday).total_seconds() == 86400


# ------------------------------------------------------- 分布视图（第 210 步）
# 定预算要的是「最坏能到多少」，不是合计。所以这一组钉的是三个口径的分位数，
# 而且**不插值** —— 样本只有几十次时，插值出来的 P95 是个不存在的数。


def test_percentile_takes_a_real_sample_not_an_interpolation() -> None:
    """最近秩：P50/P95 必须命中真实用过的值。"""
    values = [10, 20, 30, 40, 50]
    assert usage._percentile(values, 0.5) == 30
    assert usage._percentile(values, 0.95) == 50
    assert usage._percentile(values, 0.0) == 10
    assert usage._percentile([], 0.95) == 0


def test_stats_separate_the_three_scopes() -> None:
    """单次 / 单轮 / 单日三个口径各算各的——它们对应三个不同的预算。"""
    db = _db()
    # 一轮两步：1000 + 1000 → 单轮 2000，单次 1000
    _record(db, turn_id="t1", step=1,
            usage={"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000})
    _record(db, turn_id="t1", step=2,
            usage={"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000})
    # 另一轮一步：3000
    _record(db, turn_id="t2", step=1,
            usage={"prompt_tokens": 2500, "completion_tokens": 500, "total_tokens": 3000})

    data = usage.stats(db, days=7)

    assert data["calls"] == 3
    assert data["turns"] == 2
    assert data["per_call"]["max"] == 3000, "单次口径不该把两步合成一步"
    assert data["per_turn"]["max"] == 3000
    assert data["per_call"]["p50"] == 1000
    # 2000 + 3000 = 5000 全落在今天这一天
    assert data["max_day"] == 5000
    assert len(data["per_day"]) == 1
    assert data["prompt_share"] == 0.86, "prompt 占 4300/5000"


def test_stats_does_not_crash_on_an_empty_ledger() -> None:
    db = _db()

    data = usage.stats(db, days=7)

    assert data["calls"] == 0
    assert data["per_call"]["n"] == 0 and data["per_turn"]["n"] == 0
    assert data["per_day"] == [] and data["max_day"] == 0


def test_stats_counts_a_day_by_local_date() -> None:
    """按**本地**日期切天 —— 与 `daily_total()` 用同一个日界，否则两处会对不上。"""
    from datetime import datetime, timedelta, timezone

    db = _db()
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 昨天中午（本地）：只属于昨天那一天
    yesterday_noon = (local_midnight - timedelta(days=1) + timedelta(hours=12))
    _record(db, turn_id="y", usage={"prompt_tokens": 500, "completion_tokens": 0,
                                    "total_tokens": 500})
    with db.transaction() as conn:
        conn.execute(
            "UPDATE llm_usage SET created_at = ? WHERE turn_id = 'y'",
            (yesterday_noon.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        )
    _record(db, turn_id="t", usage={"prompt_tokens": 700, "completion_tokens": 0,
                                    "total_tokens": 700})

    data = usage.stats(db, days=2)

    dates = {row["date"]: row["total"] for row in data["per_day"]}
    assert dates[yesterday_noon.date().isoformat()] == 500
    assert dates[local_midnight.date().isoformat()] == 700
