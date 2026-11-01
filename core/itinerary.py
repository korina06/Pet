"""出行行程的领域模型：Itinerary / Day / Spot / Leg / Meal 五个实体、游览时长映射与确定性校验。

没有数据源的字段一律 `None`，缺数据不当作 0；估算值与真实值在数据里就分开标。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from typing import Any

# --------------------------------------------------------------------- 常量

#: 一次行程最多多少天。超过就明确拒绝——不生成，而不是生成一个 365 天的循环。
MAX_TRIP_DAYS = 30

#: 和风每日预报最多给多少天（官方文档：days 1–10，默认 7）。
#: 天气工具与排程共用这一个数：超出的天数写进降级原因，**不插值、不外推**。
MAX_FORECAST_DAYS = 10

#: 一天的可用窗口（09:00 ~ 18:00）。可行性校验拿它当分母。
DAY_START_MINUTES = 9 * 60
DAY_END_MINUTES = 18 * 60
DAY_WINDOW_MINUTES = DAY_END_MINUTES - DAY_START_MINUTES

#: 一顿饭按多久算（只计入真实找到的餐厅）。
MEAL_MINUTES = 60

#: 短于这个距离就优先用步行估算，不用驾车时长。
#: 原因见 README 第 41 步：市内 1~2 公里驾车时长会明显偏长（绕行、找车位），
#: 直接当"路上时间"会把本来可行的行程算成不可行。
WALK_FIRST_METERS = 3000
#: 步行速度按 80 米/分钟（约 4.8 km/h）。
WALK_METERS_PER_MINUTE = 80

DEFAULT_SPOT_MINUTES = 90

#: 高德 typecode 前 4 位 -> 默认游览时长（分钟）。
#: 这是**估算**，不是任何接口给的数字；它存在的价值是让"约 2 小时"有一个
#: 可解释、可单测的来源，而不是一句空话。
SPOT_TYPE_MINUTES: dict[str, int] = {
    "1100": 120,   # 风景名胜
    "1101": 90,    # 公园广场
    "1102": 120,   # 风景名胜相关
    "0800": 90,    # 体育休闲
    "0801": 90,    # 体育休闲相关
    "1400": 120,   # 科教文化（博物馆/美术馆）
    "0600": 90,    # 购物
}
#: 前 2 位兜底（上面没命中时用）
SPOT_TYPE_MINUTES_PREFIX: dict[str, int] = {
    "11": 120, "08": 90, "14": 120, "06": 90, "07": 60, "05": 60,
}


def default_duration_for(typecode: str | None) -> tuple[int, str]:
    """按高德 typecode 给一个默认游览时长，返回 ``(分钟, 来源标记)``。

    拿不到 typecode 就返回全局默认值——**依然标注它是估算**。
    """
    code = (typecode or "").strip()
    if len(code) >= 4 and code[:4] in SPOT_TYPE_MINUTES:
        return SPOT_TYPE_MINUTES[code[:4]], "type_default"
    if len(code) >= 2 and code[:2] in SPOT_TYPE_MINUTES_PREFIX:
        return SPOT_TYPE_MINUTES_PREFIX[code[:2]], "type_default"
    return DEFAULT_SPOT_MINUTES, "type_default"


def walk_minutes(straight_m: float | None) -> int | None:
    """直线距离 -> 步行分钟估算。没有距离就返回 None（不猜）。"""
    if straight_m is None:
        return None
    meters = max(0.0, float(straight_m))
    return max(1, int(round(meters / WALK_METERS_PER_MINUTE)))


def straight_line_m(
    a: tuple[float, float] | None, b: tuple[float, float] | None
) -> int | None:
    """两个 (lat, lon) 之间的直线距离（米）。

    这是**本地**估算，不花任何接口配额，用途是决定"这一对值不值得去查驾车路线"
    以及短途的步行时间。两点必须同坐标系（高德 POI 给的都是 GCJ-02），
    混用坐标系会有几十到几百米误差——所以调用方要保证来源一致。
    """
    if a is None or b is None:
        return None
    import math

    lat1, lon1 = float(a[0]), float(a[1])
    lat2, lon2 = float(b[0]), float(b[1])
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return int(round(2 * radius * math.asin(min(1.0, math.sqrt(h)))))


#: 装箱时估算远距离通行时间用的城市路网速度（米/分钟，约 24 km/h）。
#: 装箱阶段还没有驾车数据（要等装箱定下来才知道查哪些段），所以先用一个
#: 诚实的代理值；等真查到驾车时长，可行性校验会用真实值覆盖它。
CITY_METERS_PER_MINUTE = 400


def travel_estimate_minutes(straight_m: float | None) -> int | None:
    """装箱时用的"路上时间"估算。

    近距离按步行算，远距离按城市路网车速算。
    **不能一律按步行**：两个相距 13 公里的景点按步行算是 163 分钟，
    会把本来同一天能去的两个点硬拆到两天——这不是保守，是算错了。
    """
    if straight_m is None:
        return None
    meters = max(0.0, float(straight_m))
    if meters < WALK_FIRST_METERS:
        return walk_minutes(meters)
    return max(1, int(round(meters / CITY_METERS_PER_MINUTE)))


_OPENTIME_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(?:[-~至]|到)\s*(\d{1,2}):(\d{2})\s*$"
)


def parse_opentime(text: str | None) -> tuple[int, int] | None:
    """解析 ``HH:MM-HH:MM`` 形式的营业时间，返回 ``(开门分钟, 关门分钟)``。

    解析不出来就返回 None。**绝不猜**：猜错会得出"当天不开放"这种错误结论，
    而错误的拒绝比不检查更糟——用户会因此错过一个本来能去的地方。
    """
    if not text:
        return None
    match = _OPENTIME_RE.match(str(text))
    if not match:
        return None
    open_h, open_m, close_h, close_m = (int(g) for g in match.groups())
    if open_h > 23 or close_h > 24 or open_m > 59 or close_m > 59:
        return None
    if close_h < open_h:
        return None
    return open_h * 60 + open_m, close_h * 60 + close_m


# --------------------------------------------------------------------- 实体


@dataclass
class Spot:
    """一个景点。名称/地址/坐标/评分/营业时间来自高德，时长是估算。"""

    name: str
    address: str = ""
    #: (lat, lon)。None 表示高德没给坐标——此时算不了路线，也不会被编一个出来。
    location: tuple[float, float] | None = None
    typecode: str = ""
    type_text: str = ""
    #: 真实值（高德 show_fields=business）
    opentime: str | None = None
    rating: float | None = None
    #: Unsplash 配图（含署名），结构由 /api/photo 决定
    photo: dict[str, Any] | None = None
    #: 高德 POI 自带的地点照片直链（`show_fields=photos`）。
    #: 只作 Unsplash 的**兜底**：Unsplash 是"图库配图"、更好看，
    #: 高德这张是"这个地点自己的照片"、更贴切但往往很糙。
    #: 两条来源的**署名要求不一样**，所以界面必须按来源分别署名。
    amap_photos: list[str] = field(default_factory=list)
    #: 估算值。前端必须显示成"约 X 小时"
    duration_min: int | None = None
    duration_source: str = "type_default"   # "type_default" | "estimated"
    source: str = "amap_poi"
    #: False = 模型建议、高德没返回过。UI 必须显示"未核实"
    verified: bool = True


@dataclass
class Leg:
    """两个相邻景点之间的通行段。"""

    from_name: str
    to_name: str
    #: 真实值（高德驾车路线）
    distance_m: int | None = None
    duration_min: int | None = None          # 驾车口径
    #: 高速过路费（元，第 235 步）。只有**驾车**路线会给：
    #: `0.0` 表示查到了且没有过路费，`None` 表示没查到或不适用——
    #: 两者在界面上都不显示，但含义不同，所以不能混成一个值。
    tolls: float | None = None
    #: 由 core/coords.py 算出的直线距离与步行估算（不需要额外接口）
    straight_m: int | None = None
    walk_estimate_min: int | None = None
    mode: str = "driving"
    #: 默认是估算。只有真查到驾车路线时才改成 "amap_driving"——
    #: 这个字段是给归因用的，默认值绝不能谎称"这是高德给的"。
    source: str = "straight_line_estimate"

    def travel_minutes(self) -> int | None:
        """做可行性校验时该用的"路上时间"。

        优先级：近距离的步行估算 > 真实驾车时长 > 城市路网速度估算 > 步行估算。
        近距离优先步行，是因为驾车口径在 1~2 公里会明显偏长（绕行、找车位），
        拿它当路上时间会误判可行性。

        **远距离绝不能退回按步行算**：13 公里按步行是 163 分钟，
        会把同一天能去的两个点算成塞不下——那不是保守，是算错了。
        """
        if self.duration_min is not None and (
            self.straight_m is None or self.straight_m >= WALK_FIRST_METERS
        ):
            return self.duration_min
        if self.straight_m is not None and self.straight_m < WALK_FIRST_METERS:
            if self.walk_estimate_min is not None:
                return self.walk_estimate_min
        if self.straight_m is not None:
            return travel_estimate_minutes(self.straight_m)
        if self.duration_min is not None:
            return self.duration_min
        return self.walk_estimate_min


@dataclass
class Meal:
    """一家餐厅。高德 POI 的真实返回（名称/地址/评分/营业时间）。

    **没有"人均消费"字段**：高德不给，硬接只会得到模型编的数字。
    """

    name: str
    address: str = ""
    location: tuple[float, float] | None = None
    typecode: str = ""
    rating: float | None = None
    opentime: str | None = None
    #: 餐次（第 125 步）：早餐/午餐/晚餐。空串表示"没定餐次"。
    #: 高德**不返回**这个信息，是我们按位置与顺序分配的 —— 界面要说成"建议"。
    slot: str = ""
    source: str = "amap_poi"


@dataclass
class Hotel:
    """一家住宿候选。名称/地址/坐标/评分/营业时间来自高德。

    **没有"每晚价格"，也没有星级**：
    - 高德不返回房价；
    - 它给的 `type` 是「住宿服务;宾馆酒店」这种**分类**，不是"经济型/舒适型"。
    所以界面上只呈现真实能给的那几项——宁可少显示，也不编一个数字出来。
    """

    name: str
    address: str = ""
    location: tuple[float, float] | None = None
    typecode: str = ""
    rating: float | None = None
    opentime: str | None = None
    #: 到**当天第一个景点**的直线距离（米）。本地算的，不花配额。
    distance_m: int | None = None
    source: str = "amap_poi"


@dataclass
class Day:
    index: int
    date: str = ""
    spots: list[Spot] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    meals: list[Meal] = field(default_factory=list)
    #: 当天落脚点附近的真实酒店候选（高德）。见 nearest_hotels 的说明：
    #: 每天按"离当天第一个景点最近"挑，而不是把同一批复制到每一天。
    hotels: list[Hotel] = field(default_factory=list)
    notes: str = ""
    #: 当天的逐日预报（第 234 步）：和风 `weather/v1/daily` 归一化后的字典。
    #: **可能没有**（行程超过 10 天、某天没返回、或这段预报没取）——没有就是 None，
    #: 绝不拿相邻两天插一个值出来。
    forecast: dict[str, Any] | None = None
    #: 校验结果：算不出来时是 None（不是 0）
    total_minutes: int | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class Itinerary:
    """一份完整行程。trip_id 与 session_id 无关：一次会话可以产出多份行程。"""

    trip_id: str
    city: str
    #: 出发地（第 231 步）。**只记录、不改排程**：现有流水线只按目的地找景点，
    #: 出发地目前只用于在概览里写清「从哪出发」，以及让聊天的上下文知道用户在哪。
    #: 为什么不顺手拿它算自驾首段：那要在 plan_trip 里插一次跨城 driving 调用，
    #: 属于排程改动，得单独一步做并单独验证（不能夹在 UI 需求里悄悄改排程）。
    origin_city: str = ""
    start_date: str = ""
    end_date: str = ""
    transport: str = ""
    stay: str = ""
    preferences: list[str] = field(default_factory=list)
    note: str = ""
    days: list[Day] = field(default_factory=list)
    #: 实况（和风 current）。只有「此刻」，不含未来几天。
    weather: dict[str, Any] | None = None
    #: 逐日预报在每一天的 `Day.forecast` 上；顶层这里只有实况，两者不混。
    #: "ok" | "degraded" | "failed"
    status: str = "ok"
    degraded_reasons: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    #: 原始工具返回，用于归因：行程变差时能回答"是哪一步出的问题"
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------- 日期


def build_trip_dates(start_date: str, end_date: str) -> list[str]:
    """按日期区间逐日生成 ``YYYY-MM-DD``。

    用 ``datetime.date`` 而不是自己加减，闰年与月末由标准库保证。
    区间非法（无法解析 / 结束早于开始）时返回空列表，由校验层负责给用户报错。
    """
    try:
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
    except (TypeError, ValueError):
        return []
    if end < start:
        return []
    dates: list[str] = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return dates


def validate_trip_request(
    city: str,
    start_date: str,
    end_date: str,
    *,
    today: date | None = None,
) -> list[str]:
    """校验表单。返回错误列表，空列表表示通过。

    **这就是"需求被正确解读"的实际含义**：不是把话听懂，
    而是在输入无法被满足时明确拒绝并说明原因，
    而不是生成一个看起来成功的错误结果。
    """
    errors: list[str] = []
    if not (city or "").strip():
        errors.append("请填写目的地城市。")

    dates = build_trip_dates(start_date, end_date)
    if not dates:
        errors.append("请选择有效的开始与结束日期（结束不能早于开始）。")
        return errors

    if len(dates) > MAX_TRIP_DAYS:
        errors.append(f"行程天数最多 {MAX_TRIP_DAYS} 天，当前填的是 {len(dates)} 天。")

    current = today if today is not None else date.today()
    if date.fromisoformat(dates[0]) < current:
        errors.append(
            "开始日期不能早于今天：景点营业时间只有当下的数据，天气也只从今天起报。"
        )
    return errors


# --------------------------------------------------------------------- 校验


def day_total_minutes(day: Day) -> int | None:
    """一天要花掉的时间：游览 + 通行 + 用餐。

    任何一项缺数据就返回 None。**缺数据不等于 0**——
    把未知当 0 会把一个塞不下的行程算成"没问题"。
    """
    total = 0
    for spot in day.spots:
        if spot.duration_min is None:
            return None
        total += int(spot.duration_min)
    for leg in day.legs:
        minutes = leg.travel_minutes()
        if minutes is None:
            return None
        total += int(minutes)
    total += MEAL_MINUTES * min(len(day.meals), 3)
    return total


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_day(day: Day) -> list[str]:
    """单日可行性校验。纯确定性代码，不调模型。

    第 125 步起多了"**按预计到达时间**"判营业时间：原先只看"整个营业窗口是否
    完全落在可用时间之外"，证据够硬但太粗 —— 一个 14:00 才开门的博物馆，
    被排在早上 9:30 也照样放过。现在会算出来："预计 09:30 到达，但 14:00 才开门"。
    """
    issues: list[str] = []

    if not day.spots:
        issues.append(f"第 {day.index} 天没有安排景点")

    total = day_total_minutes(day)
    if total is not None and total > DAY_WINDOW_MINUTES:
        issues.append(
            f"第 {day.index} 天约需 {total / 60:.1f} 小时，"
            f"超出一天可用的 {DAY_WINDOW_MINUTES / 60:.0f} 小时"
        )

    # 从"一天开始的时刻"往后推：景点时长 + 相邻景点之间的路上时间
    cursor = DAY_START_MINUTES
    for position, spot in enumerate(day.spots):
        if position > 0 and position - 1 < len(day.legs):
            cursor += day.legs[position - 1].travel_minutes() or 0
        arrival = cursor

        if spot.location is None:
            issues.append(f"「{spot.name}」没有坐标，无法计算路线")

        window = parse_opentime(spot.opentime)
        if window is not None:
            open_min, close_min = window
            if close_min <= DAY_START_MINUTES or open_min >= DAY_END_MINUTES:
                # 证据够硬：整段可用时间都在营业时间之外
                issues.append(f"「{spot.name}」当天不开放（营业时间 {spot.opentime}）")
            elif arrival < open_min:
                issues.append(
                    f"「{spot.name}」预计 {_hhmm(arrival)} 到达，"
                    f"但 {_hhmm(open_min)} 才开门（营业时间 {spot.opentime}）"
                )
            elif arrival >= close_min:
                issues.append(
                    f"「{spot.name}」预计 {_hhmm(arrival)} 到达，"
                    f"已过 {_hhmm(close_min)} 的关门时间（营业时间 {spot.opentime}）"
                )

        cursor = arrival + (spot.duration_min or DEFAULT_SPOT_MINUTES)

    if not day.meals:
        issues.append(f"第 {day.index} 天没有找到餐厅")

    names = [spot.name for spot in day.spots]
    for name in sorted({n for n in names if names.count(n) > 1}):
        issues.append(f"第 {day.index} 天重复安排了「{name}」")

    return issues


def validate_itinerary(itinerary: Itinerary) -> list[str]:
    """整体校验：逐日校验 + 跨天重复。返回问题列表，并写回各 day.issues。

    **追加**而不是覆盖 ``itinerary.issues``：调用方在装箱阶段已经往里写了
    "有几个候选没排进去""驾车查询已达上限"这类信息，
    覆盖会把它们静默抹掉——而那正是"静默丢东西"的老毛病。
    """
    issues: list[str] = []
    seen: dict[str, int] = {}

    for day in itinerary.days:
        day_issues = validate_day(day)
        day.issues = day_issues
        day.total_minutes = day_total_minutes(day)
        issues.extend(day_issues)

        for spot in day.spots:
            first_day = seen.get(spot.name)
            if first_day is None:
                seen[spot.name] = day.index
            elif first_day != day.index:
                issues.append(
                    f"「{spot.name}」在第 {first_day} 天和第 {day.index} 天重复出现"
                )

    itinerary.issues = [*itinerary.issues, *issues]
    return issues


# ------------------------------------------------- 候选筛选与装箱（确定性）
#
# 下面这些函数**不调模型**。把真实候选点分配到各天，本质上是个装箱问题：
# 代码能做、能单测、结果可复现。让模型来做只会引入两个新问题——
# 它可能编出不存在的景点，也可能把行程排得超出一天的可用时间。
# 用户填的「额外要求」是自由文本，代码无法据它筛选景点，所以本轮**如实记录**
# 而不是假装理解了它（见 README 第 41 步）。

#: 旅行偏好 -> 高德分类/名称里可能出现的词。
#: 高德的 type 是「风景名胜;公园广场」这种中文分类，所以关键词只能这样映射；
#: 映射不上就说明这条偏好本轮没有实际作用，**不假装它生效了**。
PREFERENCE_HINTS: dict[str, tuple[str, ...]] = {
    "历史文化": ("名胜", "古迹", "博物馆", "纪念馆", "寺", "塔", "宫", "遗址", "文化", "故居"),
    "自然风光": ("公园", "风景区", "山", "湖", "湿地", "园林", "自然", "植物园"),
    "美食": ("美食", "小吃", "餐饮", "餐厅", "食街"),
    "购物": ("购物", "商场", "商业街", "市场", "百货"),
    "艺术": ("美术馆", "艺术", "剧院", "音乐", "展览", "画廊"),
    "休闲": ("休闲", "广场", "温泉", "度假", "体育", "游乐园"),
}

#: 装箱时给每一天留出的活动预算：一整天的窗口减去三餐的时间。
#: 留出三餐是因为餐厅要占时间，而装箱时还不知道当天会分到几家。
PACK_BUDGET_MINUTES = DAY_WINDOW_MINUTES - MEAL_MINUTES * 3


def preference_keyword(preferences: list[str]) -> str:
    """从偏好里挑**一个**检索关键词（只发一次额外 POI 请求，省配额）。

    用映射里的第一个词而不是偏好本身：「历史文化」直接当关键词搜不出东西，
    「名胜」才是有意义的检索词。
    """
    for pref in preferences or []:
        hints = PREFERENCE_HINTS.get(str(pref).strip())
        if hints:
            return hints[0]
        text = str(pref).strip()
        if text:
            return text
    return ""


def score_place(place: dict[str, Any], preferences: list[str]) -> int:
    """按偏好命中数打分。只看高德给的分类文本与名称，不猜、不编。"""
    haystack = f"{place.get('type') or ''} {place.get('name') or ''}"
    score = 0
    for pref in preferences or []:
        for hint in PREFERENCE_HINTS.get(str(pref).strip(), ()):
            if hint in haystack:
                score += 1
    return score


def _place_location(place: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = place.get("lat"), place.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def spot_from_place(place: dict[str, Any]) -> Spot | None:
    """高德 POI -> Spot。没有名字就返回 None（当作这个点不存在）。

    ``duration_min`` 由 typecode 映射而来，**一定是估算**，所以标 type_default。
    """
    name = str(place.get("name") or "").strip()
    if not name:
        return None
    typecode = str(place.get("typecode") or "").strip()
    minutes, source = default_duration_for(typecode)
    return Spot(
        name=name,
        address=str(place.get("address") or "").strip(),
        location=_place_location(place),
        typecode=typecode,
        type_text=str(place.get("type") or "").strip(),
        opentime=place.get("opentime") or None,
        rating=place.get("rating"),
        # 高德自带的地点照片（Unsplash 的兜底），逐条只留非空字符串
        amap_photos=[str(u) for u in (place.get("amap_photos") or []) if u],
        duration_min=minutes,
        duration_source=source,
        source="amap_poi",
        verified=True,
    )


def meal_from_place(place: dict[str, Any]) -> Meal | None:
    """高德 POI -> Meal。没有名字就返回 None。

    **没有"人均消费"字段**：高德不给，硬接只会得到模型编的数字。
    """
    name = str(place.get("name") or "").strip()
    if not name:
        return None
    return Meal(
        name=name,
        address=str(place.get("address") or "").strip(),
        location=_place_location(place),
        typecode=str(place.get("typecode") or "").strip(),
        rating=place.get("rating"),
        opentime=place.get("opentime") or None,
        source="amap_poi",
    )


def hotel_from_place(place: dict[str, Any]) -> Hotel | None:
    """高德 POI -> Hotel。没有名字就返回 None。

    **没有价格、没有星级**：高德不返回房价，`type` 也只是分类不是档次。
    """
    name = str(place.get("name") or "").strip()
    if not name:
        return None
    return Hotel(
        name=name,
        address=str(place.get("address") or "").strip(),
        location=_place_location(place),
        typecode=str(place.get("typecode") or "").strip(),
        rating=place.get("rating"),
        opentime=place.get("opentime") or None,
        source="amap_poi",
    )


#: 每天给几家住宿候选。给 2 家是为了有选择余地，又不至于把窗口塞满。
HOTELS_PER_DAY = 2


def nearest_hotels(
    hotels: list[Hotel],
    anchor: tuple[float, float] | None,
    limit: int = HOTELS_PER_DAY,
) -> list[Hotel]:
    """按到 ``anchor`` 的直线距离挑最近的几家（本地算，不花配额）。

    为什么按天挑，而不是把同一批酒店复制到每一天：
    **"哪一晚住哪家"是用户自己的决定**，代码替他编一个分配方案没有意义；
    但"这家酒店离你今天第一个景点多远"是代码算得出来的事实，
    把它摆出来，比编一句"推荐这家"诚实得多。

    ``anchor`` 为空（当天没排到景点）时按原顺序取前 ``limit`` 家。

    返回的是 ``dataclasses.replace`` 出来的**副本**：同一个酒店可能出现在
    多天里，而每天的 ``distance_m`` 不同；直接改原对象会让先算的那天被后算的覆盖。
    """
    if not hotels:
        return []
    if anchor is None:
        return list(hotels[:limit])

    scored: list[tuple[int, Hotel]] = []
    for hotel in hotels:
        distance = straight_line_m(anchor, hotel.location)
        # 没有坐标的排在最后，而且**不给它编一个距离**
        scored.append((distance if distance is not None else 10**9, hotel))
    scored.sort(key=lambda item: item[0])

    picked: list[Hotel] = []
    for distance, hotel in scored[:limit]:
        if distance >= 10**9:
            picked.append(hotel)
            continue
        picked.append(replace(hotel, distance_m=distance))
    return picked


#: 同一簇（同一天先去的一片区域）内，景点之间最大的直线距离。
#: 4 公里大约相当于"同一片景区/同一个商圈"，再远就属于另一个方向了。
CLUSTER_RADIUS_M = 4000


def _cluster_by_proximity(spots: list[Spot], radius_m: int = CLUSTER_RADIUS_M) -> list[list[int]]:
    """按"互相接近"把景点连成若干簇（单链接 + 广度优先，确定性）。

    为什么需要它：原来只是**按偏好分排序**再顺次装箱，于是同一天可能被塞进
    城市两端各一个景点 —— 看起来"排满了"，实际一天大半时间在路上。
    先聚类再装箱，同一天的点基本落在同一片区域。

    **没有坐标的景点各成一簇**：它们算不了距离，就不该拿来聚类。
    """
    total = len(spots)
    seen = [False] * total
    clusters: list[list[int]] = []

    for start in range(total):
        if seen[start] or spots[start].location is None:
            continue
        seen[start] = True
        group = [start]
        stack = [start]
        while stack:
            current = stack.pop()
            for other in range(total):
                if seen[other] or spots[other].location is None:
                    continue
                if straight_line_m(spots[current].location, spots[other].location) <= radius_m:
                    seen[other] = True
                    group.append(other)
                    stack.append(other)
        clusters.append(sorted(group))

    # 无坐标的：各自一簇，保持原有优先级
    for index in range(total):
        if not seen[index]:
            clusters.append([index])

    return clusters


def _order_by_nearest(spots: list[Spot]) -> list[Spot]:
    """把同一天的景点排成"少绕路"的顺序：从第一个（偏好最高）出发做最近邻。

    只改**顺序**，不改选中哪些 —— 排序变了，但丢弃规则仍由装箱预算决定。
    """
    remaining = list(spots)
    if not remaining:
        return remaining
    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        if last.location is None:
            ordered.append(remaining.pop(0))
            continue
        remaining.sort(
            key=lambda spot: (
                straight_line_m(last.location, spot.location)
                if spot.location is not None
                else 10 ** 9
            )
        )
        ordered.append(remaining.pop(0))
    return ordered


def arrange_days(spots: list[Spot], dates: list[str]) -> list[list[Spot]]:
    """把候选景点贪心装箱到各天，每天不超过 ``PACK_BUDGET_MINUTES``。

    第 125 步起：**先按地理位置聚类，再装箱**；每天内部的顺序按最近邻排，
    于是同一天的景点基本在同一片区域、路上时间更短。

    返回长度与 ``dates`` 相同的列表；放不下的候选会被丢掉，
    由调用方决定要不要把"丢掉了几个"写进 issues——**静默丢弃是不允许的**。
    """
    days: list[list[Spot]] = [[] for _ in dates]
    if not dates:
        return days

    # 簇的先后按"簇里最好的那个景点在偏好序里的位置"排 —— 好地方仍然先安排
    clusters = _cluster_by_proximity(spots)
    clusters.sort(key=lambda group: min(group))

    day_index = 0
    used = 0
    for group in clusters:
        ordered = _order_by_nearest([spots[index] for index in group])
        for spot in ordered:
            minutes = spot.duration_min or DEFAULT_SPOT_MINUTES
            placed = False
            while day_index < len(dates):
                extra = minutes
                if days[day_index]:
                    last = days[day_index][-1]
                    leg_m = straight_line_m(last.location, spot.location)
                    extra += travel_estimate_minutes(leg_m) or 0
                if used + extra <= PACK_BUDGET_MINUTES:
                    days[day_index].append(spot)
                    used += extra
                    placed = True
                    break
                day_index += 1
                used = 0
            if not placed:
                # 天数用完：剩下的候选由调用方记为"没排进去"
                return days
    return days


def _centroid(spots: list[Spot]) -> tuple[float, float] | None:
    """一组景点的几何中心（没有坐标就返回 None）。"""
    points = [spot.location for spot in spots if spot.location is not None]
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


#: 每天最多几家餐厅（早/午/晚各一）
MEALS_PER_DAY = 3
#: 餐次顺序：先近的当早餐，再午餐、晚餐。**这是我们的建议**，不是高德给的字段。
MEAL_SLOTS = ("早餐", "午餐", "晚餐")


def distribute_meals(
    meals: list[Meal],
    day_count: int,
    per_day: int = MEALS_PER_DAY,
    *,
    day_spots: list[list[Spot]] | None = None,
) -> list[list[Meal]]:
    """把真实餐厅分到各天，每天最多 ``per_day`` 家，不重复。

    第 125 步起：**按"离当天景点群最近"挑**，不再轮流分 —— 轮流的后果是
    "今天在城东玩、晚餐却订在城西"。挑完按由近到远把餐次标成早餐 / 午餐 / 晚餐。

    没有坐标（或没传 ``day_spots``）时退回轮流分配：那种时候算不了距离，
    与其编一个"就近"，不如老实按顺序分。
    """
    buckets: list[list[Meal]] = [[] for _ in range(max(0, day_count))]
    if not buckets:
        return buckets

    centers = [
        _centroid(day_spots[index]) if day_spots and index < len(day_spots) else None
        for index in range(len(buckets))
    ]
    if not any(center is not None for center in centers):
        # 老行为：轮流分（没有坐标就谈不上就近）
        for index, meal in enumerate(meals):
            target = index % len(buckets)
            if len(buckets[target]) < per_day:
                buckets[target].append(meal)
        return buckets

    used: set[str] = set()
    for index, bucket in enumerate(buckets):
        center = centers[index]
        if center is None:
            continue
        candidates = [
            meal
            for meal in meals
            if meal.name not in used and meal.location is not None
        ]
        # `or 10**9` 会把**距离恰好为 0**（同一坐标）当成"最远"——
        # 0 是 falsy，而那种情况本该排第一。用哨兵明确区分 None（算不出距离）。
        candidates.sort(
            key=lambda meal: (
                d if (d := straight_line_m(center, meal.location)) is not None else 10 ** 9
            )
        )
        for meal in candidates[: max(0, per_day)]:
            used.add(meal.name)
            bucket.append(meal)
        for slot, meal in zip(MEAL_SLOTS, bucket):
            meal.slot = slot

    # 有坐标的日子分完还有剩的餐厅，就按老办法塞给还没满的日子 —— 不浪费真实数据
    leftovers = [meal for meal in meals if meal.name not in used]
    for meal in leftovers:
        for bucket in buckets:
            if len(bucket) < per_day:
                bucket.append(meal)
                used.add(meal.name)
                # 第 145 步：补位的餐厅也要有餐次（评价9 P2）。
                # 否则界面与导出文本里这一家是"没有建议时段"的，看着像漏了。
                slot_index = len(bucket) - 1
                if slot_index < len(MEAL_SLOTS):
                    meal.slot = MEAL_SLOTS[slot_index]
                break
    return buckets


def build_legs(spots: list[Spot]) -> list[Leg]:
    """为同一天里相邻的景点建通行段（只算本地直线距离与步行估算）。

    驾车真实数据由调用方按需补充——**只给值得查的那些对**，见 trip_planner。
    """
    legs: list[Leg] = []
    for previous, current in zip(spots, spots[1:]):
        straight = straight_line_m(previous.location, current.location)
        legs.append(
            Leg(
                from_name=previous.name,
                to_name=current.name,
                straight_m=straight,
                walk_estimate_min=walk_minutes(straight),
            )
        )
    return legs
