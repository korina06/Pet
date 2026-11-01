"""城市名 → 经纬度（高德地理编码）。

表单里填的是城市名，而天气与 POI 工具只吃经纬度；没有这一步模型只能编坐标。
只做正向地理编码，不做逆地理编码，也不做地址补全。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

import httpx

from core.config import Settings, get_settings
from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)

GEOCODE_URL: Final[str] = "https://restapi.amap.com/v3/geocode/geo"
DEFAULT_TIMEOUT: Final[float] = 8.0
DEFAULT_RETRY_BACKOFF: Final[float] = 0.6
MAX_RETRIES: Final[int] = 2
#: 地理编码结果几乎不变（改个区划才会变），缓存给足一天都不为过。
#: 第 70 步加：之前出行每次都要重新查一次城市坐标，而这是**可以完全省掉**的一次往返。
CACHE_TTL: Final[float] = 7 * 24 * 3600

#: 「查无此地」的缓存时长（秒）。评价11 P3-6：地名会变写法，一周太长。
#: 参照 api/routes.py 的 _PHOTO_CACHE —— 失败只缓存 120 秒，这里对齐它。
NEGATIVE_CACHE_TTL: Final[float] = 120

#: 缓存条数上限。进程是长跑服务，按查过的地名无限增长属结构性问题（评价11 P3-6）。
CACHE_MAX_ENTRIES: Final[int] = 512

#: 高德「等一秒就能过」的业务码：访问过于频繁 / 单 key QPS 超限
#: 评价11 P3-7：这类错误以前被当终局失败，一次出行直接丢一段数据。
_RETRYABLE_INFOCODES: Final[frozenset[str]] = frozenset({"10003", "10021"})

#: 高德错误码 -> 可读原因与下一步（与 poi_tool 保持一致的写法）
_ERROR_HINTS: Final[dict[str, str]] = {
    "10001": "key 不正确或已过期，请检查 .env 里的 GAODE_API_KEY",
    "10003": "访问过于频繁，请稍后再试",
    "10009": "key 与服务类型不匹配：请确认该 key 的『服务平台』选的是 Web 服务",
    "10021": "并发配额超限（个人 key 每天有 QPS 限制），稍等几秒再试",
    "10029": "key 配额已用尽，请到高德控制台查看用量",
    "20800": "该 key 未开通 Web 服务 API 权限",
}


def _split_location(location: str) -> tuple[float, float] | None:
    """高德返回的 location 形如 "116.410000,39.920000"（经度在前）。"""
    parts = (location or "").split(",")
    if len(parts) != 2:
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except ValueError:
        return None
    return lat, lon


class GeocodeTool(BaseTool):
    """把城市名（或"城市 + 具体地点"）转成经纬度。"""

    name = "geocode_city"
    label = "查坐标"
    intent_template = "帮我查一下{city}的经纬度。"
    #: 不给手动入口：它只服务于"出行"这类多步流程，用户不需要单独点它
    show_in_menu = False

    description = (
        "把一个地名（城市名，或“城市+地点”，例如“杭州”“北京 故宫”）转换成经纬度。"
        "**只要需要某个城市的坐标，就必须先调用本工具**，不要凭记忆写坐标。"
        "本项目的天气工具与周边推荐工具都只接受经纬度，"
        "所以“查某城市天气/周边”的正确做法是：先调用本工具拿到坐标，再用坐标调用它们。"
        "返回值里的 lat / lon 请原样传给后续工具。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名或地名，例如“北京”“上海”“杭州 西湖”",
            },
        },
        "required": ["city"],
    }

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        #: 地名 -> (写入时刻, 高德原始 payload)。只缓存**成功**结果。
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    async def run(self, city: str = "", **kwargs: Any) -> ToolResult:
        keyword = (city or "").strip()
        if not keyword:
            return ToolResult(
                ok=False,
                error="请提供要查询的地名。",
                tool_name=self.name,
                arguments={"city": city},
            )

        settings = self.settings
        if not settings.gaode_api_key:
            return ToolResult(
                ok=False,
                error=(
                    "还没配置高德地图 key，无法把地名转换成坐标。"
                    "请在 .env 里填 GAODE_API_KEY（服务类型要选「Web 服务」）。"
                ),
                tool_name=self.name,
                arguments={"city": keyword},
            )

        params = {"key": settings.gaode_api_key, "address": keyword, "output": "JSON"}
        cached = self._cache.get(keyword)
        if cached is not None:
            stored_at, cached_payload, cached_ttl = cached
            if time.monotonic() - stored_at < cached_ttl:
                logger.debug("命中地理编码缓存：%s", keyword)
                payload = cached_payload
            else:
                self._cache.pop(keyword, None)
                payload = None
        else:
            payload = None

        if payload is None:
            payload = await self._request(params, keyword)
            if isinstance(payload, ToolResult):
                # 失败**不进缓存**：错误（网络、限额、key 问题）都是临时的，
                # 缓存住只会让用户"换个说法也没用"（评价11 P3-6）
                return payload
            self._remember(keyword, payload)

        geocodes = payload.get("geocodes") or []
        if not geocodes:
            # 「查无此地」是合法业务返回，但**地名会变写法** ——
            # 所以只按 NEGATIVE_CACHE_TTL（120 秒）记住，而不是 7 天
            self._remember(keyword, payload, ttl=NEGATIVE_CACHE_TTL)
            return ToolResult(
                ok=False,
                error=(
                    f"高德没有找到「{keyword}」对应的坐标。"
                    "请换一个更常见的写法（例如只写城市名，或“城市+景点名”）。"
                ),
                tool_name=self.name,
                arguments={"city": keyword},
            )

        first = geocodes[0]
        parsed = _split_location(str(first.get("location", "")))
        if parsed is None:
            return ToolResult(
                ok=False,
                error=f"高德返回的坐标格式不对：{first.get('location')!r}",
                tool_name=self.name,
                arguments={"city": keyword},
            )

        lat, lon = parsed
        formatted = first.get("formatted_address") or keyword
        province = first.get("province") or ""
        city_name = first.get("city") or ""
        district = first.get("district") or ""

        lines = [
            f"{formatted} 的坐标：lat={lat}, lon={lon}",
            f"经纬度：{lon},{lat}（经度在前是高德的习惯，本项目工具用 lat/lon）",
        ]
        region = "".join(part for part in (province, city_name, district) if part)
        if region:
            lines.append(f"行政区：{region}")
        lines.append("把上面的 lat / lon 原样传给天气或周边推荐工具即可。")

        return ToolResult(
            ok=True,
            content="\n".join(lines),
            tool_name=self.name,
            arguments={"city": keyword},
            data={
                "lat": lat,
                "lon": lon,
                "formatted_address": formatted,
                "province": province,
                "city": city_name,
                "district": district,
                "adcode": str(first.get("adcode") or ""),
            },
        )

    def _remember(
        self, keyword: str, payload: dict[str, Any], *, ttl: float = CACHE_TTL
    ) -> None:
        """写缓存，并保证不超过上限（超了就丢最早写入的那条）。"""
        self._cache[keyword] = (time.monotonic(), payload, ttl)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)

    async def _request(
        self, params: dict[str, Any], keyword: str
    ) -> dict[str, Any] | ToolResult:
        """带重试的请求；失败时返回可直接回灌给模型的 ToolResult。"""
        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            try:
                if self._client is not None:
                    response = await self._client.get(
                        GEOCODE_URL, params=params, timeout=DEFAULT_TIMEOUT
                    )
                else:
                    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                        response = await client.get(GEOCODE_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                last_error = f"网络请求失败：{type(exc).__name__}"
                logger.warning("地理编码请求失败（第 %s 次）：%s", attempt + 1, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(DEFAULT_RETRY_BACKOFF * (attempt + 1))
                    continue
                return ToolResult(
                    ok=False,
                    error=f"{last_error}（已重试 {MAX_RETRIES} 次）",
                    tool_name=self.name,
                    arguments={"city": keyword},
                )

            status = str(payload.get("status", ""))
            if status == "1":
                return payload

            info = str(payload.get("info", ""))
            infocode = str(payload.get("infocode", ""))
            if infocode in _RETRYABLE_INFOCODES and attempt < MAX_RETRIES:
                logger.warning(
                    "地理编码碰到限流（infocode=%s，第 %s 次），退避重试", infocode, attempt + 1
                )
                await asyncio.sleep(DEFAULT_RETRY_BACKOFF * (attempt + 1) * 2)
                continue
            hint = _ERROR_HINTS.get(infocode, info or "未知原因")
            return ToolResult(
                ok=False,
                error=f"高德地理编码失败：{hint}（infocode={infocode or '无'}）",
                tool_name=self.name,
                arguments={"city": keyword},
            )

        return ToolResult(
            ok=False,
            error=last_error or "地理编码失败",
            tool_name=self.name,
            arguments={"city": keyword},
        )
