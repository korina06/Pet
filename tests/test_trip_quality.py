"""出行质量：地理聚类、餐次就近、按到达时间校验营业时间。

断言相对性质（同一天的点更近、路上总距离不增加、餐次顺序对），不写死米数。
"""

from __future__ import annotations

from core.itinerary import (
    Day,
    Meal,
    Spot,
    arrange_days,
    build_legs,
    distribute_meals,
    straight_line_m,
    validate_day,
)

#: 两个相距约 13 公里的"片区"
AREA_A = (39.900, 116.400)
AREA_B = (39.990, 116.500)


def _spot(name: str, location: tuple[float, float] | None, minutes: int = 120,
          opentime: str | None = "09:00-18:00") -> Spot:
    return Spot(name=name, location=location, duration_min=minutes, opentime=opentime)


def _near(base: tuple[float, float], offset: float) -> tuple[float, float]:
    """同一片区里的另一个点（offset 约等于 0.002 度 ≈ 200 米）。"""
    return (base[0] + offset, base[1] + offset)


def _max_pair_distance(spots: list[Spot]) -> int:
    worst = 0
    for index, first in enumerate(spots):
        for second in spots[index + 1:]:
            if first.location is None or second.location is None:
                continue
            worst = max(worst, straight_line_m(first.location, second.location))
    return worst


def test_a_day_does_not_mix_two_distant_areas():
    """同一天不该横跨两个相距很远的片区。

    构造的输入是**交错**的（A1, B1, A2, B2）：旧代码按顺序装箱，第一天会拿到 A1+B1。
    """
    spots = [
        _spot("A1", AREA_A),
        _spot("B1", AREA_B),
        _spot("A2", _near(AREA_A, 0.002)),
        _spot("B2", _near(AREA_B, 0.002)),
    ]

    days = arrange_days(spots, ["2026-05-01", "2026-05-02"])

    assert [len(day) for day in days] == [2, 2], [len(day) for day in days]
    for day in days:
        assert _max_pair_distance(day) < 3000, (
            f"这一天的景点相距 {_max_pair_distance(day)} 米，说明跨了片区："
            f"{[spot.name for spot in day]}"
        )

    names = [{spot.name for spot in day} for day in days]
    assert names[0] == {"A1", "A2"}, names
    assert names[1] == {"B1", "B2"}, names


def test_within_a_day_the_walking_order_is_shortened():
    """同一天的顺序要"少绕路"：总距离不应当比输入顺序更长。"""
    line = [
        (39.900, 116.400),
        (39.900, 116.420),
        (39.900, 116.440),
    ]
    # 故意把最远的放最前：第一个位置保留（偏好最高），其余按最近邻排
    # 每个点 100 分钟：三个点加上路上时间才装得进一天（预算 360 分钟）
    spots = [
        _spot("远", line[2], minutes=100),
        _spot("中", line[1], minutes=100),
        _spot("近", line[0], minutes=100),
    ]

    days = arrange_days(spots, ["2026-05-01"])
    ordered = days[0]

    def total_distance(items: list[Spot]) -> int:
        return sum(
            straight_line_m(a.location, b.location)
            for a, b in zip(items, items[1:])
        )

    assert len(ordered) == 3, [s.name for s in ordered]
    assert total_distance(ordered) <= total_distance(spots), (
        f"最近邻排序反而更远了：{[s.name for s in ordered]}"
    )
    assert ordered[0].name == "远", "第一个位置要保留给偏好最高的那个"


def test_spots_without_coordinates_never_break_arranging():
    spots = [
        _spot("没有坐标", None, minutes=60),
        _spot("有坐标", AREA_A, minutes=60),
    ]

    days = arrange_days(spots, ["2026-05-01"])

    assert sum(len(day) for day in days) == 2


def test_meals_are_picked_near_the_day_they_belong_to():
    day_spots = [
        [_spot("A1", AREA_A)],
        [_spot("B1", AREA_B)],
    ]
    meals = [
        Meal(name="甲餐厅", location=_near(AREA_A, 0.001)),
        Meal(name="乙餐厅", location=_near(AREA_A, 0.002)),
        Meal(name="丙餐厅", location=_near(AREA_B, 0.001)),
        Meal(name="丁餐厅", location=_near(AREA_B, 0.002)),
    ]

    buckets = distribute_meals(meals, 2, per_day=2, day_spots=day_spots)

    assert {meal.name for meal in buckets[0]} == {"甲餐厅", "乙餐厅"}, buckets[0]
    assert {meal.name for meal in buckets[1]} == {"丙餐厅", "丁餐厅"}, buckets[1]


def test_meal_slots_are_assigned_from_near_to_far():
    """餐次由近到远标成早餐/午餐/晚餐 —— 这是**我们的建议**，不是高德字段。"""
    day_spots = [[_spot("A1", AREA_A)]]
    meals = [
        Meal(name="近", location=_near(AREA_A, 0.001)),
        Meal(name="中", location=_near(AREA_A, 0.003)),
        Meal(name="远", location=_near(AREA_A, 0.005)),
    ]

    buckets = distribute_meals(meals, 1, per_day=3, day_spots=day_spots)

    assert [meal.name for meal in buckets[0]] == ["近", "中", "远"]
    assert [meal.slot for meal in buckets[0]] == ["早餐", "午餐", "晚餐"]


def test_meals_fall_back_to_round_robin_when_there_are_no_coordinates():
    """没有坐标就算不了距离 —— 这时按顺序分，别编一个"就近"。"""
    meals = [Meal(name=f"餐厅{index}") for index in range(4)]

    buckets = distribute_meals(meals, 2, per_day=2)

    assert [meal.name for meal in buckets[0]] == ["餐厅0", "餐厅2"]
    assert [meal.name for meal in buckets[1]] == ["餐厅1", "餐厅3"]
    assert all(meal.slot == "" for bucket in buckets for meal in bucket)


def test_arriving_before_opening_time_is_reported():
    """14:00 才开门的博物馆排在早上第一站，要**明说**会扑空。"""
    day = Day(
        index=1,
        spots=[
            Spot(name="上午博物馆", location=AREA_A, duration_min=60,
                 opentime="14:00-17:00"),
        ],
    )
    day.legs = build_legs(day.spots)

    issues = validate_day(day)

    assert any("09:00" in line and "14:00" in line for line in issues), issues


def test_arriving_after_closing_time_is_reported():
    """第二个景点算上路上时间已经关门了，也要说出来。"""
    day = Day(
        index=1,
        spots=[
            Spot(name="第一站", location=AREA_A, duration_min=90,
                 opentime="09:00-18:00"),
            Spot(name="第二站", location=_near(AREA_A, 0.01), duration_min=60,
                 opentime="09:00-10:00"),
        ],
    )
    day.legs = build_legs(day.spots)

    issues = validate_day(day)

    assert any("已过" in line and "第二站" in line for line in issues), issues


def test_a_normal_day_produces_no_opening_hour_issue():
    """9:00-18:00 的常规景点不该报任何营业时间问题（别把校验变成噪音）。"""
    day = Day(
        index=1,
        spots=[
            Spot(name="正常景点", location=AREA_A, duration_min=90,
                 opentime="09:00-18:00"),
        ],
        meals=[Meal(name="某餐厅", location=AREA_A)],
    )
    day.legs = build_legs(day.spots)

    assert validate_day(day) == []


def test_sub_pois_of_one_scenic_area_are_capped():
    """同一个景区的子景点最多留 2 个（第 125 步）。

    真事：真实高德返回里，候选景点有 6 个全是「吴山景区-XXX」——
    聚类把同一座山当成一个片区，于是三天的行程全在同一座山上。
    地理上"集中"，体验上很糟。
    """
    from core.trip_planner import MAX_SPOTS_PER_AREA, cap_spots_per_area

    places = [
        {"name": "吴山景区-有美堂", "rating": "4.7"},
        {"name": "吴山景区-城隍阁", "rating": "4.8"},
        {"name": "吴山景区-云居山", "rating": "4.5"},
        {"name": "吴山景区-浙江体育会", "rating": "4.4"},
        {"name": "西湖", "rating": "4.9"},
    ]

    kept = cap_spots_per_area(places)

    area = [p["name"] for p in kept if p["name"].startswith("吴山景区")]
    assert len(area) == MAX_SPOTS_PER_AREA, area
    assert "西湖" in [p["name"] for p in kept], "别的景点不该被顺手删掉"


def test_a_plain_name_is_never_merged_away():
    """没有分隔符的名字自成一类：不能被"片区"规则误合并。"""
    from core.trip_planner import cap_spots_per_area

    places = [{"name": "故宫"}, {"name": "天坛"}, {"name": "颐和园"}]

    assert len(cap_spots_per_area(places)) == 3


def test_leftover_restaurants_still_get_a_meal_slot():
    """补位的餐厅也要有餐次（第 145 步 / 评价9 P2）。

    原来最后那段 leftovers 只 append、不设 slot，于是界面和导出文本里
    这一家是"没有建议时段"的，看起来像漏了。
    """
    day_spots = [[_spot("A1", AREA_A)]]
    meals = [
        Meal(name=f"近{i}", location=_near(AREA_A, 0.001 * (i + 1))) for i in range(5)
    ]

    buckets = distribute_meals(meals, 1, per_day=3, day_spots=day_spots)

    assert len(buckets[0]) == 3
    assert all(meal.slot for meal in buckets[0]), [m.slot for m in buckets[0]]
