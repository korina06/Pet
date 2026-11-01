"""出行领域模型：映射查得到什么、一天算不算得下、缺数据返回 `None` 而不是 0。
"""

from __future__ import annotations

from datetime import date

from core.itinerary import (
    DAY_WINDOW_MINUTES,
    DEFAULT_SPOT_MINUTES,
    MEAL_MINUTES,
    Day,
    Hotel,
    Itinerary,
    Leg,
    Meal,
    Spot,
    build_trip_dates,
    day_total_minutes,
    default_duration_for,
    hotel_from_place,
    nearest_hotels,
    parse_opentime,
    spot_from_place,
    validate_day,
    validate_itinerary,
    validate_trip_request,
    walk_minutes,
)


# ------------------------------------------------------------ 游览时长映射


def test_duration_map_matches_four_digit_prefix() -> None:
    """110101（综合公园）应命中 1101（公园广场）= 90 分钟。"""
    minutes, source = default_duration_for("110101")
    assert minutes == 90, f"公园广场应给 90 分钟，实际 {minutes}"
    assert source == "type_default", "来源必须标成映射默认值，不能冒充实测"


def test_duration_map_matches_two_digit_fallback() -> None:
    """四位没命中时退到两位：1199 不是已知四位码，但 11 开头是风景名胜。"""
    minutes, _ = default_duration_for("119900")
    assert minutes == 120, f"风景名胜兜底应给 120 分钟，实际 {minutes}"


def test_duration_map_falls_back_to_default_without_typecode() -> None:
    """没有 typecode 时给全局默认值，且**依然标注是估算**。"""
    minutes, source = default_duration_for("")
    assert minutes == DEFAULT_SPOT_MINUTES
    assert source == "type_default", "没有依据也必须承认自己没依据"

    minutes_none, _ = default_duration_for(None)
    assert minutes_none == DEFAULT_SPOT_MINUTES


# ------------------------------------------------------------ 步行估算


def test_walk_minutes_uses_eighty_meters_per_minute() -> None:
    assert walk_minutes(800) == 10, "800 米 / 80 = 10 分钟"
    assert walk_minutes(0) == 1, "再近也至少 1 分钟，不返回 0"


def test_walk_minutes_returns_none_without_distance() -> None:
    """没有距离就返回 None——缺数据不等于 0。"""
    assert walk_minutes(None) is None


# ------------------------------------------------------------ 营业时间解析


def test_parse_opentime_accepts_common_separators() -> None:
    assert parse_opentime("09:00-17:00") == (540, 1020)
    assert parse_opentime("08:30~18:00") == (510, 1080)
    assert parse_opentime("9:00 到 17:30") == (540, 1050)


def test_parse_opentime_accepts_24_00_as_closing() -> None:
    assert parse_opentime("09:00-24:00") == (540, 1440)


def test_parse_opentime_refuses_to_guess() -> None:
    """解析不出来必须返回 None，绝不能猜——猜错会得出"当天不开放"的错误结论。"""
    for bad in ("全天开放", "09:00", "周一闭馆", "", None, "09:00-08:00", "25:00-26:00"):
        assert parse_opentime(bad) is None, f"{bad!r} 不该被解析出结果"


# ------------------------------------------------------------ 日期推算


def test_build_trip_dates_is_inclusive_on_both_ends() -> None:
    assert build_trip_dates("2026-03-01", "2026-03-03") == [
        "2026-03-01", "2026-03-02", "2026-03-03",
    ]


def test_build_trip_dates_handles_single_day() -> None:
    assert build_trip_dates("2026-03-01", "2026-03-01") == ["2026-03-01"]


def test_build_trip_dates_crosses_month_and_leap_day() -> None:
    """跨月与闰年交给标准库，不自己算。2028 是闰年。"""
    assert build_trip_dates("2028-02-28", "2028-03-01") == [
        "2028-02-28", "2028-02-29", "2028-03-01",
    ]


def test_build_trip_dates_rejects_bad_ranges() -> None:
    assert build_trip_dates("2026-03-03", "2026-03-01") == [], "结束早于开始应返回空"
    assert build_trip_dates("", "") == []
    assert build_trip_dates("2026/03/01", "2026-03-03") == [], "格式不对应返回空"


# ------------------------------------------------------------ 请求校验（拒绝能力）


def test_validate_request_rejects_missing_city() -> None:
    errors = validate_trip_request("", "2026-03-01", "2026-03-03", today=date(2026, 1, 1))
    assert any("城市" in e for e in errors), f"缺城市应报错，实际 {errors}"


def test_validate_request_rejects_too_many_days() -> None:
    """365 天这种输入必须被明确拒绝，而不是生成一份 365 天的循环行程。"""
    errors = validate_trip_request(
        "北京", "2026-03-01", "2027-03-31", today=date(2026, 1, 1)
    )
    assert any("最多 30 天" in e for e in errors), f"应拒绝超长行程，实际 {errors}"


def test_validate_request_rejects_past_start_date() -> None:
    """天气只有实况、营业时间只有当下——查过去的行程没有意义。"""
    errors = validate_trip_request(
        "北京", "2020-01-01", "2020-01-03", today=date(2026, 1, 1)
    )
    assert any("不能早于今天" in e for e in errors), f"应拒绝过去的日期，实际 {errors}"
    assert not any("只有实况" in e for e in errors), "预报第 234 步已接上，拒绝理由不能还这么说"


def test_validate_request_accepts_today_as_start() -> None:
    today = date(2026, 3, 1)
    assert validate_trip_request("北京", "2026-03-01", "2026-03-03", today=today) == []


def test_validate_request_reports_bad_range_and_stops() -> None:
    """日期本身不合法时不再叠加天数错误，避免给用户一串互相矛盾的话。"""
    errors = validate_trip_request("北京", "2026-03-03", "2026-03-01", today=date(2026, 1, 1))
    assert len(errors) == 1, f"只该报日期区间这一条，实际 {errors}"


# ------------------------------------------------------------ 通行段口径


def test_leg_prefers_walk_estimate_for_short_distance() -> None:
    """1.2 公里：驾车口径（可能 8 分钟）不该被当成路上时间，应用步行估算。"""
    leg = Leg(
        from_name="A", to_name="B",
        distance_m=1400, duration_min=8, straight_m=1200, walk_estimate_min=15,
    )
    assert leg.travel_minutes() == 15, "近距离应优先用步行估算"


def test_leg_uses_driving_for_long_distance() -> None:
    """20 公里：步行估算没有意义，用驾车时长。"""
    leg = Leg(
        from_name="A", to_name="B",
        distance_m=20000, duration_min=35, straight_m=18000, walk_estimate_min=225,
    )
    assert leg.travel_minutes() == 35, "远距离应用驾车时长"


def test_leg_falls_back_to_driving_when_no_straight_distance() -> None:
    leg = Leg(from_name="A", to_name="B", distance_m=900, duration_min=6)
    assert leg.travel_minutes() == 6, "没有直线距离时退回驾车值"


def test_leg_returns_none_when_nothing_is_known() -> None:
    """什么都不知道就返回 None，不能返回 0 假装"不用时间"。"""
    assert Leg(from_name="A", to_name="B").travel_minutes() is None


# ------------------------------------------------------------ 单日时长


def test_day_total_minutes_sums_spots_legs_and_meals() -> None:
    day = Day(
        index=1,
        spots=[Spot(name="A", duration_min=120), Spot(name="B", duration_min=90)],
        legs=[Leg(from_name="A", to_name="B", straight_m=1000, walk_estimate_min=13)],
        meals=[Meal(name="某餐厅")],
    )
    assert day_total_minutes(day) == 120 + 90 + 13 + MEAL_MINUTES


def test_day_total_minutes_is_none_when_a_spot_has_no_duration() -> None:
    """缺一项就返回 None。把未知当 0 会把塞不下的行程算成没问题。"""
    day = Day(index=1, spots=[Spot(name="A", duration_min=120), Spot(name="B")])
    assert day_total_minutes(day) is None


def test_day_total_minutes_is_none_when_a_leg_is_unknown() -> None:
    day = Day(
        index=1,
        spots=[Spot(name="A", duration_min=60)],
        legs=[Leg(from_name="A", to_name="B")],
    )
    assert day_total_minutes(day) is None


# ------------------------------------------------------------ 可行性校验


def _ok_spot(name: str, minutes: int = 60) -> Spot:
    return Spot(name=name, duration_min=minutes, location=(39.9, 116.4), verified=True)


def test_validate_day_flags_overstuffed_day_with_real_numbers() -> None:
    """5 个 120 分钟的景点 + 1 顿饭 = 660 分钟 = 11.0 小时，超出 540 分钟的窗口。"""
    day = Day(index=2, spots=[_ok_spot(f"S{i}", 120) for i in range(5)], meals=[Meal(name="R")])
    issues = validate_day(day)
    assert any("超出一天可用" in i for i in issues), f"应报超时，实际 {issues}"
    assert any("11.0 小时" in i for i in issues), "报错必须带具体数字，否则模型没法据此重排"


def test_validate_day_accepts_a_day_that_fits() -> None:
    day = Day(
        index=1,
        spots=[_ok_spot("A", 120), _ok_spot("B", 90)],
        legs=[Leg(from_name="A", to_name="B", straight_m=1000, walk_estimate_min=13)],
        meals=[Meal(name="R")],
    )
    assert validate_day(day) == [], "能排下的行程不该报任何问题"


def test_validate_day_flags_missing_coordinates() -> None:
    """缺坐标的点算不了路线，必须报出来而不是悄悄放过。"""
    day = Day(index=1, spots=[Spot(name="A", duration_min=60, location=None)], meals=[Meal(name="R")])
    assert any("没有坐标" in i for i in validate_day(day))


def test_validate_day_flags_same_day_duplicate() -> None:
    day = Day(index=1, spots=[_ok_spot("故宫"), _ok_spot("故宫")], meals=[Meal(name="R")])
    assert any("重复安排" in i for i in validate_day(day))


def test_validate_day_flags_closed_spot_only_with_hard_evidence() -> None:
    """营业时间 20:00-22:00 与 09:00-18:00 完全不重叠 -> 报不开放。"""
    closed = Spot(
        name="夜市", duration_min=60, location=(39.9, 116.4), opentime="20:00-22:00"
    )
    day = Day(index=1, spots=[closed], meals=[Meal(name="R")])
    assert any("不开放" in i for i in validate_day(day))

    # 无法解析的营业时间不能触发这条结论
    vague = Spot(
        name="某园", duration_min=60, location=(39.9, 116.4), opentime="周一闭馆"
    )
    day2 = Day(index=1, spots=[vague], meals=[Meal(name="R")])
    assert not any("不开放" in i for i in validate_day(day2)), "证据不足时不许下结论"


def test_validate_day_flags_day_without_spots_and_restaurants() -> None:
    day = Day(index=1)
    issues = validate_day(day)
    assert any("没有安排景点" in i for i in issues)
    assert any("没有找到餐厅" in i for i in issues)


# ------------------------------------------------------------ 整体校验


def test_validate_itinerary_flags_cross_day_duplicate() -> None:
    itinerary = Itinerary(
        trip_id="t1",
        city="北京",
        days=[
            Day(index=1, spots=[_ok_spot("故宫")], meals=[Meal(name="R")]),
            Day(index=2, spots=[_ok_spot("故宫")], meals=[Meal(name="R")]),
        ],
    )
    issues = validate_itinerary(itinerary)
    assert any("第 1 天和第 2 天重复出现" in i for i in issues), f"应报跨天重复，实际 {issues}"


def test_validate_itinerary_writes_back_per_day_numbers() -> None:
    """校验结果要写回 day.issues / day.total_minutes，前端与归因都靠它。"""
    itinerary = Itinerary(
        trip_id="t1",
        city="北京",
        days=[Day(index=1, spots=[_ok_spot("A", 60)], legs=[], meals=[Meal(name="R")])],
    )
    validate_itinerary(itinerary)
    day = itinerary.days[0]
    assert day.total_minutes == 60 + MEAL_MINUTES
    assert day.issues == []
    assert itinerary.issues == []


def test_validate_itinerary_accepts_healthy_trip() -> None:
    itinerary = Itinerary(
        trip_id="t1",
        city="北京",
        days=[
            Day(index=1, spots=[_ok_spot("A", 120)], meals=[Meal(name="R1")]),
            Day(index=2, spots=[_ok_spot("B", 120)], meals=[Meal(name="R2")]),
        ],
    )
    assert validate_itinerary(itinerary) == []


def test_day_window_is_nine_hours() -> None:
    """窗口常量本身也钉一下：改了它，上面那些数字断言会一起失效。"""
    assert DAY_WINDOW_MINUTES == 9 * 60


# ------------------------------------------------------------ 住宿候选（第 43 步）


def test_hotel_has_no_price_or_star_fields() -> None:
    """高德不给房价、给的 type 也只是分类不是星级。

    所以 Hotel 里**根本没有**这两个字段——不是"留空"，是没有地方可以编。
    """
    hotel = hotel_from_place(
        {
            "name": "某某酒店",
            "address": "某路 1 号",
            "lat": 39.9,
            "lon": 116.4,
            "typecode": "100100",
            "rating": 4.6,
            "opentime": "24小时营业",
        }
    )
    assert hotel is not None
    assert hotel.name == "某某酒店"
    assert hotel.address == "某路 1 号"
    assert hotel.location == (39.9, 116.4)
    assert hotel.rating == 4.6
    assert hotel.opentime == "24小时营业"
    assert not hasattr(hotel, "price"), "不该有价格字段"
    assert not hasattr(hotel, "star"), "不该有星级字段"


def test_hotel_from_place_needs_a_name() -> None:
    assert hotel_from_place({"address": "某路 1 号"}) is None
    assert hotel_from_place({}) is None


def test_nearest_hotels_sorts_by_distance_to_the_anchor() -> None:
    anchor = (39.92, 116.41)
    far = Hotel(name="远处酒店", location=(40.10, 116.60))
    near = Hotel(name="近处酒店", location=(39.921, 116.411))
    picked = nearest_hotels([far, near], anchor, limit=2)
    assert [h.name for h in picked] == ["近处酒店", "远处酒店"]
    assert picked[0].distance_m is not None and picked[0].distance_m < 500
    assert picked[1].distance_m > picked[0].distance_m


def test_nearest_hotels_returns_copies() -> None:
    """同一个酒店会出现在多天里、每天距离不同：必须返回副本，否则互相覆盖。"""
    hotel = Hotel(name="某酒店", location=(39.93, 116.42))
    picked = nearest_hotels([hotel], (39.92, 116.41), limit=1)
    assert picked[0].distance_m is not None
    assert hotel.distance_m is None, "原对象不该被改（先算的那天会被后算的覆盖）"


def test_nearest_hotels_never_invents_a_distance() -> None:
    """酒店没有坐标时，距离保持 None——不编一个数。"""
    picked = nearest_hotels([Hotel(name="没坐标的酒店")], (39.92, 116.41), limit=1)
    assert picked[0].distance_m is None


def test_nearest_hotels_without_anchor_keeps_order() -> None:
    """当天没排到景点时按原顺序取——不去猜一个中心点。"""
    hotels = [Hotel(name="A"), Hotel(name="B"), Hotel(name="C")]
    assert [h.name for h in nearest_hotels(hotels, None, limit=2)] == ["A", "B"]


def test_nearest_hotels_handles_empty_input() -> None:
    assert nearest_hotels([], (39.92, 116.41)) == []


def test_spot_carries_amap_photos_as_unsplash_fallback() -> None:
    """高德自带的地点照片是 Unsplash 的兜底，逐条只留非空 url。"""
    spot = spot_from_place(
        {"name": "某景点", "amap_photos": ["https://x/1.jpg", "", None, "https://x/2.jpg"]}
    )
    assert spot is not None
    assert spot.amap_photos == ["https://x/1.jpg", "https://x/2.jpg"]


def test_spot_without_photos_has_an_empty_fallback_list() -> None:
    spot = spot_from_place({"name": "某景点"})
    assert spot is not None and spot.amap_photos == []
