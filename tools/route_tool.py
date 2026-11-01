"""驾车路线工具：两个坐标之间的真实驾车距离与时长（高德 direction/driving）。

只由出行流水线实例化，不进默认注册表；返回的是驾车口径，市内短途会偏长。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Final

import httpx

from core.config import Settings, get_settings
from core.coords import wgs84_to_gcj02
from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)

DRIVING_URL: Final[str] = "https://restapi.amap.com/v3/direction/driving"
WALKING_URL: Final[str] = "https://restapi.amap.com/v3/direction/walking"
TRANSIT_URL: Final[str] = "https://restapi.amap.com/v3/direction/transit/integrated"
#: 骑行（第 223 步）：高德把它放在 v4，返回结构和 v3 不一样（`errcode`/`data.paths`）
BICYCLING_URL: Final[str] = "https://restapi.amap.com/v4/direction/bicycling"
#: 支持的通行方式 -> 端点（第 67 步）。
#: **只做这三种**：骑行接口在 v4、响应壳和 v3 不同（没有 status/infocode，
#: 改用 errcode/data），而表单里也没有「骑行」选项 —— 为一个用不到的选项
#: 多写一条解析不划算。三种都已经用真实 key 探过：
#:   walking  route.paths[0].{distance,duration}
#:   transit  route.transits[0].{distance,duration,cost,walking_distance,segments}
MODE_URLS: Final[dict[str, str]] = {
    "driving": DRIVING_URL,
    "walking": WALKING_URL,
    "transit": TRANSIT_URL,
    "bicycling": BICYCLING_URL,
}
DEFAULT_TIMEOUT: Final[float] = 8.0
DEFAULT_RETRY_BACKOFF: Final[float] = 0.6
MAX_RETRIES: Final[int] = 2
#: 路线数据变化很慢（修路才会变），缓存一天足够，也能省下大量配额。
CACHE_TTL: Final[float] = 86400.0

#: 高德错误码 -> 可读原因与下一步（与 geocode/poi 保持一致的写法）
#: 通行方式的中文名（只用于拼错误文案）
_MODE_NAMES: Final[dict[str, str]] = {
    "driving": "驾车",
    "walking": "步行",
    "transit": "公交",
    "bicycling": "骑行",
}

#: 途经道路最多显示几条。三条足够让人判断"走哪条大路"，再多就是噪音
MAX_ROADS = 3
#: 单条路名最长多少字（高速全名很长，截断比丢掉整条好）
ROAD_CHARS = 14


def road_names(steps: Any, limit: int = MAX_ROADS) -> list[str]:
    """从高德 steps 里抽「途经什么路」：按出现顺序去重，最多 `limit` 条。

    为什么只留路名不留 `instruction`：一句 instruction 常有 20~30 字
    （"向东骑行80米左转"），而模型和用户要判断的是"走哪条路"。
    """
    names: list[str] = []
    if not isinstance(steps, list):
        return names
    for step in steps:
        if not isinstance(step, dict):
            continue
        road = " ".join(str(step.get("road") or "").split())
        if not road or road in names:
            continue
        names.append(road[:ROAD_CHARS])
        if len(names) >= limit:
            break
    return names


def _roads_tail(route: dict[str, Any]) -> str:
    roads = route.get("roads")
    if not isinstance(roads, list) or not roads:
        return ""
    return "，途经 " + " → ".join(str(road) for road in roads)


_ERROR_HINTS: Final[dict[str, str]] = {
    "10001": "key 不正确或已过期，请检查 .env 里的 GAODE_API_KEY",
    "10003": "访问过于频繁，请稍后再试",
    "10009": "key 与服务类型不匹配：请确认该 key 的『服务平台』选的是 Web 服务",
    "10021": "并发配额超限（个人 key 每天有 QPS 限制），稍等几秒再试",
    "10029": "key 配额已用尽，请到高德控制台查看用量",
    "20001": "请求参数有误，请检查经纬度是否是数字",
    "20800": "该 key 未开通 Web 服务 API 权限",
    "20803": "起点或终点所在道路无法规划驾车路线（例如在景区内部）",
}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


class RouteTool(BaseTool):
    """两个坐标之间的驾车距离与时长。"""

    name = "driving_route"
    label = "驾车路线"
    #: 出行流水线内部调用，不给任何手动入口
    show_in_menu = False

    description = (
        "查询两个坐标之间的驾车距离与预计时长。"
        "仅用于出行流水线内部计算景点之间的通行段，不面向聊天场景。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "origin_lat": {"type": "number", "description": "起点纬度"},
            "origin_lon": {"type": "number", "description": "起点经度"},
            "dest_lat": {"type": "number", "description": "终点纬度"},
            "dest_lon": {"type": "number", "description": "终点经度"},
            "coordsys": {
                "type": "string",
                "enum": ["wgs84", "gcj02"],
                "description": "传入坐标的坐标系。高德 POI 给的坐标是 gcj02。",
                "default": "gcj02",
            },
        },
        "required": ["origin_lat", "origin_lon", "dest_lat", "dest_lon"],
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
        self._cache: dict[tuple, dict[str, Any]] = {}

    @property
    def settings(self) -> Settings:
        return self._settings_override if self._settings_override is not None else get_settings()

    def clear_cache(self) -> None:
        self._cache.clear()

    def _cache_get(self, key: tuple) -> dict[str, Any] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        stored_at, data = hit
        if time.monotonic() - stored_at >= self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return data

    # ------------------------------------------------------------------ 入口

    async def run(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
        coordsys: str = "gcj02",
        *,
        mode: str = "driving",
        city_adcode: str = "",
        use_cache: bool = True,
    ) -> ToolResult:
        args = {
            "origin_lat": origin_lat,
            "origin_lon": origin_lon,
            "dest_lat": dest_lat,
            "dest_lon": dest_lon,
        }
        nums = [
            _to_float(origin_lat), _to_float(origin_lon),
            _to_float(dest_lat), _to_float(dest_lon),
        ]
        if any(n is None for n in nums):
            return self._failure(f"经纬度必须是数字，收到 {args!r}。", args)
        o_lat, o_lon, d_lat, d_lon = (float(n) for n in nums)  # type: ignore[arg-type]

        try:
            api_key = self.settings.require_gaode_key()
        except ValueError:
            return self._failure(
                "未配置高德地图 Key（GAODE_API_KEY），无法计算景点之间的路线。",
                args,
            )

        # 通行方式：driving / walking / transit（第 67 步）
        way = (mode or "driving").strip().lower()
        if way not in MODE_URLS:
            return self._failure(
                f"不支持的通行方式：{mode!r}（只支持 driving / walking / transit / bicycling）。", args
            )
        adcode = str(city_adcode or "").strip()
        if way == "transit" and not adcode:
            # 公交接口必须给起终点城市；拿不到 adcode 就明确失败，
            # 由调用方决定降级成驾车（**不能静默**换口径）。
            return self._failure(
                "公交路线需要城市编码（adcode），这次没拿到。", args
            )

        system = (coordsys or "gcj02").strip().lower()
        if system == "gcj02":
            go_lon, go_lat, gd_lon, gd_lat = o_lon, o_lat, d_lon, d_lat
        else:
            go_lon, go_lat = wgs84_to_gcj02(o_lon, o_lat)
            gd_lon, gd_lat = wgs84_to_gcj02(d_lon, d_lat)

        # 缓存键必须带**通行方式**（同一个 OD 对，驾车/步行/公交是三份答案）
        cache_key = (
            round(go_lon, 4), round(go_lat, 4), round(gd_lon, 4), round(gd_lat, 4),
            way, adcode,
        )
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.debug("命中路线缓存（%s）%s -> %s", way, cache_key[:2], cache_key[2:4])
                return self._success(cached, args)

        route, error = await self._fetch(
            api_key, go_lon, go_lat, gd_lon, gd_lat, way, adcode
        )
        if route is None:
            return self._failure(error or "高德路线查询失败。", args)

        if use_cache:
            self._cache[cache_key] = (time.monotonic(), route)
        return self._success(route, args)

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
        go_lon: float,
        go_lat: float,
        gd_lon: float,
        gd_lat: float,
        way: str = "driving",
        adcode: str = "",
    ) -> tuple[dict[str, Any] | None, str | None]:
        params: dict[str, Any] = {
            "key": api_key,
            "origin": f"{go_lon:.6f},{go_lat:.6f}",       # 高德：经度,纬度
            "destination": f"{gd_lon:.6f},{gd_lat:.6f}",
        }
        if way == "transit":
            # 公交必给起终点城市；同城时 city 与 cityd 相同
            params["city"] = adcode
            params["cityd"] = adcode
            params["strategy"] = "0"
        elif way == "driving":
            # 第 223 步：`all` 才带 steps（其中的 road 就是"途经什么路"）。
            # 响应从 0.9KB 变成约 16KB，但我们只留道路名，模型那边看到的东西反而更少。
            params["extensions"] = "all"
            params["strategy"] = "0"
        # walking 只认 origin/destination，多传反而可能被拒
        url = MODE_URLS.get(way, DRIVING_URL)
        last_error = "高德路线查询失败。"
        async with self._acquire_client() as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.get(url, params=params)
                except httpx.TimeoutException:
                    last_error = f"请求高德路线超时（超过 {self._timeout:.0f} 秒），请检查网络后重试。"
                    logger.warning("高德驾车路线超时（第 %d 次尝试）", attempt + 1)
                except httpx.HTTPError as exc:
                    last_error = (
                        f"访问高德路线服务失败（网络错误）：{type(exc).__name__}。"
                        "请检查网络连通性。"
                    )
                    logger.warning("高德驾车路线网络错误（第 %d 次尝试）", attempt + 1)
                else:
                    status = response.status_code
                    if 200 <= status < 300:
                        return self._parse(response, way)
                    if status >= 500 or status == 429:
                        last_error = f"高德服务暂时不可用（HTTP {status}），请稍后再试。"
                        logger.warning("高德路线返回可重试状态码 HTTP %s", status)
                    else:
                        return None, f"高德路线接口返回 HTTP {status}，请求未被接受。"
                if attempt < self._max_retries:
                    delay = self._retry_backoff * (2**attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break
        return None, last_error

    def _parse(
        self, response: httpx.Response, way: str = "driving"
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = response.json()
        except ValueError:
            return None, f"高德返回的内容不是合法 JSON（HTTP {response.status_code}）。"
        if not isinstance(payload, dict):
            return None, "高德返回的内容结构异常。"

        # 骑行（v4）的结构和 v3 不同：没有 status，用 errcode/errcode==0 表示成功
        if way == "bicycling":
            return self._parse_bicycling(payload)

        if str(payload.get("status")) != "1":
            code = str(payload.get("infocode") or "")
            info = str(payload.get("info") or "")
            hint = _ERROR_HINTS.get(code, "请到高德控制台查看用量与权限")
            return None, f"高德返回错误 {code}（{info}）：{hint}。"

        route = payload.get("route")
        if not isinstance(route, dict):
            return None, "高德没有返回路线结构。"

        if way == "transit":
            # 公交的结构和驾车/步行完全不同：答案在 route.transits[]
            transits = route.get("transits")
            if not isinstance(transits, list) or not transits:
                return None, "高德没有规划出公交路线（可能两点之间没有公交可达）。"
            first_transit = transits[0] if isinstance(transits[0], dict) else {}
            distance_m = _to_int(first_transit.get("distance"))
            if distance_m is None:
                distance_m = _to_int(route.get("distance"))
            duration_s = _to_int(first_transit.get("duration"))
            if distance_m is None or duration_s is None:
                return None, "高德返回的公交路线缺少距离或时长字段。"
            segments = first_transit.get("segments")
            return (
                {
                    "distance_m": distance_m,
                    "duration_s": duration_s,
                    # 票价与换乘次数都是高德给的真实值，可以直接显示
                    "fare": _to_float(first_transit.get("cost")),
                    "walking_distance_m": _to_int(first_transit.get("walking_distance")),
                    "transfers": max(0, len(segments) - 1) if isinstance(segments, list) else None,
                    "mode": "transit",
                },
                None,
            )

        paths = route.get("paths")
        if not isinstance(paths, list) or not paths:
            return None, f"高德没有规划出{_MODE_NAMES.get(way, '')}路线（可能两点之间不可达）。"

        first = paths[0] if isinstance(paths[0], dict) else {}
        distance_m = _to_int(first.get("distance"))
        duration_s = _to_int(first.get("duration"))
        if distance_m is None or duration_s is None:
            return None, "高德返回的路线缺少距离或时长字段。"

        data: dict[str, Any] = {
            "distance_m": distance_m,
            "duration_s": duration_s,
            "mode": way,
        }
        if way == "driving":
            data["tolls"] = _to_float(first.get("tolls"))
            data["taxi_cost"] = _to_float(first.get("taxi_cost"))
        # 途经道路（第 223 步）：`_describe` 只留前几条，压缩后约 40 字，
        # 但这句话正是用户说的「具体的规划」——只说"144 公里"没人能判断走哪条。
        data["roads"] = road_names(first.get("steps"))
        return data, None

    def _parse_bicycling(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """高德 v4 骑行：`{"errcode": 0, "data": {"paths": [{"distance", "duration", "steps"}]}}`。"""
        errcode = payload.get("errcode")
        if errcode not in (0, "0"):
            return (
                None,
                f"高德骑行路线返回错误 {errcode}（{payload.get('errmsg') or '未知原因'}）。"
                "骑行只覆盖同城短途，跨城请改用驾车或公交。",
            )
        body = payload.get("data")
        paths = body.get("paths") if isinstance(body, dict) else None
        if not isinstance(paths, list) or not paths:
            return None, "高德没有规划出骑行路线（可能两点之间不可骑行）。"
        first = paths[0] if isinstance(paths[0], dict) else {}
        distance_m = _to_int(first.get("distance"))
        duration_s = _to_int(first.get("duration"))
        if distance_m is None or duration_s is None:
            return None, "高德返回的骑行路线缺少距离或时长字段。"
        return (
            {
                "distance_m": distance_m,
                "duration_s": duration_s,
                "mode": "bicycling",
                "roads": road_names(first.get("steps")),
            },
            None,
        )

    # ------------------------------------------------------------------ 输出

    def _success(self, route: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            ok=True,
            content=self._describe(route),
            tool_name=self.name,
            arguments=arguments,
            data=route,
        )

    def _failure(self, error: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, error=error, tool_name=self.name, arguments=arguments)

    def _describe(self, route: dict[str, Any]) -> str:
        way = str(route.get("mode") or "driving")
        km = route["distance_m"] / 1000
        minutes = max(1, round(route["duration_s"] / 60))

        if way == "transit":
            text = f"公共交通约 {km:.1f} 公里，约 {minutes} 分钟"
            if route.get("fare") is not None:
                text += f"，票价约 {route['fare']:.0f} 元"
            if route.get("transfers"):
                text += f"，换乘 {route['transfers']} 次"
            return text + "。（来自高德公交路线）"

        if way == "walking":
            return (
                f"步行约 {km:.1f} 公里，约 {minutes} 分钟"
                + _roads_tail(route)
                + "。（来自高德步行路线）"
            )

        if way == "bicycling":
            text = f"骑行约 {km:.1f} 公里，约 {minutes} 分钟"
            return text + _roads_tail(route) + "。（来自高德骑行路线）"

        text = f"驾车约 {km:.1f} 公里，约 {minutes} 分钟"
        if route.get("taxi_cost") is not None:
            text += f"，打车约 {route['taxi_cost']:.0f} 元"
        tolls = route.get("tolls")
        # 过路费为 0 时**不说**"过路费 0 元"：市内路线本来就没有过路费，
        # 写出来只会让模型把"0 元"当成一个需要解释的数字。
        if isinstance(tolls, (int, float)) and tolls > 0:
            text += f"，过路费约 {tolls:.0f} 元"
        return text + _roads_tail(route) + "。（距离与时长来自高德驾车路线，为驾车口径）"
