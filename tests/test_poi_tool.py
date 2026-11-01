"""高德 POI 工具：坐标顺序（高德要「经度,纬度」）与 HTTP 200 里的业务错误码。不联网。
"""

from __future__ import annotations

import httpx
import pytest

from core.config import Settings
from tools.poi_tool import PoiTool

LAT, LON = 39.92, 116.41


def make_settings(**overrides) -> Settings:
    base = {"gaode_api_key": "test-gaode-key"}
    base.update(overrides)
    return Settings(**base)


def make_tool(handler, **kwargs) -> PoiTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PoiTool(
        client=client,
        settings=make_settings(**kwargs.pop("settings_overrides", {})),
        retry_backoff=0,
        **kwargs,
    )


def ok_body() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "2",
        "pois": [
            {
                "name": "菖蒲河公园",
                "type": "风景名胜;公园广场;公园",
                "address": "东华门街道",
                "distance": "800",
            },
            {
                "name": "天安门广场",
                "type": "风景名胜;风景名胜;红色景区",
                "address": "东长安街",
                "distance": "1500",
            },
        ],
    }


async def test_sends_longitude_first_then_latitude():
    """高德的 location 必须是「经度,纬度」，不是「纬度,经度」。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    # 传入 wgs84，工具会先转成 gcj02（转换会略微改变数值）
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is True, result.error
    location = captured["params"]["location"]
    lon_part, lat_part = (float(x) for x in location.split(","))
    # 经度应接近 116.x，纬度接近 39.x —— 若顺序反了这里会得到 39,116
    assert 115 < lon_part < 118, f"location 第一段应是经度，实际 {lon_part}"
    assert 39 < lat_part < 41, f"location 第二段应是纬度，实际 {lat_part}"


async def test_gcj02_input_skips_conversion():
    """声明坐标本就是 gcj02 时，应原样发送，不做二次偏移。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON, coordsys="gcj02")

    assert captured["params"]["location"] == f"{LON:.6f},{LAT:.6f}"


async def test_parses_places_into_readable_text():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is True
    assert "菖蒲河公园" in result.content
    assert "天安门广场" in result.content
    assert "800 米" in result.content, "距离应格式化"
    assert "1.5 公里" in result.content, "超过 1000 米应换算成公里"
    # 分类取最后一级，更具体
    assert "公园" in result.content


async def test_cache_avoids_second_request():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON)
    await tool.run(lat=LAT, lon=LON)

    assert calls == 1, "相同参数应命中缓存"


async def test_missing_key_returns_configuration_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("没有 key 时不该发请求")

    tool = make_tool(handler, settings_overrides={"gaode_api_key": ""})
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert "GAODE_API_KEY" in (result.error or "")


@pytest.mark.parametrize(
    "infocode,expected",
    [
        ("10021", "并发配额超限"),
        ("10009", "Web 服务"),
        ("10001", "GAODE_API_KEY"),
        ("10029", "配额已用尽"),
    ],
)
async def test_amap_error_codes_become_readable_hints(infocode: str, expected: str):
    """高德把业务错误放在 HTTP 200 的响应体里，必须识别 infocode。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "0", "info": "SOME_ERROR", "infocode": infocode},
        )

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert expected in (result.error or ""), f"错误提示应包含「{expected}」"


async def test_empty_result_is_reported_readably():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "1", "info": "OK", "infocode": "10000", "pois": []}
        )

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert "没找到" in (result.error or "")


async def test_radius_and_limit_are_clamped():
    """半径与条数要被限制在接口允许范围内，避免模型传离谱的值。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON, radius=999999, limit=999)

    assert int(captured["params"]["radius"]) <= 50000
    assert int(captured["params"]["page_size"]) <= 10


async def test_keywords_switch_to_keyword_search_type():
    """给了关键词时不应再带 types，否则关键词会被类型过滤掉。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON, keywords="博物馆")

    assert captured["params"]["keywords"] == "博物馆"
    assert "types" not in captured["params"]


async def test_network_error_is_readable():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert "网络" in (result.error or "")


def test_tool_schema_contract():
    tool = PoiTool()
    schema = tool.to_openai_schema()
    assert schema["function"]["name"] == "find_nearby_places"
    assert schema["function"]["parameters"]["required"] == ["lat", "lon"]
    assert tool.label == "附近好玩"


# ---------------------------------------------- 出行需要的结构化字段（第 41/43 步）


async def test_show_fields_requests_photos_and_business():
    """第 43 步：高德自带的地点照片要给出来（Unsplash 的兜底），所以 show_fields 带 photos。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON, show_fields=True)

    assert "photos" in captured["params"]["show_fields"], "要请求照片"
    assert "business" in captured["params"]["show_fields"], "还要评分与营业时间"


async def test_without_show_fields_nothing_extra_is_requested():
    """默认不开 show_fields：聊天链路不需要这些，返回体会大一圈。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON)

    assert "show_fields" not in captured["params"]


async def test_amap_photos_are_parsed_for_the_unsplash_fallback():
    """photos[].url 逐条取出来，空项与垃圾数据丢掉，没有则空列表（不是 None）。"""
    body = ok_body()
    body["pois"][0]["photos"] = [
        {"title": "门脸", "url": "https://store.is.autonavi.com/showpic/aaa"},
        {"title": "没图", "url": ""},
        "垃圾数据",
    ]
    body["pois"][1]["photos"] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, show_fields=True)

    places = (result.data or {}).get("places") or []
    assert places[0]["amap_photos"] == ["https://store.is.autonavi.com/showpic/aaa"]
    assert places[1]["amap_photos"] == [], "没有照片就是空列表，不是 None"


async def test_typecode_location_rating_and_opentime_survive_parsing():
    """出行要用的四个字段：typecode（查时长）、location（算路线）、rating、opentime。"""
    body = ok_body()
    body["pois"][0]["typecode"] = "110101"
    body["pois"][0]["location"] = "116.410000,39.920000"
    body["pois"][0]["business"] = {"rating": "4.6", "opentime_today": "09:00-17:00"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, show_fields=True)
    place = ((result.data or {}).get("places") or [])[0]

    assert place["typecode"] == "110101"
    assert place["lat"] == 39.92 and place["lon"] == 116.41, "location 要拆成 lat/lon"
    assert place["rating"] == 4.6
    assert place["opentime"] == "09:00-17:00"


async def test_location_is_none_when_amap_does_not_give_one():
    """没有 location 时不许编一个坐标——出行会拿它去算路线。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body())   # ok_body 里没有 location

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, show_fields=True)
    place = ((result.data or {}).get("places") or [])[0]

    assert place["lat"] is None and place["lon"] is None
