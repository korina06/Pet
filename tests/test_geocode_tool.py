"""地理编码工具：坐标顺序、HTTP 200 里的业务错误码、缺 key 与查不到时返回失败而不是抛异常。
"""

from __future__ import annotations

import httpx

from core.config import Settings
from tools.geocode_tool import GeocodeTool


def make_settings(**overrides) -> Settings:
    base = {"gaode_api_key": "test-gaode-key"}
    base.update(overrides)
    return Settings(**base)


def make_tool(handler, **kwargs) -> GeocodeTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeocodeTool(
        client=client,
        settings=make_settings(**kwargs.pop("settings_overrides", {})),
    )


def ok_body(location: str = "116.410000,39.920000") -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "1",
        "geocodes": [
            {
                "formatted_address": "北京市",
                "province": "北京市",
                "city": "北京市",
                "district": "东城区",
                "location": location,
            }
        ],
    }


async def test_sends_city_as_address_param():
    """地名要以 address 参数发出去，key 必须带上。"""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        captured["url"] = str(request.url).split("?")[0]
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    result = await tool.run(city="杭州")

    assert result.ok is True, result.error
    assert captured["params"]["address"] == "杭州"
    assert captured["params"]["key"] == "test-gaode-key"
    assert "geocode/geo" in captured["url"], f"请求地址不对：{captured['url']}"


async def test_parses_longitude_first():
    """高德返回 "经度,纬度"，必须还原成 lat=39.92 / lon=116.41。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body("116.410000,39.920000"))

    tool = make_tool(handler)
    result = await tool.run(city="北京")

    assert result.ok is True, result.error
    assert result.content is not None
    assert "lat=39.92" in result.content, result.content
    assert "lon=116.41" in result.content, result.content


async def test_missing_key_returns_readable_error():
    """没配 key 时给出可操作提示，而不是崩掉。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("缺 key 时不该发出请求")

    tool = make_tool(handler, settings_overrides={"gaode_api_key": ""})
    result = await tool.run(city="北京")

    assert result.ok is False
    assert "GAODE_API_KEY" in (result.error or "")


async def test_business_error_code_is_reported():
    """高德把错误放在 HTTP 200 的 body 里，必须识别。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
        )

    tool = make_tool(handler)
    result = await tool.run(city="北京")

    assert result.ok is False
    assert "key" in (result.error or "").lower(), result.error
    assert "10001" in (result.error or ""), "应带上 infocode，便于排查"


async def test_no_result_is_not_an_exception():
    """查不到地点时返回 ok=False 并提示换个写法。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "1", "info": "OK", "infocode": "10000", "count": "0", "geocodes": []},
        )

    tool = make_tool(handler)
    result = await tool.run(city="一个不存在的地方")

    assert result.ok is False
    assert "没有找到" in (result.error or "")


async def test_empty_city_is_rejected_without_network():
    """空地名直接拒绝，不浪费一次请求。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空地名时不该发出请求")

    tool = make_tool(handler)
    result = await tool.run(city="   ")

    assert result.ok is False
    assert "地名" in (result.error or "")


async def test_network_error_is_folded_into_result():
    """网络异常不该抛出，而应变成可回灌的 ToolResult。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    tool = make_tool(handler)
    result = await tool.run(city="北京")

    assert result.ok is False
    assert "网络" in (result.error or ""), result.error


async def test_tool_is_registered_but_hidden_from_menu():
    """它是"流程里的工具"，不该出现在轮盘菜单里。"""
    from tools.registry import build_default_registry

    registry = build_default_registry()
    assert "geocode_city" in registry.names(), "地理编码应注册进默认注册表"
    assert "geocode_city" not in registry.menu_names(), "它不该有轮盘入口"


async def test_same_city_is_cached_and_not_requested_twice():
    """同名城市第二次直接命中缓存（第 70 步）。

    地理编码结果几乎不变，而出行每次生成都要查一次城市坐标 ——
    这是**可以完全省掉**的一次往返。
    """
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ok_body())

    tool = make_tool(handler)
    first = await tool.run(city="北京")
    second = await tool.run(city="北京")

    assert first.ok is True and second.ok is True
    assert calls == 1, f"同一地名应命中缓存，实际请求了 {calls} 次"
    assert second.content == first.content, "缓存命中后结果要一致"


async def test_a_failed_geocode_is_not_cached():
    """失败**不**进缓存：把错误也缓存 7 天，等于让一个小故障变成一周不可用。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "0", "info": "bad", "infocode": "10001"})

    tool = make_tool(handler)
    await tool.run(city="北京")
    await tool.run(city="北京")

    assert calls == 2, "失败不该被缓存"


async def test_a_not_found_result_is_cached_only_briefly():
    """「查无此地」只记 120 秒，不是 7 天（评价11 P3-6）**。

    地名会变写法（"杭州西湖" vs "西湖"），把"查不到"按成功缓存放 7 天，
    等于告诉用户"换个说法也没用，除非重启进程"。
    """
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "count": "0",
                "geocodes": [],
            },
        )

    tool = make_tool(handler)
    first = await tool.run(city="不存在的地方XYZZ")
    assert first.ok is False

    stored = tool._cache["不存在的地方XYZZ"]
    assert stored[2] == 120, f"负缓存时长应当是 120 秒，实际 {stored[2]}"

    second = await tool.run(city="不存在的地方XYZZ")
    assert second.ok is False
    assert len(calls) == 1, "120 秒内应当命中缓存、不再请求"


async def test_a_qps_business_error_is_retried(monkeypatch):
    """HTTP 200 + infocode=10021 要退避重试（评价11 P3-7）。

    这类错误是"等一秒就能过"，以前被当终局失败 —— 一次出行直接丢一段数据。
    """
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
            )
        return httpx.Response(200, json=ok_body())

    # 别真的等 0.3 秒 ×2
    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    tool = make_tool(handler)
    result = await tool.run(city="杭州")

    assert result.ok is True, result.error
    assert len(calls) == 2, f"应当重试一次，实际请求 {len(calls)} 次"
