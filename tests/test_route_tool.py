"""高德路线工具：驾车/步行/公交三种口径的参数、响应结构、缓存键与降级文案都要钉住。
"""

from __future__ import annotations

import httpx
import pytest

from core.config import Settings
from tools.route_tool import (
    BICYCLING_URL,
    DRIVING_URL,
    MODE_URLS,
    TRANSIT_URL,
    WALKING_URL,
    RouteTool,
    road_names,
)

#: 起点/终点（杭州一带）。工具对外统一用 lat/lon，内部转成高德的 "经度,纬度"。
O_LAT, O_LON = 30.246566, 120.209903
D_LAT, D_LON = 30.237273, 120.172906


def make_settings(**overrides) -> Settings:
    base = {"gaode_api_key": "test-gaode-key"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_tool(handler, **kwargs) -> RouteTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RouteTool(
        client=client,
        settings=make_settings(),
        retry_backoff=0,
        **kwargs,
    )


def paths_body(distance: str = "4872", duration: str = "3898", **extra) -> dict:
    first = {"distance": distance, "duration": duration, "steps": [{"instruction": "走"}]}
    first.update(extra)
    return {"status": "1", "info": "ok", "infocode": "10000", "route": {"paths": [first]}}


def bicycling_body(distance: str = "4623", duration: str = "1110", **extra) -> dict:
    """高德 v4 骑行的真实形状：`{"errcode": 0, "data": {"paths": [...]}}`，没有 status。"""
    first = {
        "distance": distance,
        "duration": duration,
        "steps": [
            {"road": "银泰路", "instruction": "向东南骑行64米左转"},
            {"road": "干将西路辅路", "instruction": "向东骑行80米左转"},
            {"road": "银泰路", "instruction": "重复路名要去重"},
        ],
    }
    first.update(extra)
    return {"errcode": 0, "errmsg": "OK", "data": {"paths": [first]}}


def transit_body() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "route": {
            "distance": "4587",
            "taxi_cost": "18",
            "transits": [
                {
                    "cost": "2.0",
                    "duration": "2127",
                    "distance": "4600",
                    "walking_distance": "700",
                    "segments": [{"bus": {"buslines": []}}, {"bus": {"buslines": []}}],
                }
            ],
        },
    }


# ------------------------------------------------------------ 三种口径的端点


async def test_driving_hits_the_driving_endpoint():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=paths_body(taxi_cost="18"))

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="driving")

    assert result.ok is True
    assert captured["url"].startswith(DRIVING_URL)
    assert captured["params"]["strategy"] == "0"
    assert result.data["mode"] == "driving"
    assert result.data["distance_m"] == 4872
    assert result.data["duration_s"] == 3898
    assert result.data["taxi_cost"] == 18.0


async def test_walking_hits_the_walking_endpoint():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=paths_body())

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="walking")

    assert result.ok is True
    assert captured["url"].startswith(WALKING_URL)
    assert result.data["mode"] == "walking"
    # 步行接口不需要 strategy/extensions，多传反而可能被拒
    assert "strategy" not in captured["params"]
    assert "show_fields" not in captured["params"]
    assert "步行" in result.content


async def test_transit_hits_the_transit_endpoint_with_city_codes():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=transit_body())

    tool = make_tool(handler)
    result = await tool.run(
        O_LAT, O_LON, D_LAT, D_LON, mode="transit", city_adcode="330100"
    )

    assert result.ok is True
    assert captured["url"].startswith(TRANSIT_URL)
    assert captured["params"]["city"] == "330100", "公交必须给起点城市"
    assert captured["params"]["cityd"] == "330100", "公交必须给终点城市"


async def test_transit_parses_the_transit_shape_not_the_paths_shape():
    """公交的答案在 route.transits[]，和 paths[] 完全不同的结构。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=transit_body())

    tool = make_tool(handler)
    result = await tool.run(
        O_LAT, O_LON, D_LAT, D_LON, mode="transit", city_adcode="330100"
    )

    data = result.data or {}
    assert data["mode"] == "transit"
    assert data["distance_m"] == 4600
    assert data["duration_s"] == 2127
    assert data["fare"] == 2.0, "票价是高德给的真实值"
    assert data["walking_distance_m"] == 700
    assert data["transfers"] == 1, "两段 = 换乘一次"
    assert "公交" in result.content and "票价" in result.content


# ------------------------------------------------------------ 参数与失败语义


async def test_transit_without_adcode_fails_loudly_instead_of_guessing():
    """拿不到 adcode 就明确失败，**不能**静默按驾车算——那会让口径悄悄变掉。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("缺 adcode 时不该发请求")

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="transit")

    assert result.ok is False
    assert "adcode" in (result.error or "")


async def test_unknown_mode_is_rejected_without_a_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("未知通行方式不该发请求")

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="flying")

    assert result.ok is False
    assert "driving" in (result.error or "") and "walking" in (result.error or "")


@pytest.mark.parametrize(
    "infocode, expected",
    [
        ("10001", "GAODE_API_KEY"),
        ("10029", "配额"),
        ("20800", "Web 服务"),
    ],
)
async def test_amap_error_codes_become_readable_hints(infocode: str, expected: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "0", "info": "bad", "infocode": infocode}
        )

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON)

    assert result.ok is False
    assert expected in (result.error or ""), result.error


async def test_missing_key_is_a_configuration_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("没 key 时不该发请求")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = RouteTool(client=client, settings=make_settings(gaode_api_key=""), retry_backoff=0)

    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON)

    assert result.ok is False
    assert "GAODE_API_KEY" in (result.error or "")


# ------------------------------------------------------------------ 缓存键


async def test_cache_key_separates_the_three_modes():
    """同一个 OD 对，驾车/步行/公交是**三份答案**，缓存不能共用一格。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=paths_body())

    tool = make_tool(handler)
    await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="driving")
    await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="driving")
    assert calls == 1, "同方式同 OD 应命中缓存"

    await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="walking")
    assert calls == 2, "换了方式必须重新请求，不能拿驾车的缓存当步行"


def test_mode_urls_cover_exactly_the_supported_modes():
    """钉住支持面：第 223 步加上了骑行（v4，响应壳不同），支持面就是这四种。"""
    assert set(MODE_URLS) == {"driving", "walking", "transit", "bicycling"}
    assert MODE_URLS["bicycling"].endswith("/v4/direction/bicycling"), "骑行只在 v4"

# ------------------------------------------------- 骑行（第 223 步）与途经道路


async def test_bicycling_hits_the_v4_endpoint_and_parses_its_own_shape():
    """骑行在 v4，返回里没有 status、用 errcode；解析必须走另一条分支。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=bicycling_body())

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="bicycling")

    assert result.ok, result.error
    assert captured["url"].startswith(BICYCLING_URL)
    assert "extensions" not in captured["url"], "v4 骑行不认 extensions 参数"
    assert result.data["mode"] == "bicycling"
    assert result.data["distance_m"] == 4623
    assert result.data["roads"] == ["银泰路", "干将西路辅路"], "重复路名要去重"
    assert "骑行约" in (result.content or "") and "银泰路" in (result.content or "")


async def test_bicycling_error_code_becomes_a_readable_hint():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 30001, "errmsg": "超出骑行范围"})

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="bicycling")

    assert result.ok is False
    assert "30001" in (result.error or "") and "骑行" in (result.error or "")


async def test_driving_asks_for_details_so_roads_exist():
    """驾车要 `extensions=all` 才带 steps；`base` 模式下没有途经道路可显示。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=paths_body(
                tolls="52",
                taxi_cost="310",
                steps=[{"road": "三香路"}, {"road": "西环路"}, {"road": "三香路"}],
            ),
        )

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="driving")

    assert captured["params"].get("extensions") == "all"
    assert result.data["roads"] == ["三香路", "西环路"]
    content = result.content or ""
    assert "过路费约 52 元" in content, "有高速费就必须说出来"
    assert "途经 三香路 → 西环路" in content


async def test_zero_toll_is_not_reported():
    """市内驾车 `tolls` 是 0；写成"过路费 0 元"只会让模型去解释这个 0。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=paths_body(tolls="0"))

    tool = make_tool(handler)
    result = await tool.run(O_LAT, O_LON, D_LAT, D_LON, mode="driving")

    assert "过路费" not in (result.content or "")


def test_road_names_are_bounded_and_deduped():
    steps = [{"road": "路" * index} for index in range(1, 8)] + [{"road": "路" * 1}]
    names = road_names(steps)

    assert names == ["路", "路" * 2, "路" * 3], "最多三条，按顺序、去重、截断"
    assert road_names(None) == []
    assert road_names([{"road": ""}, "不是字典", {"instruction": "没有 road"}]) == []
