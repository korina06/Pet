"""农历、节气与放假调休的锚点核对：只钉有官方出处的几个日期，不追求逐年自洽。
"""

from __future__ import annotations

from datetime import date, timedelta

from core import holidays
from core.lunar import (
    LUNAR_YEARS,
    festivals_of,
    lunar_label,
    lunar_of,
    solar_term_qingming,
)

#: 农历锚点：(公历日期, (农历年, 月, 日))。
#: 前四条来自国务院办公厅 2026 年放假通知原文（"2 月 15 日（农历腊月二十八）"、
#: "2 月 23 日（农历正月初七）"），其余是众所周知的春节/中秋日期。
LUNAR_ANCHORS = {
    date(2019, 2, 5): (2019, 1, 1),      # 2019 春节（也是本表起点）
    date(2024, 2, 10): (2024, 1, 1),     # 2024 春节
    date(2025, 1, 29): (2025, 1, 1),     # 2025 春节
    date(2026, 2, 17): (2026, 1, 1),     # 2026 春节
    date(2026, 2, 15): (2025, 12, 28),   # 官方原文：腊月二十八
    date(2026, 2, 23): (2026, 1, 7),     # 官方原文：正月初七
    date(2020, 10, 1): (2020, 8, 15),    # 2020 国庆与中秋同日
    date(2025, 10, 6): (2025, 8, 15),    # 2025 中秋
    date(2026, 9, 25): (2026, 8, 15),    # 2026 中秋
}


def test_lunar_anchors_match_official_dates():
    for solar, (year, month, day) in LUNAR_ANCHORS.items():
        value = lunar_of(solar)
        assert value is not None, f"{solar} 应该能算出农历"
        assert (value.year, value.month, value.day) == (year, month, day), (
            f"{solar} 应为农历 {year}-{month}-{day}，实际 {value.year}-{value.month}-{value.day}"
        )


def test_lunar_year_table_covers_the_calendar_range():
    """农历表必须**逐年**覆盖 2019–2050（第 147 步 / 评价10 P2）。

    原来这条是**假覆盖**：

    ```python
    for year in range(2019, 2051):
        assert lunar_of(EPOCH).year == 2019     # year 根本没被用到
    ```

    循环跑了 32 次，每次都断言同一个 2019 —— 就算 2020–2050 的表项全错，它也全绿。
    现在逐项验：年份集合完整、每一年都真的有正月初一、每个农历月 29 或 30 天。
    """
    assert sorted(LUNAR_YEARS) == list(range(2019, 2051)), "农历表的年份集合不连续"


def test_every_lunar_year_has_a_new_year_day():
    """逐年找正月初一：找不到就说明那一年的表项坏了。"""
    for year in range(2019, 2051):
        hit = None
        for offset in range(32):                      # 春节一定落在 1/20 – 2/20 之间
            day = date(year, 1, 20) + timedelta(days=offset)
            value = lunar_of(day)
            if value is not None and (value.year, value.month, value.day) == (year, 1, 1):
                hit = day
                break
        assert hit is not None, f"农历 {year} 年找不到正月初一"


def test_every_lunar_month_is_29_or_30_days():
    """按月统计长度：只允许 29 / 30，且闰月最多一个。

    这一条比锚点密度高 —— 表里任何一个月的天数写错，这里都会红。
    """
    lengths: dict[tuple[int, int, bool], int] = {}
    spans: dict[tuple[int, int, bool], list[date]] = {}
    day = date(2019, 1, 1)
    end = date(2051, 1, 1)
    while day < end:
        value = lunar_of(day)
        if value is not None:
            key = (value.year, value.month, bool(value.is_leap))
            lengths[key] = lengths.get(key, 0) + 1
            spans.setdefault(key, [day, day])[1] = day
        day += timedelta(days=1)

    # 窗口两端的农历月是被**截断**的（例如 2050 年 11 月只数到 1 月 1 日之前），
    # 它们的计数当然不是 29/30 —— 只校验完整落在窗口里的月份。
    complete = {
        key: count
        for key, count in lengths.items()
        if spans[key][0] > date(2019, 1, 1) and spans[key][1] < date(2050, 12, 31)
    }
    assert len(complete) > 32 * 11, f"完整月份太少（{len(complete)}），窗口可能不对"

    bad = {key: count for key, count in complete.items() if count not in (29, 30)}
    assert not bad, f"这些农历月的天数不是 29/30：{bad}"

    for year in range(2019, 2051):
        leap_months = [key for key in lengths if key[0] == year and key[2]]
        assert len(leap_months) <= 1, f"{year} 年出现了多个闰月：{leap_months}"


def test_lunar_returns_none_outside_the_table():
    """超出范围**不编数据**：日历上那格就不写农历。"""
    assert lunar_of(date(2018, 6, 1)) is None
    assert lunar_of(date(2056, 6, 1)) is None


def test_lunar_label_uses_month_name_on_the_first_day():
    """初一写月份名（"九月"），其余写"廿四"这种日名。"""
    assert lunar_label(date(2026, 10, 10)) == "九月"     # 农历九月初一
    assert lunar_label(date(2026, 10, 25)) == "十六"
    assert lunar_label(date(2026, 2, 17)) == "正月"      # 春节：正月初一


def test_qingming_dates_match_published_calendars():
    """清明是节气：逐年在公开日历上核对过（2026 是 4 月 5 日，不是 4 日）。"""
    expected = {
        2020: date(2020, 4, 4),
        2021: date(2021, 4, 4),
        2022: date(2022, 4, 5),
        2023: date(2023, 4, 5),
        2024: date(2024, 4, 4),
        2025: date(2025, 4, 4),
        2026: date(2026, 4, 5),
        2027: date(2027, 4, 5),
        2028: date(2028, 4, 4),
    }
    for year, day in expected.items():
        assert solar_term_qingming(year) == day, year


def test_festivals():
    assert "春节" in festivals_of(date(2026, 2, 17))
    assert "元宵节" in festivals_of(date(2026, 3, 3))
    assert "中秋节" in festivals_of(date(2026, 9, 25))
    assert "国庆节" in festivals_of(date(2026, 10, 1))
    assert "清明节" in festivals_of(date(2026, 4, 5))
    # 除夕：2026 年腊月只有 29 天，所以除夕是 2 月 16 日
    assert "除夕" in festivals_of(date(2026, 2, 16))
    # 普通日子没有节日
    assert festivals_of(date(2026, 6, 3)) == []


def test_holiday_arrangements_follow_the_official_notices():
    """逐条对官方原文：放假区间、补班日、以及"没数据的年份不显示"。"""
    # 2026 国庆：10/1–7 放假，9/20 与 10/10 上班
    for day in range(1, 8):
        assert holidays.status_of(date(2026, 10, day)) == holidays.REST
    assert holidays.status_of(date(2026, 10, 8)) == ""
    assert holidays.status_of(date(2026, 10, 10)) == holidays.WORK
    assert holidays.status_of(date(2026, 9, 20)) == holidays.WORK

    # 2026 春节：2/15–23 放假，2/14 与 2/28 上班
    assert holidays.status_of(date(2026, 2, 15)) == holidays.REST
    assert holidays.status_of(date(2026, 2, 23)) == holidays.REST
    assert holidays.status_of(date(2026, 2, 24)) == ""
    assert holidays.status_of(date(2026, 2, 14)) == holidays.WORK
    assert holidays.status_of(date(2026, 2, 28)) == holidays.WORK

    # 2025 国庆中秋连休 8 天，10/11 补班
    assert holidays.status_of(date(2025, 10, 8)) == holidays.REST
    assert holidays.status_of(date(2025, 10, 11)) == holidays.WORK

    # 2024 元旦只有 1 天；春节 2/10–17
    assert holidays.status_of(date(2024, 1, 1)) == holidays.REST
    assert holidays.status_of(date(2024, 1, 2)) == ""
    assert holidays.status_of(date(2024, 2, 10)) == holidays.REST


def test_years_without_official_data_show_no_rest_marker():
    """2027 年的安排还没发布 → 不显示休/班（猜错了会害用户按错日子安排行程）。"""
    assert holidays.has_data(2027) is False
    assert holidays.status_of(date(2027, 10, 1)) == ""
    assert holidays.has_data(2026) is True


def test_rest_and_work_labels():
    assert holidays.label_of(holidays.REST) == "休"
    assert holidays.label_of(holidays.WORK) == "班"
    assert holidays.label_of("") == ""
