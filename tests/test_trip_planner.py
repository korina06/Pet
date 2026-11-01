"""出行流水线：失败语义。拿不到坐标或景点就 failed 且不产出行程，天气/餐厅/路线失败则
degraded，状态不许谎称 ok，每一步失败都要可归因。

工具是注入的，整条流水线能完全离线跑。
"""

from __future__ import annotations

from typing import Any

from core.itinerary import DAY_WINDOW_MINUTES, MAX_FORECAST_DAYS
from core.schemas import ToolResult
from core.trip_planner import MAX_ROUTE_CALLS, TripRequest, build_itinerary


class FakeTool:
    """记录调用参数的假工具：真断言"这一步到底有没有被调用、用什么参数"。"""

    def __init__(self, name: str, result: ToolResult) -> None:
        self.name = name
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return self._result


def _geocode_ok(lat: float = 39.92, lon: float = 116.41) -> ToolResult:
    return ToolResult(
        ok=True,
        content="坐标拿到了",
        tool_name="geocode_city",
        data={"lat": lat, "lon": lon, "formatted_address": "北京市", "adcode": "110000"},
    )


def _weather_ok() -> ToolResult:
    return ToolResult(
        ok=True,
        content="北京当前晴，20°C",
        tool_name="get_weather",
        data={"location": "北京", "temp": 20, "text": "晴", "humidity": "40%"},
    )


def _place(
    name: str,
    *,
    lat: float = 39.92,
    lon: float = 116.41,
    typecode: str = "110200",
    distance_m: int = 500,
    opentime: str | None = "09:00-17:00",
    rating: float | None = 4.5,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "风景名胜;风景名胜;景点",
        "typecode": typecode,
        "address": f"{name}路 1 号",
        "distance": "500 米",
        "distance_m": distance_m,
        "lat": lat,
        "lon": lon,
        "rating": rating,
        "opentime": opentime,
    }


def _hotel(
    name: str, *, lat: float = 39.92, lon: float = 116.41, rating: float = 4.5
) -> dict[str, Any]:
    """一个住宿类 POI。注意字段里**没有价格、没有星级**——高德不给。"""
    place = _place(name, lat=lat, lon=lon, typecode="100100", rating=rating)
    place["type"] = "住宿服务;宾馆酒店"
    return place


def _poi_ok(places: list[dict[str, Any]]) -> ToolResult:
    return ToolResult(
        ok=True, content="找到若干地点", tool_name="find_nearby_places",
        data={"places": places},
    )


def _route_ok(
    distance_m: int = 12000, duration_s: int = 1500, tolls: float | None = None
) -> ToolResult:
    data: dict[str, Any] = {"distance_m": distance_m, "duration_s": duration_s}
    if tolls is not None:
        data["tolls"] = tolls
    return ToolResult(
        ok=True, content="驾车约 12 公里", tool_name="driving_route", data=data,
    )


def _request(**overrides: Any) -> TripRequest:
    base: dict[str, Any] = {
        "city": "北京",
        "start_date": "2026-03-01",
        "end_date": "2026-03-02",
        "transport": "公共交通",
        "stay": "市中心",
        "preferences": ["历史文化"],
        "note": "带老人，不要爬坡",
        "session_id": "s1",
    }
    base.update(overrides)
    return TripRequest(**base)


async def _run(
    *,
    geocode: ToolResult | None = None,
    weather: ToolResult | None = None,
    pois: ToolResult | None = None,
    meals: ToolResult | None = None,
    hotels: ToolResult | None = None,
    route: ToolResult | None = None,
    request: TripRequest | None = None,
) -> tuple[Any, FakeTool, FakeTool, FakeTool, FakeTool]:
    geocoder = FakeTool("geocode_city", geocode or _geocode_ok())
    weather_tool = FakeTool("get_weather", weather or _weather_ok())
    poi_tool = FakeTool(
        "find_nearby_places",
        pois or _poi_ok([_place("故宫博物院"), _place("景山公园")]),
    )
    route_tool = FakeTool("driving_route", route or _route_ok())
    # 餐厅与住宿走的是同一个 POI 工具，只是 types 不同：
    # 用独立的假工具更清楚，也证明"三类检索真的分了三条路"。
    meal_tool = FakeTool("find_nearby_places", meals or _poi_ok([_place("某餐厅", typecode="050100")]))
    hotel_tool = FakeTool(
        "find_nearby_places",
        hotels or _poi_ok([_hotel("近处酒店"), _hotel("稍远酒店", lat=39.99, lon=116.49)]),
    )

    class _PoiRouter:
        """按 types 把请求分给"景点 / 餐厅 / 住宿"的假结果。"""

        name = "find_nearby_places"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.spot_tool = poi_tool
            self.meal_tool = meal_tool
            self.hotel_tool = hotel_tool

        async def run(self, **kwargs: Any) -> ToolResult:
            self.calls.append(kwargs)
            types = str(kwargs.get("types") or "")
            if types == "050000":
                return await self.meal_tool.run(**kwargs)
            if types == "100000":
                return await self.hotel_tool.run(**kwargs)
            return await self.spot_tool.run(**kwargs)

    router = _PoiRouter()
    itinerary = await build_itinerary(
        request or _request(),
        geocoder=geocoder,
        weather_tool=weather_tool,
        poi_tool=router,
        route_tool=route_tool,
    )
    return itinerary, geocoder, weather_tool, router, route_tool  # type: ignore[return-value]


# ------------------------------------------------------- 失败语义（核心）


async def test_failed_geocode_produces_no_itinerary_at_all() -> None:
    """拿不到坐标 -> failed，且**一个景点都不能有**。

    这是"后端挂了还显示完美假行程"的直接针对：失败必须长得像失败。
    """
    itinerary, *_ = await _run(
        geocode=ToolResult(ok=False, error="高德没有找到「火星」对应的坐标", tool_name="geocode_city")
    )
    assert itinerary.status == "failed"
    assert all(day.spots == [] for day in itinerary.days), "失败时不能产出任何景点"
    assert any("火星" in reason or "坐标" in reason for reason in itinerary.degraded_reasons)
    assert itinerary.evidence["geocode"]["ok"] is False, "必须留下可归因的记录"


async def test_failed_poi_search_is_failed_not_empty_trip() -> None:
    itinerary, *_ = await _run(
        pois=ToolResult(ok=False, error="高德周边搜索失败。", tool_name="find_nearby_places")
    )
    assert itinerary.status == "failed"
    assert all(day.spots == [] for day in itinerary.days)


async def test_weather_failure_degrades_but_keeps_real_spots() -> None:
    """天气是非关键项：它失败时行程照出，但状态绝不能是 ok。"""
    itinerary, *_ = await _run(
        weather=ToolResult(ok=False, error="和风天气服务端错误", tool_name="get_weather")
    )
    assert itinerary.status == "degraded", "有降级原因时不能谎称 ok"
    assert itinerary.weather is None, "拿不到天气就不能编一个"
    assert any("和风天气服务端错误" in reason for reason in itinerary.degraded_reasons)
    assert sum(len(day.spots) for day in itinerary.days) == 2, "真实景点必须保留"


def _weather_with_forecast(
    dates: list[str], *, until: str | None = None
) -> ToolResult:
    """带逐日预报的假天气返回，形状与 WeatherTool 归一化后的 data 一致。

    ``until`` 是工具给的覆盖截止日（真实实现里 = 今天 + days - 1）；
    它可能晚于 `dates` 的最后一天（也就是「有几天查得到但没返回」）。
    """
    daily = [
        {
            "date": d,
            "text_day": "晴",
            "text_night": "多云",
            "temp_max": 20.0,
            "temp_min": 8.0,
            "temp_unit": "°C",
            "precip_prob": 0.1,
            "source": "qweather_daily",
        }
        for d in dates
    ]
    coverage = until or (dates[-1] if dates else "")
    return ToolResult(
        ok=True,
        content="北京当前晴，20°C",
        tool_name="get_weather",
        data={
            "location": "北京",
            "temp": 20,
            "text": "晴",
            "daily": daily,
            "forecast_days": len(daily),
            "forecast_until": coverage,
        },
    )


async def test_forecast_is_mapped_onto_each_day() -> None:
    """第 234 步：逐日预报按日期落到那一天，索引不能错位。"""
    itinerary, _, weather_tool, _, _ = await _run(
        weather=_weather_with_forecast(["2026-03-01", "2026-03-02"])
    )

    assert itinerary.days[0].forecast["date"] == "2026-03-01"
    assert itinerary.days[0].forecast["text_day"] == "晴"
    assert itinerary.days[1].forecast["date"] == "2026-03-02"
    # 第 236 步：传的是「查到哪天」，不是行程天数（days 从今天起算，会错位）
    assert weather_tool.calls[0]["end_date"] == "2026-03-02"
    assert "days" not in weather_tool.calls[0], "排程不该再猜 days"
    assert itinerary.status == "ok", itinerary.degraded_reasons


async def test_forecast_uses_the_coverage_window_instead_of_trip_length() -> None:
    """第 236 步回归：行程 11-04 开始，工具只覆盖到 11-08 时，
    不能因为「行程 7 天」就把 11-06 说成没预报。"""
    itinerary, *_ = await _run(
        request=_request(start_date="2026-11-04", end_date="2026-11-10"),
        weather=_weather_with_forecast(
            [f"2026-11-{day:02d}" for day in range(4, 9)], until="2026-11-08"
        ),
    )

    assert itinerary.days[2].forecast is not None, "11-06 在覆盖窗口内，必须映射上"
    assert itinerary.days[3].forecast is not None
    reasons = "；".join(itinerary.degraded_reasons)
    assert "2026-11-09" in reasons, "窗口外的日子要单独点名"
    assert "2026-11-06" not in reasons, "窗口内的日子不能被误报成缺数据"


async def test_days_beyond_the_forecast_limit_are_reported_not_interpolated() -> None:
    """超过覆盖窗口的部分：明说没有预报，**绝不拿最后一天插值**。"""
    dates = [f"2026-03-{day:02d}" for day in range(1, 13)]  # 12 天
    itinerary, *_ = await _run(
        weather=_weather_with_forecast(dates[:MAX_FORECAST_DAYS], until="2026-03-10"),
        request=_request(end_date="2026-03-12"),
    )

    assert itinerary.days[9].forecast is not None, "前 10 天应当有预报"
    assert itinerary.days[10].forecast is None, "第 11 天不能借用第 10 天的预报"
    assert any("2026-03-11" in reason for reason in itinerary.degraded_reasons)
    assert itinerary.status == "degraded"


async def test_a_day_missing_from_the_forecast_is_named_in_the_reasons() -> None:
    """某一天在覆盖窗口内却没返回：点名说清是哪天，那天的 forecast 保持 None。"""
    itinerary, *_ = await _run(
        weather=_weather_with_forecast(["2026-03-01"], until="2026-03-02")
    )

    assert itinerary.days[0].forecast is not None
    assert itinerary.days[1].forecast is None
    assert any("2026-03-02" in reason for reason in itinerary.degraded_reasons)


async def test_forecast_failure_keeps_the_current_weather() -> None:
    """预报挂了不该把实况一起丢掉，但要说清预报没拿到。"""
    result = _weather_with_forecast([])
    result.data = {  # type: ignore[union-attr]
        "location": "北京",
        "temp": 20,
        "text": "晴",
        "forecast_error": "和风天气返回的逐日预报为空。",
    }
    itinerary, *_ = await _run(weather=result)

    assert itinerary.weather and itinerary.weather["text"] == "晴"
    assert any("逐日预报为空" in reason for reason in itinerary.degraded_reasons)


async def test_missing_restaurants_degrades_and_is_recorded() -> None:
    itinerary, *_ = await _run(
        meals=ToolResult(ok=False, error="附近没有搜到餐厅", tool_name="find_nearby_places")
    )
    assert itinerary.status == "degraded"
    assert all(day.meals == [] for day in itinerary.days)
    assert any("餐厅" in reason for reason in itinerary.degraded_reasons)


async def test_route_failure_marks_leg_as_estimate_not_driving() -> None:
    """驾车查不到时必须标成估算——绝不能谎称是高德给的。"""
    far_a = _place("远处A", lat=39.90, lon=116.40)
    far_b = _place("远处B", lat=40.00, lon=116.50)   # 约 12 公里
    itinerary, _, _, _, route_tool = await _run(
        pois=_poi_ok([far_a, far_b]),
        route=ToolResult(ok=False, error="高德服务暂时不可用", tool_name="driving_route"),
    )
    assert route_tool.calls, "远距离的段应该去查驾车路线"
    legs = [leg for day in itinerary.days for leg in day.legs]
    assert legs, "应该有通行段"
    for leg in legs:
        assert leg.source == "straight_line_estimate", "查不到就不能标成 amap_driving"
        assert leg.duration_min is None
        assert leg.tolls is None, "没查到路线时，过路费是「不知道」而不是 0"
    assert itinerary.status == "degraded"
    assert any("直线估算" in reason for reason in itinerary.degraded_reasons)


async def test_driving_tolls_are_carried_into_the_leg() -> None:
    """第 235 步：聊天里说了过路费，表单行程的通行段也得带上。"""
    far_a = _place("远处A", lat=39.90, lon=116.40)
    far_b = _place("远处B", lat=40.00, lon=116.50)   # 约 12 公里
    itinerary, _, _, _, _ = await _run(
        pois=_poi_ok([far_a, far_b]),
        route=_route_ok(tolls=52.0),
    )

    legs = [leg for day in itinerary.days for leg in day.legs]
    assert legs, "应该有通行段"
    assert legs[0].tolls == 52.0, "高德给了过路费就必须落到 Leg 上"


async def test_zero_tolls_stay_zero_and_are_not_the_same_as_unknown() -> None:
    """市内驾车 `tolls=0`：照实存 0，但和「没查到」(None) 是两个意思。"""
    far_a = _place("远处A", lat=39.90, lon=116.40)
    far_b = _place("远处B", lat=40.00, lon=116.50)
    itinerary, _, _, _, _ = await _run(
        pois=_poi_ok([far_a, far_b]),
        route=_route_ok(tolls=0.0),
    )

    legs = [leg for day in itinerary.days for leg in day.legs]
    assert legs[0].tolls == 0.0
    assert legs[0].tolls is not None, "0 元不等于没查到"


async def test_tolls_do_not_survive_an_unusable_route_result() -> None:
    """第 238 步：没有时长就不算「认下这条高德路线」，过路费不能单独留下。

    否则界面会出现「过路费约 52 元（直线估算）」——钱数来自高德、口径却写着估算，
    自相矛盾。
    """
    far_a = _place("远处A", lat=39.90, lon=116.40)
    far_b = _place("远处B", lat=40.00, lon=116.50)   # 约 12 公里
    itinerary, *_ = await _run(
        pois=_poi_ok([far_a, far_b]),
        route=ToolResult(
            ok=True,
            content="只给了距离",
            tool_name="driving_route",
            data={"distance_m": 12000, "tolls": 52.0},
        ),
    )

    legs = [leg for day in itinerary.days for leg in day.legs]
    assert legs, "应该有通行段"
    assert legs[0].source == "straight_line_estimate"
    assert legs[0].tolls is None, "来源没认下来时，过路费也不该留下"


# ------------------------------------------------------- 正常路径


async def test_happy_path_produces_real_structured_spots() -> None:
    itinerary, geocoder, weather_tool, poi_router, _ = await _run()
    assert itinerary.status == "ok", f"全绿时应是 ok，实际 {itinerary.status}：{itinerary.degraded_reasons}"
    assert itinerary.city == "北京"
    assert len(itinerary.days) == 2
    assert itinerary.days[0].date == "2026-03-01"
    assert itinerary.days[1].date == "2026-03-02"
    assert itinerary.weather and itinerary.weather["text"] == "晴"

    spots = [spot for day in itinerary.days for spot in day.spots]
    assert [spot.name for spot in spots] == ["故宫博物院", "景山公园"]
    first = spots[0]
    assert first.address == "故宫博物院路 1 号", "地址必须来自高德，不能是编的"
    assert first.location == (39.92, 116.41)
    assert first.rating == 4.5, "评分是高德给的真实值"
    assert first.opentime == "09:00-17:00"
    assert first.verified is True

    # 游览时长是估算，来源必须标出来
    assert first.duration_min == 120, "typecode 110200 -> 风景名胜 120 分钟"
    assert first.duration_source == "type_default"

    # 每一步都被真实调用过
    assert geocoder.calls and geocoder.calls[0]["city"] == "北京"
    # 天气拿到的是**转成 WGS84** 之后的坐标，不是 geocode 的原始 GCJ-02（第 65 步）
    from core.coords import gcj02_to_wgs84

    expected_lon, expected_lat = gcj02_to_wgs84(116.41, 39.92)
    weather_call = weather_tool.calls[0]
    assert abs(weather_call["lat"] - expected_lat) < 1e-9
    assert abs(weather_call["lon"] - expected_lon) < 1e-9
    assert abs(weather_call["lat"] - 39.92) > 1e-6, "不能把 GCJ-02 直接当 WGS84 喂给和风"
    assert poi_router.calls, "至少调用过一次 POI"
    assert itinerary.evidence["geocode"]["data"]["adcode"] == "110000"


async def test_meals_come_from_real_restaurant_search() -> None:
    """餐厅必须来自 types=050000 的那次真实检索，不是模型写的。"""
    itinerary, _, _, poi_router, _ = await _run()
    assert any(call.get("types") == "050000" for call in poi_router.calls), (
        "应该有一次专门的餐饮类型检索"
    )
    meals = [meal for day in itinerary.days for meal in day.meals]
    assert [meal.name for meal in meals] == ["某餐厅"]
    assert meals[0].rating == 4.5


async def test_spot_duration_comes_from_typecode_map() -> None:
    """不同的 typecode 给不同的时长，证明映射真的生效（而不是写死一个数）。"""
    itinerary, *_ = await _run(
        pois=_poi_ok([
            _place("公园", typecode="110101"),     # 公园广场 -> 90
            _place("博物馆", typecode="140100"),   # 科教文化 -> 120
        ])
    )
    spots = {spot.name: spot for day in itinerary.days for spot in day.spots}
    assert spots["公园"].duration_min == 90
    assert spots["博物馆"].duration_min == 120


# ------------------------------------------------------- 配额与偏好


async def test_short_legs_never_spend_a_driving_call() -> None:
    """市内短途用步行估算就够了：不花配额，这是"只做驾车省配额"的关键。"""
    close = [_place("近A", lat=39.920, lon=116.410), _place("近B", lat=39.925, lon=116.415)]
    itinerary, _, _, _, route_tool = await _run(pois=_poi_ok(close))
    assert route_tool.calls == [], "1 公里以内不该去查驾车路线"
    legs = [leg for day in itinerary.days for leg in day.legs]
    assert legs and legs[0].walk_estimate_min is not None
    assert legs[0].duration_min is None, "没有驾车数据时不能有驾车时长"


async def test_driving_calls_are_capped() -> None:
    """封顶必须生效，且截断要写进 issues——静默截断是不允许的。"""
    many = [
        _place(f"远{i}", lat=39.90 + i * 0.1, lon=116.40 + i * 0.1)
        for i in range(MAX_ROUTE_CALLS + 4)
    ]
    itinerary, _, _, _, route_tool = await _run(request=_request(end_date="2026-03-10"), pois=_poi_ok(many))
    assert len(route_tool.calls) <= MAX_ROUTE_CALLS, "驾车调用必须封顶"
    assert itinerary.evidence["route_calls"] <= MAX_ROUTE_CALLS
    if len(route_tool.calls) == MAX_ROUTE_CALLS:
        assert any("上限" in issue for issue in itinerary.issues), "截断必须说出来"


async def test_preference_triggers_one_extra_search_with_mapped_keyword() -> None:
    """偏好只用**一个**关键词追加检索（省配额），且用映射后的词而不是偏好原文。"""
    _, _, _, poi_router, _ = await _run(request=_request(preferences=["历史文化"]))
    keyword_calls = [call for call in poi_router.calls if call.get("keywords")]
    assert len(keyword_calls) == 1, f"应该只有一次带关键词的检索，实际 {len(keyword_calls)}"
    assert keyword_calls[0]["keywords"] == "名胜", "「历史文化」直接当关键词搜不出东西，要用映射词"


async def test_no_preference_means_no_extra_search() -> None:
    _, _, _, poi_router, _ = await _run(request=_request(preferences=[]))
    assert not [call for call in poi_router.calls if call.get("keywords")]


# ------------------------------------------------------- 装箱与诚实性


async def test_spots_beyond_capacity_are_dropped_and_reported() -> None:
    """排不下的候选要被说出来，不能静默丢掉（那会让人以为当地只有这几个地方）。"""
    lots = [_place(f"S{i}", typecode="110200") for i in range(12)]
    itinerary, *_ = await _run(request=_request(end_date="2026-03-02"), pois=_poi_ok(lots))
    arranged = sum(len(day.spots) for day in itinerary.days)
    assert arranged < 12, "两天装不下 12 个 120 分钟的景点"
    assert any("没排进行程" in issue for issue in itinerary.issues)


async def test_day_never_exceeds_available_window() -> None:
    """装箱结果必须自洽：正常路径下不该出现"超出可用时间"这条问题。"""
    itinerary, *_ = await _run()
    assert not any("超出一天可用" in issue for issue in itinerary.issues)
    for day in itinerary.days:
        if day.total_minutes is not None and day.spots:
            assert day.total_minutes <= DAY_WINDOW_MINUTES, f"第{day.index}天算出来超了窗口"


async def test_note_is_recorded_not_faked_as_understood() -> None:
    """「额外要求」是自由文本，本轮如实记录，不假装被理解了。"""
    itinerary, *_ = await _run()
    assert itinerary.note == "带老人，不要爬坡"
    assert itinerary.preferences == ["历史文化"]


async def test_itinerary_keeps_user_intent_fields() -> None:
    itinerary, *_ = await _run()
    assert itinerary.transport == "公共交通"
    assert itinerary.stay == "市中心"
    assert itinerary.trip_id, "每份行程要有独立 id（与 session 无关）"
    assert itinerary.created_at > 0


async def test_evidence_records_every_step_for_attribution() -> None:
    """归因记录必须齐：行程变差时要能回答是哪一步出的问题。"""
    itinerary, *_ = await _run()
    for key in ("geocode", "weather", "poi", "poi_preference", "restaurants", "hotels"):
        assert key in itinerary.evidence, f"缺少 {key} 的归因记录"
    assert itinerary.evidence["poi"]["tool"] == "find_nearby_places"
    assert itinerary.evidence["geocode"]["ok"] is True


# ------------------------------------------------------- 住宿候选（第 43 步）


async def test_hotel_search_uses_the_lodging_type_code() -> None:
    """住宿必须来自 types=100000 的那次真实检索，不是模型写的。"""
    itinerary, _, _, poi_router, _ = await _run()
    assert any(str(call.get("types")) == "100000" for call in poi_router.calls), (
        "应该有一次住宿类型的检索"
    )
    hotels = [hotel for day in itinerary.days for hotel in day.hotels]
    assert hotels and hotels[0].name == "近处酒店"
    assert itinerary.evidence["hotels"]["ok"] is True


async def test_hotels_are_chosen_per_day_by_distance() -> None:
    """住宿按天挑最近的，而不是把同一批复制到每一天——两天的首选应该不同。"""
    near_first = _hotel("甲酒店", lat=39.900, lon=116.400)   # 贴近第 1 天的点
    near_third = _hotel("乙酒店", lat=40.100, lon=116.600)   # 贴近第 3 个点（被排到第 2 天）
    spots = [
        _place("S1", lat=39.90, lon=116.40),
        _place("S2", lat=39.901, lon=116.401),
        _place("S3", lat=40.10, lon=116.60),
    ]
    itinerary, *_ = await _run(pois=_poi_ok(spots), hotels=_poi_ok([near_first, near_third]))

    assert [spot.name for spot in itinerary.days[0].spots] == ["S1", "S2"]
    assert [spot.name for spot in itinerary.days[1].spots] == ["S3"]
    assert itinerary.days[0].hotels[0].name == "甲酒店", "第 1 天应优先离 S1 近的"
    assert itinerary.days[1].hotels[0].name == "乙酒店", "第 2 天应优先离 S3 近的"
    assert itinerary.days[0].hotels[0].distance_m is not None, "距离要算出来，否则没法排序"


async def test_missing_hotels_degrades_and_keeps_the_real_spots() -> None:
    """住宿是非关键项：它失败时行程照出，但状态不能谎称 ok。"""
    itinerary, *_ = await _run(
        hotels=ToolResult(ok=False, error="附近没有搜到住宿", tool_name="find_nearby_places")
    )
    assert itinerary.status == "degraded"
    assert all(day.hotels == [] for day in itinerary.days)
    assert any("住宿" in reason for reason in itinerary.degraded_reasons)
    assert sum(len(day.spots) for day in itinerary.days) == 2, "景点必须保留"


async def test_hotels_never_carry_price_or_star() -> None:
    """价格与星级没有数据源——返回里不许出现这两个键。"""
    itinerary, *_ = await _run()
    for day in itinerary.days:
        for hotel in day.hotels:
            payload = hotel.__dict__
            assert "price" not in payload, "不该有价格字段"
            assert "star" not in payload, "不该有星级字段"


# ------------------------------------------- 坐标系在边界上分开（第 65 步）


async def test_coordinates_are_converted_at_the_boundary() -> None:
    """高德给 GCJ-02、和风天气吃 WGS84 —— 必须在边界分开，不能混用。

    改之前有两个错：
    - geocode 的 GCJ-02 被直接喂给和风（偏约 600 米）；
    - 同一个 GCJ-02 传给 POI，而 POI 默认按 WGS84 又转一次（搜索中心偏几百米）。
    两者都不会报错，只是悄悄落在错的位置上。
    """
    from core.coords import gcj02_to_wgs84

    itinerary, _, weather_tool, poi_router, route_tool = await _run()

    # 1) POI 的入参就是 GCJ-02，必须声明，跳过那次多余的转换
    assert poi_router.calls, "应该有 POI 调用"
    assert all(call.get("coordsys") == "gcj02" for call in poi_router.calls), (
        "每次 POI 调用都要声明 coordsys=gcj02，实际 "
        + str([call.get("coordsys") for call in poi_router.calls])
    )

    # 2) 天气拿到的必须是转好的 WGS84
    expected_lon, expected_lat = gcj02_to_wgs84(116.41, 39.92)
    got = weather_tool.calls[0]
    assert abs(got["lat"] - expected_lat) < 1e-9 and abs(got["lon"] - expected_lon) < 1e-9, (
        f"天气要拿 WGS84，实际 {got['lat']},{got['lon']}"
    )

    # 3) 路线一直是对的：POI 坐标本身是 GCJ-02
    assert all(call.get("coordsys") == "gcj02" for call in route_tool.calls)

    # 4) 归因记录里也要能看出用的是哪套坐标
    assert itinerary.evidence["geocode"]["data"]["coordsys"] == "gcj02"

# ------------------------------------- 交通方式真的影响路线口径（第 67 步）


def _far_pois():
    return _poi_ok([
        _place("远A", lat=39.90, lon=116.40),
        _place("远B", lat=40.00, lon=116.50),   # 约 12 公里，必然触发路线查询
    ])


async def test_public_transit_sends_the_transit_mode_and_adcode() -> None:
    """选「公共交通」就必须走公交接口，并带上 geocode 给的 adcode。

    改之前这一列只是被存下来、再原样显示；用户选公交拿到的仍是驾车时长。
    """
    _itinerary, _geo, _weather, _poi, route_tool = await _run(
        request=_request(transport="公共交通"), pois=_far_pois()
    )

    assert route_tool.calls, "远距离的段应该查路线"
    assert all(call.get("mode") == "transit" for call in route_tool.calls), (
        "公共交通要走公交接口，实际 " + str([c.get("mode") for c in route_tool.calls])
    )
    assert all(call.get("city_adcode") == "110000" for call in route_tool.calls), (
        "公交必须带 adcode（来自 geocode），实际 "
        + str([c.get("city_adcode") for c in route_tool.calls])
    )


async def test_self_drive_stays_on_the_driving_api() -> None:
    _itinerary, _geo, _weather, _poi, route_tool = await _run(
        request=_request(transport="自驾"), pois=_far_pois()
    )

    assert route_tool.calls
    assert all(call.get("mode") == "driving" for call in route_tool.calls)


async def test_walk_plus_taxi_is_driving_for_long_legs_and_never_hits_the_api_for_short_ones() -> None:
    """用户拍板的映射：短途步行、长途驾车。

    短途那部分**不查接口**（省配额），靠 `Leg.travel_minutes()` 的直线步行估算，
    所以这里断言的是"没有路线调用 + 腿上有 walk_estimate_min"。
    """
    close = _poi_ok([
        _place("近A", lat=39.920, lon=116.410),
        _place("近B", lat=39.925, lon=116.415),
    ])
    _itinerary, _geo, _weather, _poi, route_tool = await _run(
        request=_request(transport="步行 + 打车"), pois=close
    )
    assert route_tool.calls == [], "短途不该花配额去查路线"
    legs = [leg for day in _itinerary.days for leg in day.legs]
    assert legs and legs[0].walk_estimate_min is not None
    assert legs[0].travel_minutes() == legs[0].walk_estimate_min, "短途按步行算"


async def test_transit_degrades_loudly_when_the_city_code_is_missing() -> None:
    """拿不到 adcode 时**不许**静默按驾车算，必须说出来。"""
    no_adcode = ToolResult(
        ok=True,
        content="坐标拿到了",
        tool_name="geocode_city",
        data={"lat": 39.92, "lon": 116.41, "formatted_address": "北京市"},
    )
    itinerary, *_ = await _run(
        geocode=no_adcode,
        request=_request(transport="公共交通"),
        pois=_far_pois(),
    )

    assert itinerary.status == "degraded"
    assert any("公交" in reason for reason in itinerary.degraded_reasons), (
        "降级原因要说清是公交查不了，实际 " + str(itinerary.degraded_reasons)
    )


# ----------------------------------- 选「不住宿」就不查住宿（第 81 步）


async def test_choosing_no_lodging_skips_the_hotel_search_entirely() -> None:
    """选了「不住宿」，住宿 POI **一次都不该查**。

    以前是无条件查：用户明明说不住宿，行程里照样挂一堆酒店候选，
    界面还写着"按你的住宿偏好"——那是**看起来会管**，最伤信任。
    跳过不是失败，所以也**不该**产生 degraded 原因。
    """
    itinerary, _geo, _weather, poi_router, _route = await _run(
        request=_request(stay="不住宿")
    )

    hotel_calls = [call for call in poi_router.calls if call.get("types") == "100000"]
    assert hotel_calls == [], f"不住宿时不该查住宿 POI，实际查了 {len(hotel_calls)} 次"

    assert itinerary.evidence["hotels"].get("skipped"), "evidence 里要记下是**主动跳过**"
    assert all(not day.hotels for day in itinerary.days), "每天都不该挂酒店"
    assert not any("住宿" in reason for reason in itinerary.degraded_reasons), (
        "主动跳过不是降级，不该进 degraded_reasons"
    )


async def test_a_lodging_preference_still_searches_hotels() -> None:
    """反证：选了「民宿」仍然要查住宿 —— 别把跳过做成"永远不查"。"""
    itinerary, _geo, _weather, poi_router, _route = await _run(
        request=_request(stay="民宿")
    )

    hotel_calls = [call for call in poi_router.calls if call.get("types") == "100000"]
    assert len(hotel_calls) == 1, f"民宿要查一次住宿，实际 {len(hotel_calls)} 次"
    assert itinerary.evidence["hotels"].get("ok") is True
    assert not itinerary.evidence["hotels"].get("skipped")
