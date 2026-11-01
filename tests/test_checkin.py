"""签到纯逻辑：三种状态、日历网格偏移、连续天数、日期校验与翻月范围，以及落盘后重开仍在。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.checkin import (
    MAKEUP_CARDS_START,
    MIN_DATE,
    STATUS_DONE,
    STATUS_FUTURE,
    STATUS_TODO,
    CheckinArchive,
    CheckinError,
    NoMakeupCard,
    day_status,
    month_grid,
    month_view,
    parse_iso_date,
    parse_month,
    streak_of,
)
from core.storage import Storage

TODAY = date(2026, 3, 18)          # 2026-03-18 是周三


def _statuses(rows: list[dict[str, object]]) -> list[str | None]:
    return [row["status"] for row in rows]


# --------------------------------------------------------------- 建/取记录


def test_new_session_starts_empty():
    archive = CheckinArchive()
    assert archive.checked_days("s1") == set()
    assert archive.get("s1") is None


def test_sign_is_idempotent_and_reports_freshness():
    """同一天签两次不算错，但第二次要如实说"不是新签的"。"""
    archive = CheckinArchive()
    assert archive.sign("s1", date(2026, 3, 18)) is True
    assert archive.sign("s1", date(2026, 3, 18)) is False
    assert archive.checked_days("s1") == {"2026-03-18"}


def test_sessions_are_independent():
    archive = CheckinArchive()
    archive.sign("s1", date(2026, 3, 18))
    assert archive.checked_days("s2") == set()


def test_oldest_session_is_evicted_at_capacity():
    """会话 id 由前端生成，不清空的话进程会一直攒着——所以有上限。"""
    archive = CheckinArchive(max_sessions=2)
    archive.sign("s1", date(2026, 3, 1))
    archive.sign("s2", date(2026, 3, 2))
    archive.sign("s3", date(2026, 3, 3))
    assert archive.checked_days("s1") == set()
    assert archive.checked_days("s3") == {"2026-03-03"}


# --------------------------------------------------------------- 三种状态


def test_status_is_future_before_today_even_if_signed():
    """未来的日期一律是"还没到"——即便记录里真有那一天（数据脏了也不显示假信息）。"""
    checked = {"2026-03-25"}
    assert day_status(date(2026, 3, 25), today=TODAY, checked=checked) == STATUS_FUTURE


def test_status_is_done_or_todo_by_record():
    checked = {"2026-03-17"}
    assert day_status(date(2026, 3, 17), today=TODAY, checked=checked) == STATUS_DONE
    assert day_status(date(2026, 3, 16), today=TODAY, checked=checked) == STATUS_TODO
    assert day_status(TODAY, today=TODAY, checked=checked) == STATUS_TODO


# --------------------------------------------------------------- 日历网格


def test_grid_pads_to_the_right_weekday():
    """2026-03-01 是周日 → 周一起算要在前面补 6 格。"""
    rows = month_grid(date(2026, 3, 1), today=TODAY, checked=set())
    blanks = [row for row in rows if not row["date"]]
    assert len(blanks) == 6
    # 补的格子必须是"什么都没有"：没有状态、没有文案、不可点
    assert all(row["status"] is None and row["note"] == "" for row in blanks)
    assert all(row["clickable"] is False for row in blanks)
    assert rows[6]["date"] == "2026-03-01" and rows[6]["day"] == 1


def test_grid_has_one_row_per_day_of_month():
    rows = month_grid(date(2026, 3, 1), today=TODAY, checked=set())
    days = [row for row in rows if row["date"]]
    assert len(days) == 31
    assert days[0]["day"] == 1 and days[-1]["day"] == 31


def test_grid_month_starting_on_monday_needs_no_padding():
    """2026-06-01 是周一 → 一格都不用补（补格逻辑的正向对照）。"""
    rows = month_grid(date(2026, 6, 1), today=TODAY, checked=set())
    assert rows[0]["date"] == "2026-06-01"


def test_grid_leap_february_has_29_days():
    rows = month_grid(date(2024, 2, 1), today=date(2024, 2, 1), checked=set())
    assert len([row for row in rows if row["date"]]) == 29


def test_notes_come_from_the_status():
    checked = {"2026-03-17"}
    rows = month_grid(date(2026, 3, 1), today=TODAY, checked=checked)
    by_day = {row["day"]: row for row in rows if row["date"]}
    assert by_day[17]["note"] == "已签"
    assert by_day[16]["note"] == "未签"
    # 用户要求：还没到的日期不算（不写字）
    assert by_day[25]["note"] == ""
    assert by_day[25]["clickable"] is False
    assert by_day[17]["clickable"] is True
    assert by_day[16]["clickable"] is True


def test_past_flag_marks_the_days_that_need_a_makeup_card():
    """过去的日子 = 点它就是补签（要消耗道具卡）；今天与未来都不是补签。"""
    rows = month_grid(date(2026, 3, 1), today=TODAY, checked=set())
    by_day = {row["day"]: row for row in rows if row["date"]}
    assert by_day[17]["past"] is True
    assert by_day[18]["past"] is False         # 今天：点一下就是签到，不花道具
    assert by_day[25]["past"] is False         # 还没到
    # 月初的空白格也没有补签标记
    blanks = [row for row in rows if not row["date"]]
    assert all(row["past"] is False for row in blanks)


def test_today_is_marked():
    rows = month_grid(date(2026, 3, 1), today=TODAY, checked=set())
    today_rows = [row for row in rows if row["is_today"]]
    assert len(today_rows) == 1 and today_rows[0]["date"] == "2026-03-18"


# --------------------------------------------------------------- 连续天数


def test_streak_counts_back_from_today_when_signed():
    checked = {"2026-03-16", "2026-03-17", "2026-03-18"}
    assert streak_of(checked, today=TODAY) == 3


def test_streak_starts_from_yesterday_when_today_is_not_signed():
    """今天还没签不该立刻把之前的连续清零（晚上才想起来签也很正常）。"""
    checked = {"2026-03-16", "2026-03-17"}
    assert streak_of(checked, today=TODAY) == 2


def test_streak_stops_at_a_gap():
    checked = {"2026-03-15", "2026-03-17", "2026-03-18"}
    assert streak_of(checked, today=TODAY) == 2


def test_streak_is_zero_without_records():
    assert streak_of(set(), today=TODAY) == 0


def test_streak_ignores_future_records():
    """记录里混进未来日期（不该发生）也不该把连续天数算大。"""
    checked = {"2026-03-18", "2026-03-25", "2026-03-26"}
    assert streak_of(checked, today=TODAY) == 1


# --------------------------------------------------------------- 输入解析


@pytest.mark.parametrize("text", ["2026-03-18", "2024-02-29"])
def test_parse_iso_date_accepts_strict_iso(text: str):
    assert parse_iso_date(text).isoformat() == text


@pytest.mark.parametrize(
    "text",
    ["2026/03/18", "2026-3-8", "20260318", "", "今天", "2026-02-30"],
)
def test_parse_iso_date_rejects_loose_formats(text: str):
    """宽松解析会让"同一天"变成两个不同的键，同一天就能签两次。"""
    with pytest.raises(CheckinError):
        parse_iso_date(text)


def test_parse_month_falls_back_to_this_month():
    """翻月动作不该报错：格式不对就回落到本月，界面照常能用。"""
    assert parse_month("", today=TODAY) == date(2026, 3, 1)
    assert parse_month("不是月份", today=TODAY) == date(2026, 3, 1)
    assert parse_month("2026-13", today=TODAY) == date(2026, 3, 1)


def test_parse_month_accepts_valid_month():
    assert parse_month("2026-02", today=TODAY) == date(2026, 2, 1)


# --------------------------------------------------------------- 整月视图


def test_month_view_labels_and_grid():
    """视图里该有的：月份标题、星期、网格、连续天数、状态文案。

    刻意**不返回**"本月已签/未签多少天"与"今天日期"（用户第 2、3 条要求
    去掉界面上那两处），所以这里断言它们**不在**返回值里——将来谁又加回来，
    这条会先响。
    """
    checked = {"2026-03-16", "2026-03-17", "2026-03-18"}
    view = month_view(date(2026, 3, 1), today=TODAY, checked=checked)

    assert view["month"] == "2026-03"
    assert view["month_label"] == "2026 年 3 月"
    assert view["streak"] == 3
    assert view["status_labels"] == {"done": "已签", "todo": "未签"}
    assert view["weekdays"] == ["一", "二", "三", "四", "五", "六", "日"]
    assert view["leading"] == 6
    assert view["days"][6]["date"] == "2026-03-01"

    for removed in ("done_count", "todo_count", "total_count", "today"):
        assert removed not in view, f"{removed} 已按用户要求去掉，不该再返回"


def test_month_view_prev_next_are_neighbouring_months():
    view = month_view(date(2026, 3, 1), today=TODAY, checked=set())
    assert view["prev_month"] == "2026-02"
    assert view["next_month"] == "2026-04"


def test_month_view_can_page_past_today_up_to_2050():
    """用户第 5 条：支持**自由翻到 2050 年**（不再卡在"本月"）。

    未来月份可以看，只是未来的日子不给签到状态、不可点（前端按 status 渲染）。
    上限与农历表对齐（见 core/lunar.py），超过 2050-12 就停住。
    """
    view = month_view(date(2026, 3, 1), today=TODAY, checked=set())
    assert view["can_prev"] is True
    assert view["can_next"] is True               # 允许往后翻（原来是 False）

    future = month_view(date(2031, 7, 1), today=TODAY, checked=set())
    assert future["can_prev"] is True
    assert future["can_next"] is True

    last = month_view(date(2050, 12, 1), today=TODAY, checked=set())
    assert last["can_next"] is False, "2050-12 之后就翻不动了（农历表到头）"


def test_month_view_prev_stops_at_2020():
    """下限仍是 MIN_DATE（2020-01）：再往前没有数据，也没必要。"""
    floor = month_view(date(2020, 1, 1), today=TODAY, checked=set())
    assert floor["can_prev"] is False

    beyond = month_view(date(2019, 12, 1), today=TODAY, checked=set())
    assert beyond["can_prev"] is False


def test_month_view_flags_rest_and_work_days():
    """休 / 班 来自官方放假安排（core/holidays.py），逐日标出来。"""
    view = month_view(date(2026, 10, 1), today=date(2026, 10, 25), checked=set())
    by_day = {row["day"]: row for row in view["days"] if row["date"]}

    assert by_day[1]["rest"] == "off" and by_day[1]["rest_label"] == "休"
    assert by_day[7]["rest"] == "off", "国庆 10/1–10/7 都放假"
    assert by_day[8]["rest"] == "", "10/8 不是假期"
    assert by_day[10]["rest"] == "work" and by_day[10]["rest_label"] == "班", (
        "10/10 是调休补班日"
    )
    assert view["rest_labels"] == {"off": "休", "work": "班"}


def test_month_view_carries_lunar_and_festival():
    """农历与节日由 core/lunar.py 算，格子第二行显示节日、第三行显示农历。"""
    view = month_view(date(2026, 10, 1), today=date(2026, 10, 25), checked=set())
    by_day = {row["day"]: row for row in view["days"] if row["date"]}

    assert by_day[1]["festival"] == "国庆节"
    assert by_day[1]["lunar"] == "廿一", "2026-10-01 是农历八月廿一"
    assert by_day[10]["lunar"] == "九月", "初一显示月份名"
    assert by_day[25]["lunar"] == "十六"
    # 没有节日的那天 festival 为空，但农历仍在
    assert by_day[25]["festival"] == ""
    # 悬停提示里有农历全称，并且不重复写同一个节日
    assert "九月十六" in by_day[25]["calendar_tip"]
    assert by_day[1]["calendar_tip"].count("国庆节") == 1


def test_month_view_never_goes_before_min_date():
    """绝对下限：再早也没有意义（也挡住"传个 1900 年"这种请求）。"""
    checked = {"2019-05-05"}                        # 脏数据：早于 MIN_DATE
    view = month_view(date(2019, 6, 1), today=TODAY, checked=checked)
    assert view["can_prev"] is False
    assert MIN_DATE == date(2020, 1, 1)


# --------------------------------------------------- 补签道具卡（第 4 条要求）
# 用户原话：补签条件为需要消耗道具；道具后面再做，现在只做"补签时消耗道具卡，
# 没有则提醒'未持有补签卡'"。所以这里测的是**消耗**与**没有卡就别让签**。


def test_new_session_starts_with_no_makeup_cards():
    """还没做道具系统 → 一开始就没有卡，补签会被拒（而不是凭空发卡）。"""
    archive = CheckinArchive()
    assert archive.cards("s1") == 0
    assert MAKEUP_CARDS_START == 0
    with pytest.raises(NoMakeupCard) as excinfo:
        archive.spend_makeup_card("s1")
    assert str(excinfo.value) == "未持有补签卡"


def test_spending_a_card_decrements_and_is_per_session():
    archive = CheckinArchive()
    archive.set_cards("s1", 2)
    assert archive.spend_makeup_card("s1") == 1
    assert archive.spend_makeup_card("s1") == 0
    assert archive.cards("s1") == 0
    # 别的会话不受影响——卡是按会话记的
    archive.set_cards("s2", 1)
    assert archive.cards("s2") == 1
    with pytest.raises(NoMakeupCard):
        archive.spend_makeup_card("s1")


def test_cards_do_not_go_negative():
    archive = CheckinArchive()
    archive.set_cards("s1", 1)
    archive.spend_makeup_card("s1")
    for _ in range(3):                              # 反复越界也不该变成负数
        with pytest.raises(NoMakeupCard):
            archive.spend_makeup_card("s1")
    assert archive.cards("s1") == 0


# ------------------------------------------------ 落盘：重启之后记录还在
# 用户实测到"重启服务后签过的日期全变回未签"——这是本节的由来。
# 用真的 SQLite 文件（pytest 的 tmp_path），写完关掉连接再重新打开，
# 相当于"重启进程"，比断言 dict 有没有值可信得多。


def _open_persistent(path: Path | str) -> tuple[CheckinArchive, Storage]:
    from core.recipe_library import ensure_schema

    db = Storage(path)
    ensure_schema(db)               # 相当于启动时那一步：把库升到最新（含 v8）
    return CheckinArchive(storage=db), db


def test_persistent_archive_reports_its_mode(tmp_path):
    archive, db = _open_persistent(tmp_path / "pet.db")
    try:
        assert archive.persistent is True
        assert CheckinArchive().persistent is False
    finally:
        db.close()


def test_signed_days_survive_a_restart(tmp_path):
    path = tmp_path / "pet.db"
    archive, db = _open_persistent(path)
    archive.sign("s1", date(2026, 3, 16))
    archive.sign("s1", date(2026, 3, 17))
    db.close()                       # 关掉 = 进程结束

    restarted, db2 = _open_persistent(path)
    try:
        assert restarted.checked_days("s1") == {"2026-03-16", "2026-03-17"}
    finally:
        db2.close()


def test_cards_survive_a_restart(tmp_path):
    path = tmp_path / "pet.db"
    archive, db = _open_persistent(path)
    archive.set_cards("s1", 2)
    archive.spend_makeup_card("s1")
    db.close()

    restarted, db2 = _open_persistent(path)
    try:
        assert restarted.cards("s1") == 1
    finally:
        db2.close()


def test_signing_twice_in_persistent_mode_is_still_one_row(tmp_path):
    archive, db = _open_persistent(tmp_path / "pet.db")
    try:
        assert archive.sign("s1", date(2026, 3, 18)) is True
        assert archive.sign("s1", date(2026, 3, 18)) is False
        rows = db.query("SELECT day FROM checkin_days WHERE session_id = ?", ("s1",))
        assert [row["day"] for row in rows] == ["2026-03-18"]
    finally:
        db.close()


def test_persistent_mode_keeps_sessions_separate(tmp_path):
    archive, db = _open_persistent(tmp_path / "pet.db")
    try:
        archive.sign("alice", date(2026, 3, 18))
        assert archive.checked_days("alice") == {"2026-03-18"}
        assert archive.checked_days("bob") == set()
    finally:
        db.close()


def test_persistent_mode_has_no_in_memory_session(tmp_path):
    """持久模式下真值只在库里：`ensure()` 不该悄悄给一份"改了不落库"的对象。"""
    archive, db = _open_persistent(tmp_path / "pet.db")
    try:
        with pytest.raises(RuntimeError):
            archive.ensure("s1")
    finally:
        db.close()


def test_reset_and_clear_touch_the_database_too(tmp_path):
    archive, db = _open_persistent(tmp_path / "pet.db")
    try:
        archive.sign("s1", date(2026, 3, 18))
        archive.set_cards("s1", 3)
        assert archive.reset("s1") is True
        assert archive.checked_days("s1") == set()
        assert archive.cards("s1") == 0

        archive.sign("s1", date(2026, 3, 19))
        archive.clear()
        assert archive.checked_days("s1") == set()
    finally:
        db.close()


def test_a_writable_memory_db_still_loses_after_close():
    """库文件打不开时的降级是内存库：记录只在进程里（`open_storage()` 的语义）。

    这不是"持久化失败"，而是桌宠明确的取舍：能开就有用，数据重启即清空。
    把这条钉住，免得将来有人以为"传了 storage 就一定落盘"。
    """
    db = Storage.memory()
    from core.recipe_library import ensure_schema

    ensure_schema(db)
    archive = CheckinArchive(storage=db)
    archive.sign("s1", date(2026, 3, 18))
    assert archive.checked_days("s1") == {"2026-03-18"}
    assert str(db.path) == ":memory:"
    db.close()
