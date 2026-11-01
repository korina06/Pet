"""高德 POI 周边搜索：返回附近真实地点。

对外统一用 (lat, lon)，内部转成高德的「经度,纬度」；接受 WGS84 或 GCJ-02 坐标；
只发一个请求（个人 key 并发配额低），错误给可读提示而不抛异常。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Final

import httpx

from core.config import Settings, get_settings
from core.coords import wgs84_to_gcj02
from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)

AMAP_AROUND_URL: Final[str] = "https://restapi.amap.com/v5/place/around"
DEFAULT_RADIUS: Final[int] = 5000
DEFAULT_LIMIT: Final[int] = 6
DEFAULT_TIMEOUT: Final[float] = 8.0
DEFAULT_RETRY_BACKOFF: Final[float] = 0.6
MAX_RETRIES: Final[int] = 2
CACHE_TTL: Final[float] = 600.0

#: 默认查询的类型（实测可用、且确实是"好玩的地方"）
DEFAULT_TYPES: Final[str] = "110000|110100|080000"

#: 高德错误码 -> 可读原因与下一步
_ERROR_HINTS: Final[dict[str, str]] = {
    "10001": "key 不正确或已过期，请检查 .env 里的 GAODE_API_KEY",
    "10003": "访问过于频繁，请稍后再试",
    "10009": "key 与服务类型不匹配：请确认该 key 的『服务平台』选的是 Web 服务",
    "10021": "并发配额超限（个人 key 每天有 QPS 限制），稍等几秒再试",
    "10029": "key 配额已用尽，请到高德控制台查看用量",
    "20001": "请求参数有误，请检查经纬度是否是数字、半径是否在合法范围",
    "20800": "该 key 未开通 Web 服务 API 权限",
}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_distance(value: Any) -> str:
    """高德返回的 distance 单位是米（字符串），这里转成好读的说法。"""
    meters = _to_float(value)
    if meters is None:
        return ""
    if meters < 1000:
        return f"{int(meters)} 米"
    return f"{meters / 1000:.1f} 公里"


class PoiTool(BaseTool):
    """查询指定坐标周边的景点 / 公园 / 休闲场所。"""

    name = "find_nearby_places"
    label = "附近好玩"
    intent_template = "帮我看看北京附近有什么好玩的地方。"
    #: 不在轮盘菜单展示：周边推荐由天气问答自动带出（见第 13 步）。
    #: 第 24 步起聊天框的胶囊也改为"打开出行窗口"，所以当前没有手动入口，
    #: 但接口与能力都保留——模型照常调用，出行窗口下一步也会用它。
    show_in_menu = False

    description = (
        "查询某个坐标附近值得去的地方（景点、公园广场、休闲场所等），"
        "返回真实的地点名称、类型、地址和距离。"
        "当用户问“附近有什么好玩的/好吃的/值得去的”“推荐个公园/景点”"
        "“周末去哪玩”，或者你刚查完某个城市的天气、需要给出具体去处时，调用这个工具。"
        "参数是经纬度（lat/lon，与本项目查天气工具一致，都是先纬度后经度）；"
        "如果用户没说地点，就用默认城市的坐标。"
        "radius 是搜索半径（米），默认 5000，最大 50000。"
        "不要自己编造地点名称，一律以本工具返回的结果为准。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "lat": {
                "type": "number",
                "description": "纬度（十进制度），例如北京 39.92",
            },
            "lon": {
                "type": "number",
                "description": "经度（十进制度），例如北京 116.41",
            },
            "radius": {
                "type": "integer",
                "description": "搜索半径，单位米，默认 5000，最大 50000",
                "default": DEFAULT_RADIUS,
            },
            "keywords": {
                "type": "string",
                "description": "可选关键词，例如“博物馆”“公园”。不填则按默认类型搜",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 6，最多 10",
                "default": DEFAULT_LIMIT,
            },
            "coordsys": {
                "type": "string",
                "enum": ["wgs84", "gcj02"],
                "description": (
                    "传入坐标的坐标系。默认 wgs84（和风天气返回的格式），"
                    "工具会自动转成高德需要的 gcj02；"
                    "若坐标本来就得自高德，传 gcj02 跳过转换。"
                ),
                "default": "wgs84",
            },
        },
        "required": ["lat", "lon"],
    }

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        settings: Settings | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_retries: int = MAX_RETRIES,
        cache_ttl: float = CACHE_TTL,
    ) -> None:
        self._client = client
        self._settings_override = settings
        self._timeout = timeout
        self._retry_backoff = max(0.0, retry_backoff)
        self._max_retries = max(0, max_retries)
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}

    @property
    def settings(self) -> Settings:
        return self._settings_override if self._settings_override is not None else get_settings()

    # ------------------------------------------------------------------ 缓存

    def clear_cache(self) -> None:
        self._cache.clear()

    def _cache_get(self, key: tuple) -> list[dict[str, Any]] | None:
        import time

        hit = self._cache.get(key)
        if hit is None:
            return None
        stored_at, data = hit
        if time.monotonic() - stored_at >= self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return data

    def _cache_put(self, key: tuple, data: list[dict[str, Any]]) -> None:
        import time

        self._cache[key] = (time.monotonic(), data)

    # ------------------------------------------------------------------ 入口

    async def run(
        self,
        lat: float,
        lon: float,
        radius: int = DEFAULT_RADIUS,
        keywords: str = "",
        limit: int = DEFAULT_LIMIT,
        coordsys: str = "wgs84",
        *,
        types: str = "",
        show_fields: bool = False,
        use_cache: bool = True,
    ) -> ToolResult:
        lat_f, lon_f = _to_float(lat), _to_float(lon)
        if lat_f is None or lon_f is None:
            return self._failure(
                f"经纬度必须是数字，收到 lat={lat!r}, lon={lon!r}。",
                {"lat": lat, "lon": lon},
            )

        try:
            api_key = self.settings.require_gaode_key()
        except ValueError:
            return self._failure(
                "未配置高德地图 Key（GAODE_API_KEY）：请到 https://console.amap.com/dev/key "
                "创建应用并添加 Key（服务平台选『Web 服务』），写入 .env 后重试。",
                {"lat": lat_f, "lon": lon_f},
            )

        radius_i = int(_to_float(radius) or DEFAULT_RADIUS)
        radius_i = max(100, min(radius_i, 50000))
        limit_i = int(_to_float(limit) or DEFAULT_LIMIT)
        limit_i = max(1, min(limit_i, 10))
        keyword_text = (keywords or "").strip()
        types_text = (types or "").strip()
        system = (coordsys or "wgs84").strip().lower()

        # 统一对外用 (lat, lon)；高德要的是 "经度,纬度"
        if system == "gcj02":
            g_lon, g_lat = lon_f, lat_f
        else:
            g_lon, g_lat = wgs84_to_gcj02(lon_f, lat_f)

        cache_key = (
            round(g_lon, 4), round(g_lat, 4), radius_i,
            keyword_text, limit_i, types_text, show_fields,
        )
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.debug("命中周边搜索缓存 lon=%s lat=%s", g_lon, g_lat)
                return self._success(cached, {"lat": lat_f, "lon": lon_f, "radius": radius_i})

        places, error = await self._fetch(
            api_key, g_lon, g_lat, radius_i, keyword_text, limit_i,
            types_text, show_fields,
        )
        if places is None:
            return self._failure(
                error or "高德周边搜索失败。",
                {"lat": lat_f, "lon": lon_f, "radius": radius_i},
            )
        if not places:
            return self._failure(
                f"在半径 {radius_i} 米内没找到符合条件的地点，可以换个坐标或调大半径再试。",
                {"lat": lat_f, "lon": lon_f, "radius": radius_i},
            )

        if use_cache:
            self._cache_put(cache_key, places)
        return self._success(places, {"lat": lat_f, "lon": lon_f, "radius": radius_i})

    # ------------------------------------------------------------------ 请求

    @asynccontextmanager
    async def _acquire_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            yield client

    async def _fetch(
        self,
        api_key: str,
        g_lon: float,
        g_lat: float,
        radius: int,
        keywords: str,
        limit: int,
        types: str = "",
        show_fields: bool = False,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """只发一个请求（个人 key 并发配额低，不做多请求聚合）。"""
        params: dict[str, Any] = {
            "key": api_key,
            "location": f"{g_lon:.6f},{g_lat:.6f}",  # 高德：经度,纬度
            "radius": radius,
            "page_size": limit,
            "sortrule": "weight",
        }
        # types 与 keywords 可以并存（出行的偏好检索就是"景点类型 + 关键词"）。
        # 只给 keywords 时高德做全类型关键词匹配，结果容易跑偏，所以显式给 types 更稳。
        if types:
            params["types"] = types
        elif not keywords:
            params["types"] = DEFAULT_TYPES
        if keywords:
            params["keywords"] = keywords
        if show_fields:
            # business 才返回 rating / opentime_today（第 28 步实测）；
            # photos 返回**该地点自己的照片直链**，作为 Unsplash 的兜底（第 43 步）。
            # 默认不开：会改变返回体大小，聊天链路不需要这些字段。
            params["show_fields"] = "photos,business"

        last_error = "高德周边搜索失败。"
        async with self._acquire_client() as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.get(AMAP_AROUND_URL, params=params)
                except httpx.TimeoutException:
                    last_error = (
                        f"请求高德地图超时（超过 {self._timeout:.0f} 秒），"
                        "请检查网络后重试。"
                    )
                    logger.warning("高德周边搜索超时（第 %d 次尝试）", attempt + 1)
                except httpx.HTTPError as exc:
                    last_error = (
                        f"访问高德地图失败（网络错误）：{type(exc).__name__}。"
                        "请检查网络连通性。"
                    )
                    logger.warning("高德周边搜索网络错误（第 %d 次尝试）", attempt + 1)
                else:
                    status = response.status_code
                    if 200 <= status < 300:
                        return self._parse(response)
                    if status >= 500 or status == 429:
                        last_error = f"高德服务暂时不可用（HTTP {status}），请稍后再试。"
                        logger.warning("高德返回可重试状态码 HTTP %s", status)
                    else:
                        return None, f"高德接口返回 HTTP {status}，请求未被接受。"
                if attempt < self._max_retries:
                    delay = self._retry_backoff * (2**attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break
        return None, last_error

    def _parse(self, response: httpx.Response) -> tuple[list[dict[str, Any]] | None, str | None]:
        try:
            payload = response.json()
        except ValueError:
            return None, f"高德返回的内容不是合法 JSON（HTTP {response.status_code}）。"
        if not isinstance(payload, dict):
            return None, "高德返回的内容结构异常。"

        if str(payload.get("status")) != "1":
            code = str(payload.get("infocode") or "")
            info = str(payload.get("info") or "")
            hint = _ERROR_HINTS.get(code, "请到高德控制台查看用量与权限")
            return None, f"高德返回错误 {code}（{info}）：{hint}。"

        pois = payload.get("pois") or []
        places: list[dict[str, Any]] = []
        for item in pois:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            # 高德的 location 形如 "116.410000,39.920000"（经度在前）。
            # 出行流水线必须拿真实坐标去算路线，所以这里保留；解析不出来就置 None，
            # **绝不编造一个坐标**——编出来的坐标会让路线与地图全错。
            place_lat: float | None = None
            place_lon: float | None = None
            raw_location = str(item.get("location") or "").strip()
            if "," in raw_location:
                lon_part, _, lat_part = raw_location.partition(",")
                place_lon = _to_float(lon_part)
                place_lat = _to_float(lat_part)

            business = item.get("business")
            if not isinstance(business, dict):
                business = {}
            # 只有 show_fields=business 时高德才给这两项（第 28 步实测）。
            opentime = str(
                item.get("opentime_today") or business.get("opentime_today") or ""
            ).strip()
            distance_m = _to_float(item.get("distance"))

            # 只有 show_fields=photos 时才有：该地点自己的照片直链，
            # 出行拿它当 Unsplash 的兜底。逐条只留非空 url。
            photos = item.get("photos")
            amap_photos: list[str] = []
            if isinstance(photos, list):
                for photo in photos:
                    if not isinstance(photo, dict):
                        continue
                    url = str(photo.get("url") or "").strip()
                    if url:
                        amap_photos.append(url)

            places.append(
                {
                    "name": name,
                    "type": str(item.get("type") or "").strip(),
                    # typecode 是数字分类码（如 "110101"），type 是中文分类文本。
                    # 出行用它查"游览时长默认值"，中文文本没法当键。
                    "typecode": str(item.get("typecode") or "").strip(),
                    "address": str(item.get("address") or "").strip(),
                    "distance": _fmt_distance(item.get("distance")),
                    "distance_m": int(distance_m) if distance_m is not None else None,
                    "lat": place_lat,
                    "lon": place_lon,
                    "rating": _to_float(business.get("rating")),
                    "opentime": opentime or None,
                    "amap_photos": amap_photos,
                }
            )
        return places, None

    # ------------------------------------------------------------------ 输出

    def _success(self, places: list[dict[str, Any]], arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            ok=True,
            content=self._describe(places),
            tool_name=self.name,
            arguments=arguments,
            # 出行流水线要拿真实坐标算路线、拿评分排序、拿营业时间做可行性校验，
            # 所以结构化结果一并带出；聊天链路只看 content。
            data={"places": places},
        )

    def _failure(self, error: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, error=error, tool_name=self.name, arguments=arguments)

    def _describe(self, places: list[dict[str, Any]]) -> str:
        """拼成一句句可读文本回灌给模型；只报接口给了的字段，不补充任何猜测。"""
        lines = [f"附近找到 {len(places)} 个地点："]
        for place in places:
            parts = [place["name"]]
            if place["type"]:
                # 高德的 type 是分号分隔的多级分类，取最后一级更具体
                leaf = place["type"].split(";")[-1].strip()
                if leaf:
                    parts.append(f"（{leaf}）")
            if place["address"]:
                parts.append(f"，{place['address']}")
            if place["distance"]:
                parts.append(f"，距离约 {place['distance']}")
            # rating / opentime 只在显式要了 show_fields 时才存在（出行要、聊天不要），
            # 所以用 get 且有值才拼——聊天那边的输出一字不变。
            if place.get("rating") is not None:
                parts.append(f"，评分 {place['rating']}")
            if place.get("opentime"):
                parts.append(f"，营业时间 {place['opentime']}")
            lines.append("- " + "".join(parts))
        lines.append("（以上为高德地图返回的真实地点，可直接向用户推荐。）")
        return "\n".join(lines)
