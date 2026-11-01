"""出行窗口的高德地图：给前端发配置，并在 `/_AMapService` 上代理高德的数据接口。

安全密钥只在服务端补，前端只拿到 key；代理只放行 `v3/`、`v4/`；没配 key 时地图不亮。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from core.config import get_settings
from tools.route_tool import MODE_URLS

logger = logging.getLogger(__name__)

router = APIRouter()

AMAP_HOST = "https://restapi.amap.com"
#: 静态图（第 226 步）：一次普通 HTTPS GET，不需要 jscode
STATICMAP_URL: Final[str] = "https://restapi.amap.com/v3/staticmap"
#: 底图样式的上游（第 231 步）。**必须和 restapi 分开**：
#: 官方 Nginx 示例就把 `/_AMapService/v4/map/styles` 单独指到 `webapi.amap.com`。
#: 指错会怎样（实测，2026-10-30）：restapi 回 `{"errcode":10009,"errmsg":"USERKEY_PLAT_NOMATCH"}`
#: —— 因为我们的 key 是「Web 端(JS API)」类型，restapi 不认。样式拿不到 → 底图空白，
#: 但点位、编号全都正常，控制台也**不报错**（HTTP 200 的 JSON 错误）。这就是"白板"的真因。
AMAP_STYLE_HOST = "https://webapi.amap.com"
#: 只转发数据接口；`v3`/`v4` 是高德版本号，其余路径（含静态图、脚本）一律拒绝
ALLOWED_PREFIXES = ("v3/", "v4/")
#: 要往 `webapi.amap.com` 转发的路径前缀（底图样式/自定义地图）
STYLE_PREFIXES = ("v4/map/styles",)
TIMEOUT_SECONDS = 8.0
#: 高德数据接口的 JSON 都很小，超过这个大小说明拿到的不是预期内容
MAX_BYTES = 2 * 1024 * 1024


@router.get("/api/map/config")
async def map_config() -> dict[str, Any]:
    """前端渲染地图前先问这里：能不能用、用哪把 key、请求往哪打。

    key 是可以给前端的（它必然出现在 JS API 的 script 地址里）；
    **安全密钥不会出现在任何响应里**，它只在 `_proxy` 里被附加到转发的请求上。

    **为什么路径是 `/api/map/...` 而不是 `/api/trip/map-config`**（第 229 步）：
    后者会被先注册的 `GET /api/trip/{trip_id}` 吃掉 —— FastAPI 按注册顺序匹配，
    `{trip_id}` 把 `map-config` 当成一个行程 id，于是这个接口永远回"找不到这份行程"，
    前端 `loadMapConfig()` 拿到 404 就按"没配置"处理，地图卡片**永远不出现**。
    这个 bug 在真窗口里表现为"图呢？"，而单元测试全绿 —— 因为测试是直接调函数，
    没有走一遍真实路由表。守卫见 `tests/test_route_shadowing.py`。
    """
    settings = get_settings()
    key = settings.gaode_js_key.strip()
    code = settings.gaode_js_security_code.strip()
    enabled = bool(key and code)
    return {
        "enabled": enabled,
        "key": key if enabled else "",
        # 同源相对路径：前端把它交给 _AMapSecurityConfig.serviceHost，
        # JS API 会把所有数据请求拼在它后面（形如 /_AMapService/v3/direction/driving）
        "service_host": "/_AMapService",
        "note": "" if enabled else "还没配高德 JS API 的 key 与安全密钥",
    }


async def _fetch(url: str) -> tuple[bytes, str]:
    """把请求转给高德。单独抽出来是为了测试能替换掉（不该在单测里真联网）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(url)
    return response.content, response.headers.get("content-type", "application/json")


@router.get("/_AMapService/{path:path}")
async def _proxy(path: str, request: Request) -> Response:
    """高德 JS API 的数据代理：补上 `jscode`（和 key）再转发。"""
    settings = get_settings()
    key = settings.gaode_js_key.strip()
    code = settings.gaode_js_security_code.strip()
    if not (key and code):
        raise HTTPException(status_code=503, detail="还没配高德 JS API 的 key 与安全密钥")

    # 白名单：高德只会请求 v3/ v4 开头的接口；其它路径直接 404，
    # 免得这条路由被当成通用代理用
    if not path.startswith(ALLOWED_PREFIXES):
        raise HTTPException(status_code=404, detail="只代理高德的数据接口（v3/ v4/）")

    # 底图样式走另一台服务器（第 231 步）：这台认「Web 端(JS API)」的 key，
    # restapi 不认（实测 USERKEY_PLAT_NOMATCH）。走错 → 白板。
    host = AMAP_STYLE_HOST if path.startswith(STYLE_PREFIXES) else AMAP_HOST

    params: dict[str, str] = {name: value for name, value in request.query_params.items()}
    # 前端可以传 key，但**不允许**覆盖 jscode —— 否则就绕过了"密钥只在服务端"的前提
    params["key"] = key
    params["jscode"] = code
    url = f"{host}/{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    try:
        body, content_type = await _fetch(url)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="高德地图没有及时返回") from None
    except httpx.HTTPError as error:
        logger.warning("高德地图代理失败 %s：%s", path, error)
        raise HTTPException(status_code=502, detail="高德地图连不上") from None

    if len(body) > MAX_BYTES:
        raise HTTPException(status_code=502, detail="高德地图返回的内容过大")
    return Response(content=body, media_type=content_type)


# ------------------------------------------------------- 静态路线图（第 226 步）
#
# 为什么另开一条静态图的路，而不是让 JS API 画：
#
# 1. **聊天里的图不需要交互**。用户要的是「我说明天从南京到上海，你把路画出来」，
#    一张 PNG 就够；交互图（第 224 步）在「计划概览」里，两者不冲突。
# 2. **静态图用 Web 服务 key，不需要 jscode**。它是一次普通 HTTPS GET，
#    也就不存在"密钥怎么不暴露给前端"的问题 —— 前端只请求 `/api/map/route.png`。
# 3. **导出 .md 时能直接嵌进去**（base64），换成 JS 画的地图就导不出来了。
#
# 代价（实测，2026-10-30）：每张图 340 KB / 1500×840（scale=2），
# 每次 = 1 次 Web 服务调用。所以这里加了一层内存缓存：
# 同一条路线 6 小时内再来就不重复调高德（路线本来一天也变不了几次）。

#: 一张图上最多画多少个折线点。实测 1180 个点 → URL 29K 字符（高德能渲染，
#: 但传输和日志都难看）；抽到 180 个，肉眼分不出区别，URL 降到 3.8K。
MAX_POINTS: Final[int] = 180
#: 静态图尺寸（高德按 `宽*高` 传；scale=2 出双倍像素，显示更清楚）
IMAGE_SIZE: Final[str] = "680*420"
IMAGE_SCALE: Final[str] = "2"
CACHE_TTL_SECONDS: Final[float] = 6 * 3600.0
CACHE_MAX_ENTRIES: Final[int] = 64
#: 同一条路线 6 小时内不变：让浏览器也缓存，省一次往返
CACHE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": f"private, max-age={int(CACHE_TTL_SECONDS)}"
}
#: 路线数据点缓存：键是 (mode, origin, destination)
_IMAGE_CACHE: dict[tuple[str, str, str], tuple[float, bytes]] = {}

COORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"^-?\d{1,3}(?:\.\d{1,7})?,-?\d{1,3}(?:\.\d{1,7})?$")
#: 静态图只画这几种；公交（transit）的线是高德另算的，不画
ROUTE_MODES: Final[dict[str, str]] = {
    "driving": MODE_URLS["driving"],
    "walking": MODE_URLS["walking"],
    "bicycling": MODE_URLS["bicycling"],
}
#: 每种通行方式在图上的颜色（蓝/橙/绿，和「计划概览」里的按天配色错开）
MODE_COLORS: Final[dict[str, str]] = {
    "driving": "0x2E7CF6",
    "walking": "0xF2912B",
    "bicycling": "0x18C36B",
}


def _polyline_points(payload: dict[str, Any]) -> list[str]:
    """从高德路线响应里抽折线点（`lon,lat;lon,lat;…`）。

    v3/v5 是 `route.paths[0].steps[].polyline`，v4 骑行是 `data.paths[0].steps[].polyline`：
    两种壳都试，是因为骑行那个端点（第 223 步）确实不在 v3 家族里。
    """
    for body in (payload.get("route"), payload.get("data")):
        paths = body.get("paths") if isinstance(body, dict) else None
        if not isinstance(paths, list) or not paths:
            continue
        steps = paths[0].get("steps") if isinstance(paths[0], dict) else None
        points: list[str] = []
        for step in steps or []:
            line = step.get("polyline") if isinstance(step, dict) else None
            if isinstance(line, str):
                points.extend([point for point in line.split(";") if point])
        if points:
            return points
    return []


def _thin(points: list[str], limit: int = MAX_POINTS) -> list[str]:
    """等距抽稀，但**一定保留首尾点** —— 抽掉终点，图上就画不到目的地。"""
    if len(points) <= limit:
        return points
    stride = max(1, len(points) // limit)
    thinned = points[::stride]
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


def _staticmap_url(
    *, key: str, origin: str, destination: str, points: list[str], mode: str
) -> str:
    path = f"6,{MODE_COLORS.get(mode, '0x2E7CF6')},1,,:" + ";".join(points)
    markers = f"mid,,A:{origin}|mid,,B:{destination}"
    params = {
        "key": key,
        "size": IMAGE_SIZE,
        "scale": IMAGE_SCALE,
        "paths": path,
        "markers": markers,
    }
    return f"{STATICMAP_URL}?{urlencode(params)}"


def _cache_get(cache_key: tuple[str, str, str]) -> bytes | None:
    hit = _IMAGE_CACHE.get(cache_key)
    if hit is None:
        return None
    expires_at, payload = hit
    if time.monotonic() > expires_at:
        _IMAGE_CACHE.pop(cache_key, None)
        return None
    return payload


def _cache_put(cache_key: tuple[str, str, str], payload: bytes) -> None:
    # 简单的容量上限：超了就丢最早插入的那条（dict 保持插入顺序），
    # 不用 OrderedDict —— 这里不是热路径，够用且少一层依赖。
    if len(_IMAGE_CACHE) >= CACHE_MAX_ENTRIES:
        oldest = next(iter(_IMAGE_CACHE), None)
        if oldest is not None:
            _IMAGE_CACHE.pop(oldest, None)
    _IMAGE_CACHE[cache_key] = (time.monotonic() + CACHE_TTL_SECONDS, payload)


@router.get("/api/map/route.png")
async def route_image(
    origin: str = Query(default="", description="起点 `经度,纬度`"),
    destination: str = Query(default="", description="终点 `经度,纬度`"),
    mode: str = Query(default="driving", description="driving / walking / bicycling"),
) -> Response:
    """把一段路线画成 PNG（前端直接 `<img src>`，导出 .md 时再转 base64）。

    为什么后端代取而不是前端直连高德：静态图的 URL 里带 key，前端拼就等于把 key
    写死在前端；而且高德那条接口没有 CORS 头，前端根本取不到字节。
    """
    if not (COORD_PATTERN.match(origin) and COORD_PATTERN.match(destination)):
        raise HTTPException(status_code=400, detail="起点和终点要写成「经度,纬度」")
    if mode not in ROUTE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的通行方式：{mode!r}（只支持 {'/'.join(ROUTE_MODES)}）",
        )

    cache_key = (mode, origin, destination)
    cached = _cache_get(cache_key)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=CACHE_HEADERS)
    settings = get_settings()
    key = settings.gaode_api_key.strip()
    if not key:
        raise HTTPException(status_code=503, detail="还没配高德 Web 服务 key，画不了路线图")

    route_url = ROUTE_MODES[mode]
    query = {"origin": origin, "destination": destination, "key": key}
    try:
        payload_raw, _ = await _fetch(f"{route_url}?{urlencode(query)}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="高德路线没有及时返回") from None
    except httpx.HTTPError as error:
        logger.warning("路线图取路线失败 %s：%s", mode, error)
        raise HTTPException(status_code=502, detail="高德路线连不上") from None

    try:
        payload = json.loads(payload_raw.decode("utf-8", "replace"))
    except ValueError:
        raise HTTPException(status_code=502, detail="高德路线返回的不是 JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="高德路线返回的结构不认识")

    points = _thin(_polyline_points(payload))
    if not points:
        # 把高德自己的错误话术带上，比"画不出来"有用得多
        reason = payload.get("info") or payload.get("errmsg") or "没有折线数据"
        raise HTTPException(status_code=502, detail=f"高德没给出这条路线的折线：{reason}")

    try:
        image, content_type = await _fetch(
            _staticmap_url(
                key=key, origin=origin, destination=destination, points=points, mode=mode
            )
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="高德静态图没有及时返回") from None
    except httpx.HTTPError as error:
        logger.warning("路线图取图片失败：%s", error)
        raise HTTPException(status_code=502, detail="高德静态图连不上") from None

    if not content_type.startswith("image"):
        # 高德出错时回的是 JSON（如 `{"status":"0","info":"INVALID_USER_KEY"}`）
        raise HTTPException(
            status_code=502,
            detail="高德没有返回图片：" + image[:200].decode("utf-8", "replace"),
        )
    _cache_put(cache_key, image)
    return Response(content=image, media_type="image/png", headers=CACHE_HEADERS)

