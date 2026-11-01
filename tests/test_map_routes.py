"""地图接口：配置只回 key 不回安全密钥、代理由服务端补密钥且不许前端覆盖、只转发高德数据接口。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
from fastapi import FastAPI

import api.map_routes as map_routes

KEY = "0123456789abcdef0123456789abcdef"
CODE = "fedcba9876543210fedcba9876543210"


def make_client(app: FastAPI) -> httpx.AsyncClient:
    # base_url 用 127.0.0.1：HostGuard 只放行本机名字
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    )


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(map_routes.router)
    return app


@contextmanager
def settings_with(key: str, code: str) -> Iterator[None]:
    """把 map_routes 里的 get_settings 换成固定值（不读真实 .env）。"""
    original = map_routes.get_settings
    map_routes.get_settings = lambda: SimpleNamespace(  # type: ignore[assignment]
        gaode_js_key=key, gaode_js_security_code=code
    )
    try:
        yield
    finally:
        map_routes.get_settings = original


async def test_map_config_returns_key_but_never_the_security_code() -> None:
    with settings_with(KEY, CODE):
        async with make_client(make_app()) as client:
            response = await client.get("/api/map/config")

            assert response.status_code == 200
            body: dict[str, Any] = response.json()
            assert body["enabled"] is True
            assert body["key"] == KEY
            assert CODE not in response.text, "安全密钥绝不允许出现在任何响应里"
            assert body["service_host"] == "/_AMapService"


async def test_map_config_is_disabled_when_keys_are_missing() -> None:
    with settings_with("", ""):
        async with make_client(make_app()) as client:
            response = await client.get("/api/map/config")

    body = response.json()
    assert body["enabled"] is False
    assert body["key"] == ""
    assert body["note"], "不可用时要说明原因，不要只给一个 false"


async def test_proxy_injects_jscode_and_overrides_client_supplied_values() -> None:
    seen: list[str] = []

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        seen.append(url)
        return b'{"status":"1","regeocode":{}}', "application/json"

    original = map_routes._fetch
    map_routes._fetch = fake_fetch  # type: ignore[assignment]
    try:
        with settings_with(KEY, CODE):
            async with make_client(make_app()) as client:
                response = await client.get(
                    "/_AMapService/v3/geocode/regeo",
                    params={"location": "120.1,30.2", "key": "attacker", "jscode": "leak"},
                )
    finally:
        map_routes._fetch = original

    assert response.status_code == 200
    assert response.json()["status"] == "1"
    assert len(seen) == 1
    url = seen[0]
    assert "120.1%2C30.2" in url or "120.1,30.2" in url, "原始查询参数必须带过去"
    assert f"jscode={CODE}" in url, "服务端补上的 jscode 必须进入转发请求"
    assert "jscode=leak" not in url, "前端传的 jscode 不许覆盖服务端的"
    assert f"key={KEY}" in url and "key=attacker" not in url


async def test_proxy_refuses_paths_that_are_not_data_interfaces() -> None:
    with settings_with(KEY, CODE):
        async with make_client(make_app()) as client:
            for path in ("/_AMapService/maps", "/_AMapService/v2/foo", "/_AMapService/"):
                response = await client.get(path)
                assert response.status_code == 404, path


async def test_proxy_sends_map_styles_to_webapi_not_restapi() -> None:
    """第 231 步：底图样式必须转到 `webapi.amap.com`。

    这不是"锦上添花"：实测把 styles 发到 restapi，回的是
    `{"errcode":10009,"errmsg":"USERKEY_PLAT_NOMATCH"}` —— 我们的 key 是
    「Web 端(JS API)」类型，restapi 不认。样式拿不到 = 底图空白，但点位正常、
    控制台也不报错（HTTP 200 的 JSON 错误），所以这个断言要钉死，不能靠肉眼。
    """
    seen: list[str] = []

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        seen.append(url)
        return b'{"errcode":0,"data":{}}', "application/json"

    original = map_routes._fetch
    map_routes._fetch = fake_fetch  # type: ignore[assignment]
    try:
        with settings_with(KEY, CODE):
            async with make_client(make_app()) as client:
                await client.get(
                    "/_AMapService/v4/map/styles", params={"styleid": "amap://styles/normal"}
                )
                await client.get("/_AMapService/v3/geocode/regeo", params={"location": "1,2"})
    finally:
        map_routes._fetch = original

    assert len(seen) == 2
    assert seen[0].startswith("https://webapi.amap.com/v4/map/styles?"), seen[0]
    assert seen[1].startswith("https://restapi.amap.com/v3/geocode/regeo?"), seen[1]
    for url in seen:
        assert f"jscode={CODE}" in url, "两条都要补 jscode"


async def test_proxy_says_so_when_not_configured() -> None:
    with settings_with("", ""):
        async with make_client(make_app()) as client:
            response = await client.get("/_AMapService/v3/geocode/regeo")

    assert response.status_code == 503
    assert "key" in response.json()["detail"]


# ---------------------------------------------- 静态路线图（第 226 步）
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
ORIGIN = "118.796877,32.060255"
DESTINATION = "121.472644,31.231706"


@contextmanager
def settings_for_images(api_key: str) -> Iterator[None]:
    """静态图只用 Web 服务 key（不需要 jscode）—— 这里的桩也就没有安全密钥。"""
    original = map_routes.get_settings
    map_routes.get_settings = lambda: SimpleNamespace(  # type: ignore[assignment]
        gaode_js_key="", gaode_js_security_code="", gaode_api_key=api_key
    )
    try:
        yield
    finally:
        map_routes.get_settings = original


def route_payload(points: str, *, bicycling: bool = False) -> dict[str, Any]:
    """高德路线响应的两种壳：v3/v5 是 `route.paths`，v4 骑行是 `data.paths`。"""
    body = {"paths": [{"steps": [{"polyline": points}]}]}
    return {"errcode": 0, "data": body} if bicycling else {"status": "1", "route": body}


@contextmanager
def fake_amap(route: dict[str, Any], calls: list[str]) -> Iterator[None]:
    async def fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        if "/direction/" in url:
            return json.dumps(route).encode("utf-8"), "application/json"
        return PNG, "image/png"

    original = map_routes._fetch
    map_routes._fetch = fetch  # type: ignore[assignment]
    map_routes._IMAGE_CACHE.clear()
    try:
        yield
    finally:
        map_routes._fetch = original
        map_routes._IMAGE_CACHE.clear()


async def test_route_image_draws_the_real_polyline_and_caches_it() -> None:
    calls: list[str] = []
    polyline = "118.79,32.06;119.5,31.8;121.47,31.23"
    with fake_amap(route_payload(polyline), calls), settings_for_images(KEY):
        async with make_client(make_app()) as client:
            first = await client.get(
                "/api/map/route.png",
                params={"origin": ORIGIN, "destination": DESTINATION, "mode": "driving"},
            )
            second = await client.get(
                "/api/map/route.png",
                params={"origin": ORIGIN, "destination": DESTINATION, "mode": "driving"},
            )

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.content == PNG

    # 第一次：取路线 + 取图；第二次：命中缓存，一次都不再调高德
    assert len(calls) == 2, calls
    route_url, image_url = calls
    assert "/direction/driving" in route_url, "驾车走驾车路线接口"
    assert "paths=" in image_url and "markers=" in image_url
    assert "119.5%2C31.8" in image_url or "119.5,31.8" in image_url, "折线点要进静态图"
    assert "A%3A118.796877%2C32.060255" in image_url or f"A:{ORIGIN}" in image_url
    assert second.status_code == 200 and second.content == PNG


async def test_route_image_understands_the_bicycling_envelope() -> None:
    calls: list[str] = []
    with fake_amap(route_payload("120.1,30.1;120.2,30.2", bicycling=True), calls), (
        settings_for_images(KEY)
    ):
        async with make_client(make_app()) as client:
            response = await client.get(
                "/api/map/route.png",
                params={"origin": ORIGIN, "destination": DESTINATION, "mode": "bicycling"},
            )

    assert response.status_code == 200, response.text
    assert "/v4/direction/bicycling" in calls[0], "骑行只在 v4"


async def test_route_image_rejects_bad_input() -> None:
    calls: list[str] = []
    with fake_amap(route_payload("120.1,30.1;120.2,30.2"), calls), settings_for_images(KEY):
        async with make_client(make_app()) as client:
            bad_origin = await client.get(
                "/api/map/route.png",
                params={"origin": "南京", "destination": DESTINATION},
            )
            bad_mode = await client.get(
                "/api/map/route.png",
                params={"origin": ORIGIN, "destination": DESTINATION, "mode": "flying"},
            )

    assert bad_origin.status_code == 400
    assert "经度,纬度" in bad_origin.json()["detail"]
    assert bad_mode.status_code == 400
    assert "flying" in bad_mode.json()["detail"]
    assert calls == [], "参数不合法时一次都不该调高德"


async def test_route_image_says_so_when_not_configured() -> None:
    with settings_for_images(""):
        async with make_client(make_app()) as client:
            response = await client.get(
                "/api/map/route.png", params={"origin": ORIGIN, "destination": DESTINATION}
            )

    assert response.status_code == 503
    assert "key" in response.json()["detail"]


async def test_route_image_reports_gode_failure_with_its_own_reason() -> None:
    """高德自己报错时不画图，但要把它的话带出来（否则用户只看到"画不出来"）。"""
    calls: list[str] = []
    failure = {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}
    with fake_amap(failure, calls), settings_for_images(KEY):
        async with make_client(make_app()) as client:
            response = await client.get(
                "/api/map/route.png", params={"origin": ORIGIN, "destination": DESTINATION}
            )

    assert response.status_code == 502
    assert "INVALID_USER_KEY" in response.json()["detail"]
    assert len(calls) == 1, "路线都没拿到，不该再去请求图片"


async def test_route_image_reports_an_image_endpoint_error() -> None:
    """图片接口出错时高德回 JSON；这种"拿到 200 却不是图"的情况必须变成可读错误。"""
    calls: list[str] = []

    async def fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        if "/direction/" in url:
            return json.dumps(route_payload("120.1,30.1;120.2,30.2")).encode(), "application/json"
        return b'{"status":"0","info":"DAILY_QUERY_OVER_LIMIT"}', "application/json"

    original = map_routes._fetch
    map_routes._fetch = fetch  # type: ignore[assignment]
    map_routes._IMAGE_CACHE.clear()
    try:
        with settings_for_images(KEY):
            async with make_client(make_app()) as client:
                response = await client.get(
                    "/api/map/route.png",
                    params={"origin": ORIGIN, "destination": DESTINATION},
                )
    finally:
        map_routes._fetch = original
        map_routes._IMAGE_CACHE.clear()

    assert response.status_code == 502
    assert "DAILY_QUERY_OVER_LIMIT" in response.json()["detail"]
