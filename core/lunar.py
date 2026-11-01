"""农历换算、农历与公历节日、清明节气。

农历没有公式可用，这里存 2019–2050 年的历表数据并自写换算，锚点由测试钉住。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: 表内第一年（2019 农历年）的正月初一：公历 2019-02-05（公开历表事实）。
EPOCH = date(2019, 2, 5)

#: 农历年号范围（含）；超出就不显示农历。
MIN_LUNAR_YEAR = 2019
MAX_LUNAR_YEAR = 2050

#: 每一年的农历数据：(闰月, 月长模式)。
LUNAR_YEARS: dict[int, tuple[int, str]] = {
    2019: (0, "101010010011"),
    2020: (4, "0111100101010"),
    2021: (0, "011010101010"),
    2022: (0, "101011010101"),
    2023: (2, "0101101101010"),
    2024: (0, "010010110110"),
    2025: (6, "1010011011100"),
    2026: (0, "101001001110"),
    2027: (0, "110100100110"),
    2028: (5, "1110101001100"),
    2029: (0, "110101010011"),
    2030: (0, "010110101010"),
    2031: (3, "0111011010100"),
    2032: (0, "100101101101"),
    2033: (11, "0100101011110"),
    2034: (0, "010010101101"),
    2035: (0, "101001001101"),
    2036: (6, "1101000010111"),
    2037: (0, "110100100101"),
    2038: (0, "110101010010"),
    2039: (5, "1101110101000"),
    2040: (0, "101101011010"),
    2041: (0, "010101101101"),
    2042: (2, "0101010110110"),
    2043: (0, "010010011011"),
    2044: (7, "1010010101110"),
    2045: (0, "101001001011"),
    2046: (0, "101010100101"),
    2047: (5, "1011001001011"),
    2048: (0, "011011010010"),
    2049: (0, "101011011010"),
    2050: (3, "0100101101101"),
}

#: 农历月份名（十一月/十二月按习惯叫"冬月""腊月"）。
_MONTH_NAMES = (
    "正月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "冬月", "腊月",
)

#: 农历日名：初一…初十、十一…十九、二十、廿一…廿九、三十。
_DAY_NAMES = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
)


@dataclass(frozen=True)
class LunarDate:
    """一个公历日期对应的农历。"""

    year: int
    month: int
    day: int
    is_leap: bool = False

    @property
    def month_name(self) -> str:
        name = _MONTH_NAMES[self.month - 1]
        return f"闰{name}" if self.is_leap else name

    @property
    def day_name(self) -> str:
        return _DAY_NAMES[self.day - 1]

    @property
    def is_new_year(self) -> bool:
        """正月初一（春节）。"""
        return self.month == 1 and self.day == 1 and not self.is_leap


def _months_of(year: int) -> list[tuple[int, bool, int]]:
    """按真实顺序给出这一农历年的 (月份, 是否闰月, 天数)。闰月插在同名月之后。"""
    leap, pattern = LUNAR_YEARS[year]
    months: list[tuple[int, bool, int]] = []
    for index in range(12):
        month = index + 1
        months.append((month, False, 30 if pattern[index] == "1" else 29))
        if leap == month:
            leap_days = 30 if len(pattern) > 12 and pattern[12] == "1" else 29
            months.append((month, True, leap_days))
    return months


def _month_days(year: int, month: int, is_leap: bool) -> int:
    for value_month, value_leap, days in _months_of(year):
        if value_month == month and value_leap == is_leap:
            return days
    raise ValueError(f"{year} 年没有 {month} 月")


def lunar_of(solar: date) -> LunarDate | None:
    """把公历日期换算成农历；超出表格范围返回 None（不编数据）。"""
    offset = (solar - EPOCH).days
    if offset < 0:
        return None

    for year in range(MIN_LUNAR_YEAR, MAX_LUNAR_YEAR + 1):
        for month, is_leap, days in _months_of(year):
            if offset < days:
                return LunarDate(year=year, month=month, day=offset + 1, is_leap=is_leap)
            offset -= days
    return None


#: 公历固定日期节日：月-日 → 名字。
SOLAR_FESTIVALS: dict[tuple[int, int], str] = {
    (1, 1): "元旦",
    (3, 8): "妇女节",
    (3, 12): "植树节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
}

#: 农历节日：(月, 日) → 名字。除夕单独算（腊月的最后一天）。
LUNAR_FESTIVALS: dict[tuple[int, int], str] = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (2, 2): "龙抬头",
    (5, 5): "端午节",
    (7, 7): "七夕",
    (7, 15): "中元节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (12, 8): "腊八节",
    (12, 23): "小年",
}


def solar_term_qingming(year: int) -> date:
    """清明的公历日期（二十四节气之一，节假日里要用）。

    通行近似公式（21 世纪）：`day = floor(Y × 0.2422 + 4.81) - floor(Y / 4)`，
    Y 是年份后两位。测试里对 2020–2028 逐年在 `tests/test_calendar_data.py`
    与公开日期核对过；超出 2000–2099 直接报错，不猜。
    """
    if not (2000 <= year <= 2099):
        raise ValueError("清明公式只支持 2000–2099")
    y = year % 100
    return date(year, 4, int(y * 0.2422 + 4.81) - int(y / 4))


def festivals_of(solar: date, lunar: LunarDate | None = None) -> list[str]:
    """这一天的节日名（可能为空）。按"传统节日 → 公历节日 → 清明"的顺序给。"""
    names: list[str] = []
    value = lunar if lunar is not None else lunar_of(solar)

    if value is not None and not value.is_leap:
        # 闰月不过节：传统上闰月的同名月不再过一次节。
        name = LUNAR_FESTIVALS.get((value.month, value.day))
        if name:
            names.append(name)
        if value.month == 12 and value.day == _month_days(value.year, 12, False):
            names.append("除夕")

    solar_name = SOLAR_FESTIVALS.get((solar.month, solar.day))
    if solar_name:
        names.append(solar_name)

    # 第 147 步：清明公式只覆盖 2000–2099，范围外**跳过**而不是抛异常。
    # 评价10 实测过一个脏日期（1900-01-01）能把整个月视图接口打成 500 ——
    # 一个日期不该有这种杀伤力。
    try:
        is_qingming = solar_term_qingming(solar.year) == solar
    except ValueError:
        is_qingming = False
    if is_qingming:
        names.append("清明节")

    return names


def lunar_label(solar: date, lunar: LunarDate | None = None) -> str:
    """日历格子第二行的农历文字：初一写月份名（"八月"），其余写"廿四"。

    这是纸历/手机日历的通行写法：一个月只在初一写月份，其余写初几，
    读起来才有"几月几"的连贯感。
    """
    value = lunar if lunar is not None else lunar_of(solar)
    if value is None:
        return ""
    prefix = "闰" if value.is_leap else ""
    if value.day == 1:
        return f"{prefix}{_MONTH_NAMES[value.month - 1]}"
    return f"{prefix}{value.day_name}"


def tomorrow_of(solar: date) -> date:
    """小工具：后一天（除夕判定与"跨月翻页"都用得上）。"""
    return solar + timedelta(days=1)
