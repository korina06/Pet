"""出行接口层：输入不满足要 400、拿不到真数据就 failed、非关键项失败要 degraded。

不启动服务器，用 ASGITransport 直连应用，四个外部工具全换成假的。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from api.trip_routes import router
from core.schemas import ToolResult
from core.trip_store import TripStore


# --------------------------------------------------------------- 假对象


class FakeTool:
    def __init__(self, name: str, result: ToolResult) -> None:
        self.name = name
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return self._result


class FakeRegistry:
    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools

    def get(self, name: str) -> Any:
        return self._tools.get(name)


def _place(name: str, *, typecode: str = "110200", lat: float = 39.92, lon: float = 116.41) -> dict:
    return {
        "name": name,
        "type": "风景名胜;风景名胜;景点",
        "typecode": typecode,
        "address": f"{name}路 1 号",
        "distance": "500 米",
        "distance_m": 500,
        "lat": lat,
        "lon": lon,
        "rating": 4.5,
        "opentime": "09:00-17:00",
    }


def _ok(name: str, data: dict) -> ToolResult:
    return ToolResult(ok=True, content="ok", tool_name=name, data=data)


GEOCode_OK = _ok("geocode_city", {"lat": 39.92, "lon": 116.41, "formatted_address": "北京市", "adcode": "110000"})
WEATHER_OK = _ok("get_weather", {"location": "北京", "temp": 20, "text": "晴"})
POI_OK = _ok("find_nearby_places", {"places": [_place("故宫博物院"), _place("景山公园")]})
RESTAURANT_OK = _ok("find_nearby_places", {"places": [_place("某餐厅", typecode="050100")]})
HOTEL_OK = _ok("find_nearby_places", {"places": [_place("某酒店", typecode="100100")]})
ROUTE_OK = _ok("driving_route", {"distance_m": 12000, "duration_s": 1500})


class PoiRouter:
    """同一个 POI 工具按类型把请求分成"景点"和"餐厅"两批，模拟真实调用。"""

    name = "find_nearby_places"

    def __init__(
        self,
        spots: ToolResult = POI_OK,
        meals: ToolResult = RESTAURANT_OK,
        hotels: ToolResult = HOTEL_OK,
    ) -> None:
        self.spots = spots
        self.meals = meals
        self.hotels = hotels
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        types = str(kwargs.get("types") or "")
        if types == "050000":
            return self.meals
        if types == "100000":
            return self.hotels
        return self.spots


def build_app(
    *,
    geocode: ToolResult = GEOCode_OK,
    weather: ToolResult = WEATHER_OK,
    poi: Any = None,
    route: ToolResult = ROUTE_OK,
    store: TripStore | None = None,
    omit: str | None = None,
) -> FastAPI:
    poi_tool = poi if poi is not None else PoiRouter()
    tools: dict[str, Any] = {
        "geocode_city": FakeTool("geocode_city", geocode),
        "get_weather": FakeTool("get_weather", weather),
        "find_nearby_places": poi_tool,
    }
    if omit:
        tools.pop(omit, None)

    app = FastAPI()
    app.include_router(router)
    app.state.registry = FakeRegistry(tools)
    app.state.route_tool = FakeTool("driving_route", route)
    app.state.trip_store = store if store is not None else TripStore()
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "s1",
        "city": "北京",
        "start_date": "2099-03-01",
        "end_date": "2099-03-02",
        "transport": "公共交通",
        "stay": "市中心",
        "preferences": ["历史文化"],
        "note": "带老人，不要爬坡",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------- 正常路径


async def test_create_trip_returns_structured_real_itinerary() -> None:
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["trip_id"]
    itinerary = payload["itinerary"]
    assert itinerary["status"] == "ok", itinerary["degraded_reasons"]
    assert itinerary["city"] == "北京"
    assert len(itinerary["days"]) == 2

    names = [spot["name"] for day in itinerary["days"] for spot in day["spots"]]
    assert names == ["故宫博物院", "景山公园"], "景点必须来自真实 POI 返回，不能凭空出现"
    first = itinerary["days"][0]["spots"][0]
    assert first["address"] == "故宫博物院路 1 号", "地址来自高德"
    assert first["rating"] == 4.5
    assert first["duration_min"] == 120 and first["duration_source"] == "type_default", (
        "游览时长是估算，必须标明来源"
    )
    assert itinerary["weather"]["text"] == "晴"
    assert itinerary["note"] == "带老人，不要爬坡", "自由文本如实记录"


async def test_every_spot_traces_back_to_a_tool_response() -> None:
    """证伪性检查：返回的景点集合必须**恰好等于**假 POI 给的集合。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    itinerary = response.json()["itinerary"]
    returned = {spot["name"] for day in itinerary["days"] for spot in day["spots"]}
    assert returned == {"故宫博物院", "景山公园"}


# --------------------------------------------------------------- 拒绝能力


async def test_missing_city_is_rejected_with_readable_chinese() -> None:
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body(city=""))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str), "必须是给人看的一句话，不是 pydantic 的错误数组"
    assert "城市" in detail
    assert "Input should" not in detail, "界面里不能冒出英文校验文案"


async def test_reversed_dates_are_rejected() -> None:
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body(start_date="2099-03-05", end_date="2099-03-01"))
    assert response.status_code == 400
    assert "日期" in response.json()["detail"]


async def test_absurd_day_count_is_rejected_not_looped() -> None:
    """365 天这种输入过去会生成一份循环取用的假行程，现在必须被拒绝。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body(start_date="2099-01-01", end_date="2099-12-31"))
    assert response.status_code == 400
    assert "最多 30 天" in response.json()["detail"]


async def test_past_start_date_is_rejected() -> None:
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body(start_date="2020-01-01", end_date="2020-01-03"))
    assert response.status_code == 400
    assert "不能早于今天" in response.json()["detail"]


async def test_rejected_request_never_calls_any_tool() -> None:
    """校验必须先于任何外部调用——别把配额花在一个注定被拒的请求上。"""
    app = build_app()
    async with make_client(app) as client:
        await client.post("/api/trip", json=_body(city=""))
    assert app.state.registry.get("geocode_city").calls == []
    assert app.state.route_tool.calls == []


# --------------------------------------------------------------- 失败语义


async def test_geocode_failure_returns_failed_with_no_spots() -> None:
    """这是本次改造的核心断言：拿不到坐标时**一个景点都不能有**。"""
    app = build_app(
        geocode=ToolResult(ok=False, error="高德没有找到「火星」对应的坐标", tool_name="geocode_city")
    )
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body(city="火星"))
    assert response.status_code == 200, "业务失败仍是 200，但内容必须是显式的 failed"
    itinerary = response.json()["itinerary"]
    assert itinerary["status"] == "failed"
    assert all(day["spots"] == [] for day in itinerary["days"]), "失败时不能渲染出任何景点"
    assert any("火星" in reason or "坐标" in reason for reason in itinerary["degraded_reasons"])


async def test_poi_failure_returns_failed_not_empty_success() -> None:
    app = build_app(poi=PoiRouter(spots=ToolResult(ok=False, error="高德周边搜索失败", tool_name="find_nearby_places")))
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    itinerary = response.json()["itinerary"]
    assert itinerary["status"] == "failed"
    assert all(day["spots"] == [] for day in itinerary["days"])


async def test_weather_failure_is_degraded_and_spots_survive() -> None:
    app = build_app(weather=ToolResult(ok=False, error="和风天气服务端错误", tool_name="get_weather"))
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    itinerary = response.json()["itinerary"]
    assert itinerary["status"] == "degraded", "有降级原因就不能谎称 ok"
    assert itinerary["weather"] is None, "拿不到天气就不能编一个"
    assert sum(len(day["spots"]) for day in itinerary["days"]) == 2, "真实景点必须保留"


async def test_restaurant_failure_is_degraded_and_recorded() -> None:
    app = build_app(poi=PoiRouter(meals=ToolResult(ok=False, error="附近没有搜到餐厅", tool_name="find_nearby_places")))
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    itinerary = response.json()["itinerary"]
    assert itinerary["status"] == "degraded"
    assert any("餐厅" in reason for reason in itinerary["degraded_reasons"])


async def test_missing_tool_is_a_500_not_a_silent_pass() -> None:
    """装配出问题时必须报出来，而不是跳过那一步悄悄产出缺东西的行程。"""
    app = build_app(omit="find_nearby_places")
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    assert response.status_code == 500
    assert "工具" in response.json()["detail"]


# --------------------------------------------------------------- 读取与持久


async def test_get_trip_returns_the_same_itinerary() -> None:
    """前端刷新后要能按 trip_id 恢复，而不是「行程没了、得重新生成」。"""
    app = build_app()
    async with make_client(app) as client:
        created = await client.post("/api/trip", json=_body())
        trip_id = created.json()["trip_id"]
        fetched = await client.get(f"/api/trip/{trip_id}")
    assert fetched.status_code == 200
    assert fetched.json()["itinerary"] == created.json()["itinerary"]


async def test_get_unknown_trip_is_a_readable_404() -> None:
    app = build_app()
    async with make_client(app) as client:
        response = await client.get("/api/trip/does-not-exist")
    assert response.status_code == 404
    assert "找不到这份行程" in response.json()["detail"]


async def test_generated_trip_is_stored() -> None:
    store = TripStore()
    app = build_app(store=store)
    async with make_client(app) as client:
        created = await client.post("/api/trip", json=_body())
    assert store.get(created.json()["trip_id"]) is not None


async def test_two_generations_do_not_overwrite_each_other() -> None:
    """同一 session 连生成两次要留下两份（「编辑行程」的前提）。"""
    store = TripStore()
    app = build_app(store=store)
    async with make_client(app) as client:
        first = await client.post("/api/trip", json=_body())
        second = await client.post("/api/trip", json=_body(end_date="2099-03-03"))
    ids = {first.json()["trip_id"], second.json()["trip_id"]}
    assert len(ids) == 2
    assert store.get(first.json()["trip_id"]) is not None
    assert len(store.get(second.json()["trip_id"]).days) == 3


async def test_itinerary_carries_evidence_for_attribution() -> None:
    """行程变差时要能回答是哪一步出的问题——归因记录必须随行程返回。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())
    evidence = response.json()["itinerary"]["evidence"]
    for key in ("geocode", "weather", "poi", "restaurants"):
        assert key in evidence, f"缺少 {key} 的归因记录"
    assert evidence["geocode"]["ok"] is True


async def test_preferences_are_passed_through_and_used_for_one_extra_search() -> None:
    poi = PoiRouter()
    app = build_app(poi=poi)
    async with make_client(app) as client:
        await client.post("/api/trip", json=_body(preferences=["历史文化", "美食"]))
    keyword_calls = [call for call in poi.calls if call.get("keywords")]
    assert len(keyword_calls) == 1, "偏好只用 1 个关键词追加检索（省配额）"
    assert keyword_calls[0]["keywords"] == "名胜"
    assert any(str(call.get("types")) == "050000" for call in poi.calls), "必须有餐饮类型检索"


# --------------------------------------------------- 住宿与配图兜底（第 43 步）


async def test_hotels_come_from_a_real_lodging_search() -> None:
    """住宿候选必须来自 types=100000 的真实检索，并带上高德给的字段。"""
    poi = PoiRouter()
    app = build_app(poi=poi)
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())

    itinerary = response.json()["itinerary"]
    assert any(str(call.get("types")) == "100000" for call in poi.calls), "缺住宿类型检索"
    hotels = [hotel for day in itinerary["days"] for hotel in day["hotels"]]
    assert hotels, "应返回住宿候选"
    assert hotels[0]["name"] == "某酒店"
    assert hotels[0]["rating"] == 4.5, "评分是高德给的真实值"
    assert hotels[0]["source"] == "amap_poi"


async def test_hotels_never_expose_price_or_star() -> None:
    """高德不给房价与星级——返回体里不许有这两个键（不是留空，是没有）。"""
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())

    for day in response.json()["itinerary"]["days"]:
        for hotel in day["hotels"]:
            assert "price" not in hotel, "不该有价格字段"
            assert "star" not in hotel, "不该有星级字段"


async def test_spots_carry_amap_photos_for_the_fallback() -> None:
    """景点要带上高德自带照片，前端才能在 Unsplash 拿不到时退回它。"""
    spots = _ok("find_nearby_places", {"places": [
        dict(_place("故宫博物院"), amap_photos=["https://store.is.autonavi.com/showpic/aaa"]),
    ]})
    app = build_app(poi=PoiRouter(spots=spots))
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())

    found = [spot for day in response.json()["itinerary"]["days"] for spot in day["spots"]]
    assert found and found[0]["amap_photos"] == ["https://store.is.autonavi.com/showpic/aaa"]


async def test_missing_hotels_degrade_the_trip_instead_of_failing_it() -> None:
    """住宿失败是非关键项：行程照出，但状态必须是 degraded 并说明原因。"""
    poi = PoiRouter(hotels=ToolResult(
        ok=False, error="附近没有搜到住宿", tool_name="find_nearby_places"
    ))
    app = build_app(poi=poi)
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())

    itinerary = response.json()["itinerary"]
    assert itinerary["status"] == "degraded"
    assert all(day["hotels"] == [] for day in itinerary["days"])
    assert any("住宿" in reason for reason in itinerary["degraded_reasons"])
    assert sum(len(day["spots"]) for day in itinerary["days"]) == 2, "景点必须保留"

async def test_a_timed_out_pipeline_returns_504_with_a_readable_reason(monkeypatch) -> None:
    """第 70 步：总预算到了就中止，返回 504 + 人话，而不是让用户一直干等。

    以前只有"每次调用各自的超时与重试"：最坏情况（每个都重试）会串起来拖很久，
    用户看不出在等什么，运维也说不清 p95。
    """

    async def boom(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr("api.trip_routes.build_itinerary", boom)
    app = build_app()
    async with make_client(app) as client:
        response = await client.post("/api/trip", json=_body())

    assert response.status_code == 504
    detail = response.json()["detail"]
    assert "秒" in detail, detail
    assert "再试" in detail, "要给下一步怎么做，而不是只报个错误码"
