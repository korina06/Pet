"""出行行程的确定性流水线：取真实数据 → 按天装箱 → 如实校验。

不经过 Agent 循环，行程内容不由模型生成；工具是注入的，所以能用假工具离线跑。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.coords import gcj02_to_wgs84
from core.itinerary import (
    MAX_FORECAST_DAYS,
    WALK_FIRST_METERS,
    Day,
    Itinerary,
    Leg,
    Spot,
    arrange_days,
    build_legs,
    build_trip_dates,
    distribute_meals,
    hotel_from_place,
    meal_from_place,
    nearest_hotels,
    preference_keyword,
    score_place,
    spot_from_place,
    validate_itinerary,
)

logger = logging.getLogger(__name__)

#: 一次生成最多查几次驾车路线。
#: 用户明确选了"只做驾车、节省配额"，而驾车数据只在"这一对明显不近"时才可能
#: 改变结论，所以按需查（见下面的 straight_m 判断）并封顶。
MAX_ROUTE_CALLS = 8
#: 合并后最多留多少个候选景点（再多也排不进去，白占内存与配额）
MAX_CANDIDATE_SPOTS = 24
#: 每类 POI 请求的条数
SPOT_LIMIT = 10
RESTAURANT_LIMIT = 10
HOTEL_LIMIT = 8
#: 高德类型码：景点类 / 餐饮类 / 住宿类。
#: 住宿类是 "100000"（住宿服务）；餐饮是 "050000"。
SPOT_TYPES = "110000|110100|080000"
RESTAURANT_TYPES = "050000"
HOTEL_TYPES = "100000"


@dataclass
class TripRequest:
    """出行窗口提交上来的表单。字段与 index.html 里的表单一对应。"""

    city: str = ""
    #: 出发地（第 231 步）：只随行程记录与展示，**不参与排程**。
    #: 要让它算自驾首段，得在排程里插一次跨城 driving 调用，那是另一步的事。
    origin_city: str = ""
    start_date: str = ""
    end_date: str = ""
    transport: str = ""
    stay: str = ""
    preferences: list[str] = field(default_factory=list)
    #: 自由文本「额外要求」。本轮**如实记录**，不做语义解析（见模块 docstring）。
    note: str = ""
    session_id: str = ""


def _normalize_name(name: str) -> str:
    """景点名归一化，用于去重：去掉空白与常见的括号后缀差异。"""
    return re.sub(r"\s+", "", str(name or "")).strip()


def _merge_places(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按名字合并两批候选，保留先出现的那个（基础检索的结果更权威）。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for place in [*primary, *extra]:
        key = _normalize_name(place.get("name"))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(place)
    return merged


def _places_from(result: Any) -> list[dict[str, Any]]:
    """从 ToolResult 里取结构化地点列表。

    失败、结构不对、字段缺失一律返回空列表——**不猜、不补**。
    """
    if result is None or not getattr(result, "ok", False):
        return []
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return []
    places = data.get("places")
    if not isinstance(places, list):
        return []
    return [place for place in places if isinstance(place, dict)]


def _evidence(result: Any) -> dict[str, Any]:
    """给归因留的原始记录：这一次调用成没成、错在哪。

    行程变差时要能回答"是 geocode 给错坐标、POI 空了、还是路线失败"——
    没有这份记录，这三个答案长得一模一样。
    """
    if result is None:
        return {"ok": False, "error": "工具未执行"}
    record: dict[str, Any] = {
        "ok": bool(getattr(result, "ok", False)),
        "tool": getattr(result, "tool_name", "") or "",
    }
    error = getattr(result, "error", None)
    if error:
        record["error"] = str(error)
    return record


def _sort_key(place: dict[str, Any], preferences: list[str]) -> tuple[int, float]:
    """排序：偏好命中多的在前；同分按离市中心近的在前。"""
    distance = place.get("distance_m")
    if not isinstance(distance, (int, float)):
        distance = float("inf")
    return (-score_place(place, preferences), float(distance))


#: 同一个"片区"最多保留几个候选。
#: 高德会把一个景区拆成很多子 POI（实测：6 个候选全是「吴山景区-XXX」），
#: 不去重的话三天的行程会全在同一座山上 —— 地理上很"集中"，体验上很糟。
MAX_SPOTS_PER_AREA = 2


def _area_key(name: str) -> str:
    """取"片区名"：`吴山景区-有美堂` -> `吴山景区`。

    只按 高德 实际会用的分隔符切，不猜别的规则：切不出来就用整个名字，
    这样每个独立景点自成一类，不会被误合并。
    """
    for separator in ("-", "·", "（", "("):
        if separator in name:
            head = name.split(separator, 1)[0].strip()
            if head:
                return head
    return name.strip()


def cap_spots_per_area(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一个片区的子景点最多留 ``MAX_SPOTS_PER_AREA`` 个（保留评分高的）。

    必须保留至少一个：某个景点名字里恰好带 "-" 时，它不该被整条丢掉。
    """
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for place in places:
        name = str(place.get("name") or "")
        key = _area_key(name)
        if counts.get(key, 0) >= MAX_SPOTS_PER_AREA:
            continue
        counts[key] = counts.get(key, 0) + 1
        kept.append(place)
    return kept


def wants_no_lodging(stay: str) -> bool:
    """用户是不是明确选了「不住宿」（第 81 步）。

    为什么要有这个判断：以前住宿 POI 是**无条件**查的 —— 选了「不住宿」照样
    返回一堆酒店，界面还写着"按你的住宿偏好"。不查既省一次配额，也不会误导。
    """
    return "不住" in str(stay or "") or "不含住宿" in str(stay or "")


def route_mode_for(transport: str, straight_m: int | None) -> str:
    """把表单的「交通方式」映射成高德的路线口径（第 67 步）。

    改之前这一列只是被存进 Itinerary、然后原样显示在界面上 ——
    用户选「公共交通」拿到的仍是驾车时长，界面却说"按你选的交通方式安排"。

    映射（三种接口的可用性都用真实 key 探过）：
    - 公共交通 → ``transit``（高德要 adcode，取自 geocode）
    - 自驾     → ``driving``
    - 步行+打车 → **短途步行、长途驾车**（用户拍板）；打车时长≈驾车时长，
                 不额外接打车接口
    - 不限/其它 → ``driving``

    关于 ``walking``：调用方只在"这一段 >= 3km"时才会来查接口（省配额），
    而那种距离按"步行+打车"本来就该坐车，所以这里只发出 driving / transit。
    **短途的步行时间由 ``Leg.travel_minutes()`` 用直线距离估算**（它本来就近距离
    优先步行估算），所以"短途步行"是成立的，只是不花接口而已。
    `RouteTool` 仍然支持 walking（有测试），留给将来"用户明确要全程步行"的场景。
    """
    text = str(transport or "").strip()
    if "公共" in text or "公交" in text or "地铁" in text:
        return "transit"
    # 自驾 / 步行+打车 / 不限：需要查接口的那些段（>= 3km）都按驾车口径
    return "driving"

async def build_itinerary(
    request: TripRequest,
    *,
    geocoder: Any,
    weather_tool: Any,
    poi_tool: Any,
    route_tool: Any,
) -> Itinerary:
    """跑完整条流水线，返回一份**只用真实数据**的行程。

    失败语义（这是整个改造的核心）：
    - 拿不到坐标 → ``status="failed"``，立刻返回，**不生成任何行程**；
    - 拿不到景点 → ``status="failed"``，同上；
    - 天气/偏好检索/餐厅/部分路线失败 → ``status="degraded"`` 并记下具体原因，
      行程照常返回（因为剩下的部分依然是**真的**）；
    - 只要有任何一条降级原因，状态就不会是 "ok"——**不允许静默降级**。
    """
    preferences = [str(p).strip() for p in (request.preferences or []) if str(p).strip()]

    itinerary = Itinerary(
        trip_id=uuid.uuid4().hex[:12],
        city=(request.city or "").strip(),
        origin_city=(request.origin_city or "").strip(),
        start_date=request.start_date,
        end_date=request.end_date,
        transport=(request.transport or "").strip(),
        stay=(request.stay or "").strip(),
        preferences=preferences,
        note=(request.note or "").strip(),
        created_at=time.time(),
    )
    dates = build_trip_dates(request.start_date, request.end_date)
    itinerary.days = [Day(index=i + 1, date=d) for i, d in enumerate(dates)]

    # ---------------------------------------------------------- 1) 地理编码
    # 没有坐标，天气和 POI 都无法调用 —— 直接判失败，不编造一个坐标。
    geo = await geocoder.run(city=itinerary.city)
    itinerary.evidence["geocode"] = _evidence(geo)
    if not geo.ok or not isinstance(geo.data, dict):
        itinerary.status = "failed"
        itinerary.degraded_reasons.append(
            geo.error or f"没能把「{itinerary.city}」转换成坐标"
        )
        return itinerary
    try:
        lat = float(geo.data["lat"])
        lon = float(geo.data["lon"])
    except (KeyError, TypeError, ValueError):
        itinerary.status = "failed"
        itinerary.degraded_reasons.append("地理编码返回的坐标不完整")
        return itinerary
    # 高德给的是 **GCJ-02**（火星坐标）。三件事必须按坐标系分开，
    # 否则就是"把 GCJ-02 当 WGS84 又转一次"，搜索中心会整体偏几百米（第 65 步修）：
    #   - POI 检索：坐标本来就是 GCJ-02，所以显式声明 `coordsys="gcj02"` 跳过二次转换；
    #   - 驾车路线：同样用 GCJ-02 并声明（本来就是这样）；
    #   - 和风天气：它吃 **WGS84**，所以这里先转一次再喂。
    # POI 返回的景点坐标也是 GCJ-02，而 `straight_line_m` 比的是"同系统两点"，
    # 所以距离计算不受影响。
    wgs_lon, wgs_lat = gcj02_to_wgs84(lon, lat)
    itinerary.evidence["geocode"]["data"] = {
        "lat": lat, "lon": lon,
        "coordsys": "gcj02",
        "formatted_address": geo.data.get("formatted_address"),
        "adcode": geo.data.get("adcode"),
    }

    # ------------------------------------------------- 2) 天气（实况 + 逐日预报）
    # 非关键：拿不到就记降级原因，行程照做（天气只是附加信息）。
    # 用转好的 WGS84 坐标——把 GCJ-02 直接喂给和风会偏几百米。
    # 第 236 步：和风的 days 是「从今天起算」，不是「行程几天」。所以这里传
    # **end_date=行程最后一天**，由工具做日期减法并封顶 10 天；不能用 len(dates)，
    # 否则提前几天规划就会把「本来就查得到的日子」误报成没有预报。
    weather = await weather_tool.run(
        lat=wgs_lat, lon=wgs_lon, end_date=dates[-1] if dates else None
    )
    itinerary.evidence["weather"] = _evidence(weather)
    if weather.ok and isinstance(weather.data, dict):
        weather_data = weather.data
        # 实况缺了但预报在：仍然可用，只把缺的那块记下来。
        if weather_data.get("current_error"):
            itinerary.degraded_reasons.append(str(weather_data["current_error"]))
        elif weather_data.get("temp") is not None or weather_data.get("text"):
            # 顶层只留实况：逐日预报按天放在 Day.forecast 上，不在这里再存一份
            # （否则同一批预报在 JSON 里出现两次，界面还得挑一个信）。
            itinerary.weather = {
                key: value
                for key, value in weather_data.items()
                if key
                not in (
                    "daily",
                    "forecast_days",
                    "forecast_from",
                    "forecast_until",
                    "forecast_truncated",
                    "forecast_error",
                    "current_error",
                )
            }
        if weather_data.get("forecast_error"):
            itinerary.degraded_reasons.append(str(weather_data["forecast_error"]))
        if "daily" in weather_data:
            by_date = {
                item.get("date"): item
                for item in (weather_data.get("daily") or [])
                if isinstance(item, dict) and item.get("date")
            }
            for day in itinerary.days:
                item = by_date.get(day.date)
                if item is not None:
                    day.forecast = item
            # 覆盖到哪天由工具给出（它知道今天是哪天），这里不做日期减法：
            # 没有 until 时退回「只检查真有返回的日期」，宁可不报也不误报。
            until = str(weather_data.get("forecast_until") or "")
            inside = [d for d in dates if not until or d <= until]
            missing = [d for d in inside if d not in by_date]
            if missing:
                itinerary.degraded_reasons.append(
                    f"这几天没拿到预报：{'、'.join(missing)}"
                )
            beyond = [d for d in dates if until and d > until]
            if beyond:
                itinerary.degraded_reasons.append(
                    f"和风天气最多预报到今天起 {MAX_FORECAST_DAYS} 天"
                    f"（到 {until}），{beyond[0]} 起的那 {len(beyond)} 天没有预报"
                )
    else:
        itinerary.degraded_reasons.append(weather.error or "这次没拿到天气")

    # ---------------------------------------------------------- 3) 景点候选
    base = await poi_tool.run(
        lat=lat, lon=lon, limit=SPOT_LIMIT, types=SPOT_TYPES, show_fields=True,
        coordsys="gcj02",
    )
    itinerary.evidence["poi"] = _evidence(base)
    places = _places_from(base)
    if not places:
        itinerary.status = "failed"
        itinerary.degraded_reasons.append(base.error or "没找到任何候选景点")
        return itinerary

    # ------------------------------------------- 4) 按偏好追加一次检索（省配额）
    keyword = preference_keyword(preferences)
    if keyword:
        extra = await poi_tool.run(
            lat=lat, lon=lon, limit=SPOT_LIMIT,
            keywords=keyword, types=SPOT_TYPES, show_fields=True,
            coordsys="gcj02",
        )
        itinerary.evidence["poi_preference"] = _evidence(extra)
        extra_places = _places_from(extra)
        if extra_places:
            places = _merge_places(places, extra_places)
        else:
            itinerary.degraded_reasons.append(
                extra.error or f"按偏好「{keyword}」没搜到额外地点"
            )

    # ------------------------------------------------------------ 5) 餐厅
    food = await poi_tool.run(
        lat=lat, lon=lon, limit=RESTAURANT_LIMIT,
        types=RESTAURANT_TYPES, show_fields=True,
        coordsys="gcj02",
    )
    itinerary.evidence["restaurants"] = _evidence(food)
    restaurant_places = _places_from(food)
    if not restaurant_places:
        itinerary.degraded_reasons.append(food.error or "这次没找到餐厅")

    # ----------------------------------------------------------- 5b) 住宿
    # 住宿类 POI 给的是**真实酒店**：名称 / 地址 / 坐标 / 评分 / 营业时间。
    # **没有房价、没有星级** —— 高德不返回这两项，所以 Itinerary 里也没有这两个字段，
    # 界面上就不会出现一个"看起来是价格"的东西。
    if wants_no_lodging(itinerary.stay):
        # 选了「不住宿」：**不查、不降级**。这不是失败，是按用户要求跳过。
        # evidence 里记一笔，将来归因时能看出"这里本来就该是空的"。
        hotels = []
        itinerary.evidence["hotels"] = {
            "ok": True,
            "skipped": "用户选了不住宿",
        }
    else:
        lodging = await poi_tool.run(
            lat=lat, lon=lon, limit=HOTEL_LIMIT, types=HOTEL_TYPES, show_fields=True,
            coordsys="gcj02",
        )
        itinerary.evidence["hotels"] = _evidence(lodging)
        hotel_places = _places_from(lodging)
        if not hotel_places:
            itinerary.degraded_reasons.append(lodging.error or "这次没找到住宿候选")
        hotels = [
            hotel
            for hotel in (hotel_from_place(place) for place in hotel_places)
            if hotel is not None
        ]

    # ------------------------------------------------- 6) 打分排序 -> Spot
    places.sort(key=lambda place: _sort_key(place, preferences))
    # 第 125 步：同一片区的子景点最多留 2 个。排序在前，所以留下来的是评分/偏好更高的。
    places = cap_spots_per_area(places)
    spots: list[Spot] = []
    for place in places[:MAX_CANDIDATE_SPOTS]:
        spot = spot_from_place(place)
        if spot is not None:
            spots.append(spot)

    # ------------------------------------------------------------ 7) 装箱
    buckets = arrange_days(spots, dates)
    arranged = sum(len(bucket) for bucket in buckets)
    dropped = len(spots) - arranged
    if dropped > 0:
        # 丢弃必须说出来：静默丢掉候选会让人以为"当地就这几个地方"。
        itinerary.issues.append(
            f"有 {dropped} 个候选景点没排进行程（天数或每天可用时间不够）"
        )

    meals = [meal for meal in (meal_from_place(p) for p in restaurant_places) if meal]
    # 第 125 步：把"这一天的景点"传进去，餐厅就能按就近分配（而不是轮流分）
    meal_buckets = distribute_meals(meals, len(dates), day_spots=buckets)

    # ------------------------------------- 8) 通行段（本地位移 + 按需驾车）
    route_calls = 0
    truncated = False
    pending: list[tuple[Leg, tuple[float, float], tuple[float, float]]] = []

    for day, day_spots, day_meals in zip(itinerary.days, buckets, meal_buckets):
        day.spots = day_spots
        day.meals = day_meals
        day.legs = build_legs(day_spots)
        # 住宿按"离当天第一个景点最近"挑：距离是本地算的，不花配额；
        # 而"哪一晚住哪家"留给用户自己决定（见 nearest_hotels 的说明）。
        day.hotels = nearest_hotels(
            hotels, day_spots[0].location if day_spots else None
        )
        for index, leg in enumerate(day.legs):
            start = day_spots[index].location
            end = day_spots[index + 1].location
            if start is None or end is None:
                continue
            # 只给"明显不近"的段查驾车：近距离驾车时长会偏长、而且不值一次配额。
            if leg.straight_m is not None and leg.straight_m >= WALK_FIRST_METERS:
                pending.append((leg, start, end))

    # 公交要用 geocode 给的 adcode；拿不到就明确降级成驾车（并说出来）
    adcode = ""
    geo_data = itinerary.evidence.get("geocode", {}).get("data")
    if isinstance(geo_data, dict):
        adcode = str(geo_data.get("adcode") or "")
    transit_unavailable = False

    for leg, start, end in pending:
        if route_calls >= MAX_ROUTE_CALLS:
            truncated = True
            break
        way = route_mode_for(itinerary.transport, leg.straight_m)
        if way == "transit" and not adcode:
            way = "driving"
            transit_unavailable = True
        result = await route_tool.run(
            origin_lat=start[0], origin_lon=start[1],
            dest_lat=end[0], dest_lon=end[1],
            coordsys="gcj02",
            mode=way,
            city_adcode=adcode,
        )
        route_calls += 1
        if result.ok and isinstance(result.data, dict):
            distance = result.data.get("distance_m")
            seconds = result.data.get("duration_s")
            if isinstance(distance, (int, float)):
                leg.distance_m = int(distance)
            if isinstance(seconds, (int, float)):
                leg.duration_min = max(1, int(round(seconds / 60)))
            if leg.duration_min is not None:
                leg.mode = str(result.data.get("mode") or way)
                # 来源要写清是**哪种口径**的高德数据，不能一律写成 amap_driving
                leg.source = f"amap_{leg.mode}"
                # 过路费（第 235 步）：只有真拿到驾车路线（有 duration）才收。
                # 放在这个分支里，是为了避免出现「过路费 52 元（直线估算）」这种
                # 自相矛盾的组合——来源都还没认下来，钱数就不该单独留下（第 238 步）。
                tolls = result.data.get("tolls")
                if isinstance(tolls, (int, float)) and not isinstance(tolls, bool):
                    leg.tolls = float(tolls)
        else:
            # 查不到就明确标成估算，**不谎称是高德给的**；
            # 而且要说清是**哪一段**、以及用什么顶替了——只说"高德报错"
            # 用户没法判断哪段路程数字不可信。
            itinerary.degraded_reasons.append(
                f"{leg.from_name} → {leg.to_name} 的驾车路线没查到"
                f"（{result.error or '高德未返回路线'}），这一段用的是直线估算"
            )

    if transit_unavailable:
        itinerary.degraded_reasons.append(
            "这次没拿到城市编码，公交路线查不了，通行段用的是驾车时长"
        )
    if truncated:
        itinerary.issues.append(
            f"驾车路线查询已达上限（{MAX_ROUTE_CALLS} 次），其余通行段用直线距离估算"
        )
    itinerary.evidence["route_calls"] = route_calls

    # ------------------------------------------------------------ 9) 校验
    validate_itinerary(itinerary)

    if not any(day.spots for day in itinerary.days):
        itinerary.status = "failed"
        itinerary.degraded_reasons.append("没有把任何景点排进行程")
    elif itinerary.degraded_reasons:
        # 有任何一条降级原因，状态就不能是 ok —— 静默降级是之前最严重的问题。
        itinerary.status = "degraded"
    else:
        itinerary.status = "ok"

    logger.info(
        "行程 %s：%s，%d 天，%d 个景点，%d 次驾车查询，状态=%s",
        itinerary.trip_id, itinerary.city, len(itinerary.days),
        arranged, route_calls, itinerary.status,
    )
    return itinerary
