"""和风天气工具：实况与逐日预报。

host 与 key 都从配置取；认证走 `X-QW-Api-Key`；逐日要带 `localTime=true`；
客户端可注入、失败重试两次、结果缓存十分钟，任何失败都回 `ToolResult(ok=False)`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, AsyncIterator, Final

import httpx

from core.config import Settings, get_settings
from core.itinerary import MAX_FORECAST_DAYS
from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)

DEFAULT_LANG: Final[str] = "zh"
#: 实时天气缓存时长（秒）。实时天气本身分钟级更新，10 分钟足够，且能挡住同一轮对话里的重复调用。
REALTIME_CACHE_TTL: Final[float] = 600.0
DEFAULT_TIMEOUT: Final[float] = 8.0
DEFAULT_RETRY_BACKOFF: Final[float] = 0.5
MAX_RETRIES: Final[int] = 2
#: 这些状态码说明是暂时性故障，值得重试；其它 4xx（除 429）重试没有意义。
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

CONSOLE_SETTING_URL: Final[str] = "https://console.qweather.com/setting"

#: 新版 v1 文档里的风向是罗盘方位代码，转成中文才方便写进给模型看的句子里。
_COMPASS_ZH: Final[dict[str, str]] = {
    "n": "北风",
    "nne": "北东北风",
    "ne": "东北风",
    "ene": "东东北风",
    "e": "东风",
    "ese": "东东南风",
    "se": "东南风",
    "sse": "南东南风",
    "s": "南风",
    "ssw": "南西南风",
    "sw": "西南风",
    "wsw": "西西南风",
    "w": "西风",
    "wnw": "西西北风",
    "nw": "西北风",
    "nnw": "北西北风",
    "vrb": "风向不定",
    "none": "无持续风向",
}

#: 业务错误码（字符串）到可操作提示的映射。
_CODE_HINTS: Final[dict[str, str]] = {
    "204": "该地区暂无数据，请换一个经纬度再试",
    "400": "请求参数有误，请检查经纬度格式与取值范围（纬度 -90~90，经度 -180~180）",
    "401": "认证失败，请检查 .env 中的 HEFENG_API_KEY 是否正确",
    "402": "访问次数已用尽或账户余额不足，请到控制台查看用量",
    "403": "没有访问权限，请确认凭据与账号专属 API Host 是否匹配、是否已开通该数据",
    "404": "查询的地区不存在，请检查经纬度是否写错",
    "429": "请求过于频繁被限流，请稍后重试",
    "500": "和风天气服务端错误，请稍后重试",
}


def _to_float(value: Any) -> float | None:
    """把 API 返回的字符串字段安全地转成 float。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_number(value: Any) -> str:
    """格式化数值：8.0 -> "8"，31.71 -> "31.7"；非数值原样返回。"""
    number = _to_float(value)
    if number is None:
        return str(value).strip()
    if abs(number - round(number)) < 0.05:
        return str(int(round(number)))
    return f"{number:.1f}"


def _fmt_coord(value: float) -> str:
    """经纬度按文档要求最多保留两位小数，且去掉多余的 0。"""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _unwrap(value: Any) -> Any:
    """取出 ``{"value": 31.71, "unit": "°C"}`` 这类包装里的数值。"""
    if isinstance(value, dict):
        return value.get("value")
    return value


def _unit_of(value: Any) -> str | None:
    if isinstance(value, dict):
        unit = value.get("unit")
        if isinstance(unit, str) and unit.strip():
            return unit.strip()
    return None


def _first(*values: Any) -> Any:
    """返回第一个非空的候选值（用于兼容不同版本的字段名）。"""
    for value in values:
        if value is not None and value != "":
            return value
    return None


#: 能证明"这个 dict 里装的确实是天气数据"的字段名（覆盖新版与 v7 两种命名）。
_WEATHER_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "temp",
        "temperature",
        "text",
        "condition",
        "humidity",
        "relativeHumidity",
        "feelsLike",
        "feels_like",
        "windDir",
        "wind_dir",
        "wind",
        "obsTime",
        "obs_time",
    }
)


def _has_weather_fields(payload: dict[str, Any]) -> bool:
    """判断顶层 payload 是否直接就是天气数据（而不是包着 data 的外壳）。"""
    return any(key in payload for key in _WEATHER_MARKERS)


def _as_percent(value: Any) -> str | None:
    """湿度归一化成百分数。

    新版 ``data.now.humidity`` 是 "32" 这样的百分数字符串，
    而文档里的嵌套结构给的是 0~1 的小数；两种情况都要能正确显示。
    """
    if value is None or value == "":
        return None
    number = _to_float(value)
    if number is None:
        return str(value).strip()
    if isinstance(value, (int, float)) and 0.0 <= number <= 1.0:
        number *= 100.0
    return f"{_fmt_number(number)}%"


def _as_wind_dir(value: Any) -> str | None:
    """风向：中文原样用，罗盘方位代码（sw）转中文，纯数字当角度处理。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        mapped = _COMPASS_ZH.get(text.lower())
        if mapped:
            return mapped
        number = _to_float(text)
        if number is not None and text.replace(".", "", 1).lstrip("-").isdigit():
            return f"{_fmt_number(number)}°"
        return text
    number = _to_float(value)
    if number is None:
        return str(value)
    return f"{_fmt_number(number)}°"


def _as_time(value: Any) -> str | None:
    """观测时间：把 ISO 的 T 换成空格，读起来更像人话。"""
    if value is None or value == "":
        return None
    return str(value).strip().replace("T", " ")


def _location_name(data: dict[str, Any]) -> str | None:
    location = data.get("location")
    nested = location.get("name") if isinstance(location, dict) else None
    name = _first(data.get("name"), nested)
    return str(name).strip() if name else None


def _normalize_now(now: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """把 ``data.now`` 归一化成固定字段的 dict。

    主要面向任务约定的新版结构（``data.now`` + 字符串字段），
    同时兼容文档里的嵌套结构（``condition`` / ``temperature`` / ``wind``）
    和 v7 风格的 ``now``，避免上游换个字段名就整个工具不可用。
    """
    condition = now.get("condition") if isinstance(now.get("condition"), dict) else {}
    wind = now.get("wind") if isinstance(now.get("wind"), dict) else {}
    direction = wind.get("direction") if isinstance(wind.get("direction"), dict) else {}
    temperature = now.get("temperature")
    feels_like = _first(now.get("feelsLike"), now.get("feels_like"))

    return {
        "location": _location_name(data),
        "temp": _first(_unwrap(temperature), _unwrap(now.get("temp"))),
        "temp_unit": _unit_of(temperature) or _unit_of(now.get("temp")),
        "text": _first(now.get("text"), condition.get("text")),
        "feels_like": _unwrap(feels_like),
        "feels_like_unit": _unit_of(feels_like),
        "humidity": _as_percent(_first(now.get("humidity"), now.get("relativeHumidity"))),
        "wind_dir": _as_wind_dir(
            _first(
                now.get("windDir"),
                now.get("wind_dir"),
                direction.get("compass"),
                direction.get("degree"),
            )
        ),
        "wind_scale": _fmt_number(
            _first(now.get("windScale"), now.get("wind_scale"), wind.get("scale"))
        )
        if _first(now.get("windScale"), now.get("wind_scale"), wind.get("scale")) is not None
        else None,
        "obs_time": _as_time(_first(now.get("obsTime"), now.get("obs_time"), now.get("updateTime"))),
    }


def _resolve_days_window(
    days: Any, end_date: Any, today: date
) -> tuple[int | None, bool, str | None]:
    """决定要查几天，返回 ``(天数, 是否被 10 天上限截断, 错误)``。

    **和风的 ``days`` 是「从今天起算」**，不是「行程有几天」。
    行程 11-04 开始、今天 10-30，要 ``days=6`` 才够到 11-04；
    传「行程天数」只会拿回今天起的几天，后面的日子全被误判成「没预报」。
    所以这里让模型直接给 ``end_date``（查到哪天），减法由代码做。

    ``days`` 仍然保留，用于「未来几天」这种没有结束日期的问法，
    语义同样是从今天起算（含今天）。
    """
    if end_date is not None and str(end_date).strip():
        raw = str(end_date).strip()
        try:
            last = date.fromisoformat(raw)
        except ValueError:
            return None, False, f"end_date 要写成 YYYY-MM-DD，收到 {end_date!r}。"
        delta = (last - today).days
        if delta < 0:
            return None, False, (
                f"end_date 不能早于今天（{today.isoformat()}），收到 {raw}。"
            )
        wanted = delta + 1
        return min(wanted, MAX_FORECAST_DAYS), wanted > MAX_FORECAST_DAYS, None

    if days is None or str(days).strip() == "":
        return 1, False, None
    try:
        wanted = int(float(days))
    except (TypeError, ValueError):
        return None, False, (
            f"days 必须是 1（实况）到 {MAX_FORECAST_DAYS}（逐日预报）之间的整数，"
            f"收到 days={days!r}。"
        )
    if not 1 <= wanted <= MAX_FORECAST_DAYS:
        return None, False, (
            f"days 只能是 1（实况）到 {MAX_FORECAST_DAYS}（逐日预报）之间的整数，"
            f"收到 {wanted}。"
        )
    return wanted, False, None


def _normalize_forecast(day: dict[str, Any]) -> dict[str, Any] | None:
    """把一天的预报压成界面与排程都够用的几个字段。

    日期取 ``forecastStartTime`` 的前 10 位：请求带了 ``localTime=true``，
    实测返回形如 ``2026-10-30T00:00+08:00``，这 10 位就是当地日期。
    （不带 localTime 时是 UTC，同一个时刻会落到错误的日期上。）

    返不出的那一天返回 ``None``——**宁可缺一天，也不拿相邻两天插值**。
    """
    start = str(day.get("forecastStartTime") or "").strip()
    if len(start) < 10 or start[4] != "-" or start[7] != "-":
        return None
    daytime = day.get("daytime") if isinstance(day.get("daytime"), dict) else {}
    nighttime = day.get("nighttime") if isinstance(day.get("nighttime"), dict) else {}
    condition = daytime.get("condition") if isinstance(daytime.get("condition"), dict) else {}
    night_condition = (
        nighttime.get("condition") if isinstance(nighttime.get("condition"), dict) else {}
    )
    precipitation = (
        daytime.get("precipitation") if isinstance(daytime.get("precipitation"), dict) else {}
    )
    tmax = _first(day.get("temperatureMax"), daytime.get("temperatureMax"))
    tmin = _first(day.get("temperatureMin"), nighttime.get("temperatureMin"))
    return {
        "date": start[:10],
        "text_day": str(condition.get("text") or "").strip(),
        "text_night": str(night_condition.get("text") or "").strip(),
        "temp_max": _to_float(_unwrap(tmax)),
        "temp_min": _to_float(_unwrap(tmin)),
        "temp_unit": _unit_of(tmax) or _unit_of(tmin) or "°C",
        "precip_prob": _to_float(precipitation.get("probability")),
        "source": "qweather_daily",
    }


def _temp_span(item: dict[str, Any]) -> str:
    """一天的最低~最高温。缺一头就只写有的那头，两个都没有就整段不写。"""
    high = _unwrap(item.get("temp_max"))
    low = _unwrap(item.get("temp_min"))
    unit = str(item.get("temp_unit") or "°C")
    if high is None and low is None:
        return ""
    if high is None:
        return f"最低 {_fmt_number(low)}{unit}"
    if low is None:
        return f"最高 {_fmt_number(high)}{unit}"
    return f"{_fmt_number(low)}~{_fmt_number(high)}{unit}"


def _describe_forecast(
    daily: Any, until: str | None = None, truncated: bool = False
) -> str:
    """逐日预报拼成一句话。**开头就写「预报」**：它不是观测，措辞不能滑过去。"""
    if not isinstance(daily, list) or not daily:
        return ""
    parts: list[str] = []
    for item in daily:
        if not isinstance(item, dict):
            continue
        bits: list[str] = []
        span = _temp_span(item)
        if span:
            bits.append(span)
        text_day = str(item.get("text_day") or "").strip()
        text_night = str(item.get("text_night") or "").strip()
        if text_day:
            bits.append(text_day)
        if text_night and text_night != text_day:
            bits.append(f"夜间{text_night}")
        probability = item.get("precip_prob")
        if probability is not None:
            try:
                percent = round(float(probability) * 100)
            except (TypeError, ValueError):
                percent = None
            if percent is not None:
                bits.append(f"降水概率 {percent}%")
        parts.append(f"{item.get('date') or '某天'} " + "，".join(bits))
    if not parts:
        return ""
    header = "逐日预报（预报，不是观测"
    if until:
        header += f"，截至 {until}"
    if truncated:
        header += f"；和风最多预报 {MAX_FORECAST_DAYS} 天"
    return header + "）：" + "；".join(parts) + "。"


class WeatherTool(BaseTool):
    """查询指定经纬度的天气（实况 + 可选逐日预报）。"""

    name = "get_weather"
    #: 轮盘菜单上的短名称
    label = "查天气"
    #: 从菜单点击时发出的意图（第 24 步起胶囊不再用它，保留给将来的手动入口）
    intent_template = "帮我查一下北京的天气。"
    #: 不在轮盘菜单展示：天气改为在对话里自动识别调用（见第 13 步）。
    #: 第 24 步起聊天框的胶囊也改为"打开出行窗口"，所以这个工具当前没有手动入口，
    #: 但接口与能力都保留——模型照常调用，出行窗口下一步也会用它。
    show_in_menu = False

    description = (
        "查询某个经纬度位置的天气。"
        "不传窗口参数（默认）查**实况**（温度、天气现象、体感、湿度、风向风力、观测时间）；"
        "传 end_date（要查到哪一天，行程用这个）或 days（从今天起几天）还会给出**逐日预报**"
        "（每天白天/夜间天气、最高最低温、降水概率，最多 10 天）。"
        "预报不是观测：转述时要说明这是预报。"
        "当用户问“现在/今天天气怎么样”“外面冷不热”“要不要带伞、穿外套”“天气适合出门吗”，"
        "或者用户问“后几天/这周会下雨吗”“11 月 4 号那几天天气怎么样”"
        "（按问法传 days 或 end_date），"
        "或者用户只是发来一个地名（例如“北京”“上海呢”）时，都调用这个工具。"
        "注意：参数是经纬度（lat/lon），不是城市名。"
        "用户没有提到任何地点时，用配置里的默认经纬度（默认城市），不要反问坐标；"
        "用户提到了其它城市时，按你已知的地理常识把该城市换成经纬度再传进来"
        "（例如上海 31.23/121.47、广州 23.13/113.26）。"
        "lang 默认 zh（中文），仅在用户要求其它语言时修改。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            # 第 83 步：**改成可选**。以前 required 意味着模型"必须"给出数字，
            # 而它并不知道配置里的默认坐标 —— 于是只能凭记忆编一个（实测提示词里
            # 甚至明写"自己按地理常识换成经纬度"）。坐标是事实，不该由模型猜。
            "lat": {
                "type": "number",
                "description": (
                    "纬度（十进制度）。**不知道就不要传**，服务端会用配置里的默认城市；"
                    "用户提到了别的城市，先用 geocode_city 查，不要凭记忆写"
                ),
            },
            "lon": {
                "type": "number",
                "description": "经度（十进制度）。同上：不知道就别传，交给服务端默认值",
            },
            "lang": {
                "type": "string",
                "description": "返回语言，默认 zh（中文）",
                "default": DEFAULT_LANG,
            },
            "days": {
                "type": "integer",
                "description": (
                    "**从今天起算**要几天：1（默认）只要实况；2~10 额外给逐日预报（含今天）。"
                    "只知道「未来几天」时才用它；知道行程/要查到哪一天就用 end_date"
                ),
                "minimum": 1,
                "maximum": MAX_FORECAST_DAYS,
                "default": 1,
            },
            "end_date": {
                "type": "string",
                "description": (
                    "要查到哪一天（YYYY-MM-DD，含这一天）。**行程用它**："
                    "传行程最后一天即可，不要自己算「今天到那天有几天」；"
                    "工具会自动换算并封顶在 10 天内。与 days 同时给时以它为准"
                ),
            },
        },
        "required": [],
    }

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        settings: Settings | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_retries: int = MAX_RETRIES,
        cache_ttl: float = REALTIME_CACHE_TTL,
    ) -> None:
        """``client`` 传入时直接复用（测试注入 MockTransport），不传时才自己建。"""
        self._client = client
        self._settings_override = settings
        self._timeout = timeout
        self._retry_backoff = max(0.0, retry_backoff)
        self._max_retries = max(0, max_retries)
        self._cache_ttl = cache_ttl
        # key = (lat, lon, lang, days) -> (写入时刻 monotonic, 归一化后的天气数据)
        self._cache: dict[
            tuple[float, float, str, int], tuple[float, dict[str, Any]]
        ] = {}

    # ------------------------------------------------------------------ 配置

    @property
    def settings(self) -> Settings:
        """显式注入的 settings 优先，否则每次取最新配置（.env 改动立刻生效）。"""
        return self._settings_override if self._settings_override is not None else get_settings()

    def _resolve_base_url(self) -> tuple[str | None, str | None]:
        try:
            return self.settings.hefeng_base_url, None
        except ValueError:
            return None, (
                "未配置和风天气 API Host（HEFENG_API_HOST）：新版和风天气的 host 是账号专属的，"
                f"请到 {CONSOLE_SETTING_URL} 查看形如 abcxyz.qweatherapi.com 的地址，"
                "写入 .env 的 HEFENG_API_HOST 后重试。"
            )

    def _resolve_api_key(self) -> tuple[str | None, str | None]:
        try:
            return self.settings.require_hefeng_key(), None
        except ValueError:
            return None, (
                "未配置和风天气 API Key（HEFENG_API_KEY）：请到 https://console.qweather.com "
                "创建凭据并写入 .env 的 HEFENG_API_KEY 后重试。"
            )

    # ------------------------------------------------------------------ 缓存

    def clear_cache(self) -> None:
        """清空实时天气缓存（测试或用户强制刷新时用）。"""
        self._cache.clear()

    def _cache_get(self, key: tuple[float, float, str, int]) -> dict[str, Any] | None:
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
        lat: float | None = None,
        lon: float | None = None,
        lang: str = DEFAULT_LANG,
        days: int | None = None,
        end_date: str | None = None,
        *,
        use_cache: bool = True,
        today: date | None = None,
    ) -> ToolResult:
        """查询天气：不传窗口参数只要实况；传了再带上逐日预报。

        ``end_date`` 是「查到哪天」（YYYY-MM-DD），优先于 ``days``。
        ``days`` 是**从今天起算**的天数（1=只要实况，2~10=实况+逐日），
        不是「行程有几天」——行程要覆盖到某天请用 ``end_date``。

        失败一律返回 ``ToolResult(ok=False, error=...)``，由 Agent 决定怎么向用户解释。
        ``use_cache=False`` 可绕过缓存（测试与"强制刷新"用）。
        ``today`` 只给测试注入，正常调用取本机日期。
        """
        # 没给坐标就用配置里的默认城市（第 83 步）：
        # 坐标是**事实**，模型不知道就不该编；而 required 曾逼它编。
        settings = self.settings
        if lat is None or lon is None or str(lat).strip() == "" or str(lon).strip() == "":
            lat = settings.default_city_lat
            lon = settings.default_city_lon
            logger.debug("天气未给坐标，使用默认城市 %s,%s", lat, lon)

        # 参数可能是 LLM 生成的字符串，这里统一收敛；float() 失败属于可读的入参错误
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                error=f"经纬度必须是数字，收到 lat={lat!r}, lon={lon!r}。",
                tool_name=self.name,
                arguments={"lat": lat, "lon": lon, "lang": lang},
            )

        lang_s = str(lang).strip() if lang is not None else ""
        lang_s = lang_s or DEFAULT_LANG

        # 第 236 步：和风的 days 是「从今天起算」。模型给行程天数就一定会错位，
        # 所以窗口改由 end_date 推出来；越界与非法格式都在发请求前拒绝。
        current_date = today if today is not None else date.today()
        requested, truncated, window_error = _resolve_days_window(
            days, end_date, current_date
        )
        if requested is None:
            return ToolResult(
                ok=False,
                error=window_error or "天气窗口参数不合法。",
                tool_name=self.name,
                arguments={
                    "lat": lat,
                    "lon": lon,
                    "lang": lang_s,
                    "days": days,
                    "end_date": end_date,
                },
            )

        arguments: dict[str, Any] = {"lat": lat_f, "lon": lon_f, "lang": lang_s}
        if days is not None:
            arguments["days"] = requested
        if end_date is not None and str(end_date).strip():
            arguments["end_date"] = str(end_date).strip()
        cache_key = (round(lat_f, 2), round(lon_f, 2), lang_s, requested)

        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.debug("命中实时天气缓存 lat=%s lon=%s lang=%s", lat_f, lon_f, lang_s)
                return self._success(cached, arguments)

        base_url, base_url_error = self._resolve_base_url()
        if base_url is None:
            return self._failure(base_url_error or "未配置和风天气 API Host。", arguments)

        api_key, api_key_error = self._resolve_api_key()
        if api_key is None:
            return self._failure(api_key_error or "未配置和风天气 API Key。", arguments)

        data, error = await self._fetch_current(base_url, api_key, lat_f, lon_f, lang_s)
        if requested == 1:
            if data is None:
                return self._failure(error or "和风天气请求失败。", arguments)
            if use_cache:
                self._cache[cache_key] = (time.monotonic(), data)
            return self._success(data, arguments)

        # days>=2：实况 + 逐日预报各发一次，合成一份 data。
        # 为什么两块都要：实况给「此刻」，预报给「这几天」，前者不能冒充后者。
        daily, daily_error = await self._fetch_daily(
            base_url, api_key, lat_f, lon_f, lang_s, requested
        )
        if data is None and daily is None:
            return self._failure(error or daily_error or "和风天气请求失败。", arguments)

        merged: dict[str, Any] = dict(data or {})
        if daily is not None:
            merged["daily"] = daily
            merged["forecast_days"] = requested
            # 覆盖窗口写进 data：排程据此判断哪天在范围内，不用自己再做日期减法。
            merged["forecast_from"] = current_date.isoformat()
            merged["forecast_until"] = (
                current_date + timedelta(days=requested - 1)
            ).isoformat()
            if truncated:
                merged["forecast_truncated"] = True
        else:
            merged["forecast_error"] = daily_error or "这次没拿到逐日预报"
        if data is None:
            # 实况没拿到但预报拿到了：仍然可用，只是要把缺的那块说清楚。
            merged["current_error"] = error or "这次没拿到实况天气"
        if use_cache:
            self._cache[cache_key] = (time.monotonic(), merged)
        return self._success(merged, arguments)

    # ------------------------------------------------------------------ 请求

    @asynccontextmanager
    async def _acquire_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """有注入的 client 就用它；否则临时建一个带超时的 client。"""
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            yield client

    async def _get_json(
        self, url: str, params: dict[str, Any], api_key: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """带重试地发 GET，返回 ``(原始 JSON 对象, 错误信息)``。

        抽出来是因为实况与逐日预报走**同一个 host、同一个 X-QW-Api-Key**，
        只差 path 与参数（第 234 步）——重复一份重试逻辑迟早会改漏一处。
        """
        # 认证必须走请求头；api_key 绝不写进日志
        headers = {"X-QW-Api-Key": api_key, "Accept": "application/json"}

        last_error = "和风天气请求失败。"
        async with self._acquire_client() as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.get(url, params=params, headers=headers)
                except httpx.TimeoutException:
                    last_error = (
                        f"请求和风天气超时（超过 {_fmt_number(self._timeout)} 秒）："
                        "请检查本机网络或代理设置后重试。"
                    )
                    logger.warning("和风天气请求超时（第 %d 次尝试）", attempt + 1)
                except httpx.HTTPError as exc:
                    last_error = (
                        f"访问和风天气失败（网络错误）：{type(exc).__name__}: {exc}。"
                        "请检查网络连通性与 DNS/代理设置。"
                    )
                    logger.warning("和风天气网络错误（第 %d 次尝试）：%s", attempt + 1, type(exc).__name__)
                else:
                    status = response.status_code
                    if 200 <= status < 300:
                        try:
                            payload = response.json()
                        except ValueError:
                            return None, f"和风天气返回的内容不是合法 JSON（HTTP {status}）。"
                        if not isinstance(payload, dict):
                            return None, "和风天气返回的内容结构异常（不是 JSON 对象）。"
                        return payload, None
                    if status in RETRYABLE_STATUS:
                        last_error = (
                            f"和风天气服务暂时不可用（HTTP {status}），已重试仍未成功，请稍后再试。"
                        )
                        logger.warning("和风天气返回可重试状态码 HTTP %s", status)
                    elif status >= 400:
                        # 4xx（除 429）重试没有意义，直接给出可操作文案
                        return None, self._describe_http_error(response)
                    else:
                        return None, f"和风天气返回了未预期的 HTTP 状态码 {status}。"

                if attempt < self._max_retries:
                    delay = self._retry_backoff * (2**attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break

        return None, last_error

    async def _fetch_current(
        self,
        base_url: str,
        api_key: str,
        lat: float,
        lon: float,
        lang: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """实况天气：``GET /weather/v1/current/{lat}/{lon}``。"""
        url = f"{base_url}/weather/v1/current/{_fmt_coord(lat)}/{_fmt_coord(lon)}"
        payload, error = await self._get_json(url, {"lang": lang}, api_key)
        if payload is None:
            return None, error
        return self._parse_payload(payload)

    async def _fetch_daily(
        self,
        base_url: str,
        api_key: str,
        lat: float,
        lon: float,
        lang: str,
        days: int,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """逐日预报：``GET /weather/v1/daily/{lat}/{lon}``。

        ``localTime=true`` 不能省：不带时 `forecastStartTime` 是 UTC，
        取前 10 位当日期会整体错 8 小时（晚上 8 点后就是第二天，或反过来）。
        """
        url = f"{base_url}/weather/v1/daily/{_fmt_coord(lat)}/{_fmt_coord(lon)}"
        params = {"days": days, "localTime": "true", "lang": lang}
        payload, error = await self._get_json(url, params, api_key)
        if payload is None:
            return None, error
        return self._parse_daily_payload(payload)

    def _parse_daily_payload(
        self, payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """解析逐日预报：v1 的顶层是 ``days`` 数组（2026-10-30 真调用确认过）。"""
        code = str(payload.get("code") or "").strip()
        if code and code != "200":
            hint = _CODE_HINTS.get(code, "未知错误码，请到和风天气控制台查看用量与权限")
            return None, f"和风天气返回业务错误码 {code}：{hint}。"

        raw = payload.get("days")
        if raw is None and isinstance(payload.get("data"), dict):
            raw = payload["data"].get("days")
        if not isinstance(raw, list) or not raw:
            return None, "和风天气返回的逐日预报为空。"

        forecasts: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_forecast(item)
            if normalized is not None:
                forecasts.append(normalized)
        if not forecasts:
            return None, "和风天气返回了逐日预报，但每一天都缺日期或要用的天气字段。"
        return forecasts, None

    def _parse_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """解析 2xx 响应：先看业务 code，再取 ``data.now``。"""
        code = str(payload.get("code") or "").strip()
        if code and code != "200":
            hint = _CODE_HINTS.get(code, "未知错误码，请到和风天气控制台查看用量与权限")
            return None, f"和风天气返回业务错误码 {code}：{hint}。"

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        now = data.get("now") if isinstance(data.get("now"), dict) else None
        if not now:
            # 兼容两种非 data.now 结构：
            # 1) 官方文档里的顶层扁平结构（condition / temperature / humidity 直接在根上）
            # 2) v7 风格的顶层 now
            # 注意不能用 `data or legacy`：data 已被兜底成空 dict，是假值，
            # 会把顶层的真实字段一起丢掉。
            legacy = payload.get("now")
            now = legacy if isinstance(legacy, dict) else (data or payload)
            # 只有当 payload 里确实有可识别的天气字段时才认；
            # 否则保持 None，走下面的"数据为空"报错。
            if now is payload and not _has_weather_fields(payload):
                now = None
        if not now:
            return None, "和风天气返回的数据为空（缺少 data.now），无法解析实时天气。"

        return _normalize_now(now, data), None

    def _describe_http_error(self, response: httpx.Response) -> str:
        """非 2xx 时响应体形如 ``{"error": {"status": 403, "title": "Invalid Host", ...}}``。"""
        status: Any = response.status_code
        title = ""
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                status = error.get("status") or status
                title = str(error.get("title") or "").strip()
                detail = str(error.get("detail") or "").strip()

        suffix = f"服务端说明：{detail}" if detail else ""
        if title.lower() == "invalid host":
            return (
                f"和风天气拒绝了当前 API Host（HTTP {status}，Invalid Host）：该请求的域名不被这个账号授权，"
                f"通常是把 Host 配错了（旧版公共域名在新版不可用）。请到 {CONSOLE_SETTING_URL} "
                "查看你的账号专属 API Host（形如 abcxyz.qweatherapi.com），"
                "写入 .env 的 HEFENG_API_HOST 后重试。"
                + (f"（{suffix}）" if suffix else "")
            )
        if status in (401, 403):
            return (
                f"和风天气鉴权失败（HTTP {status}"
                + (f"，{title}" if title else "")
                + "）：请检查 .env 里的 HEFENG_API_KEY 和 HEFENG_API_HOST 是否正确、"
                "该凭据是否已授权访问实时天气。请到 https://console.qweather.com 核对配置。"
                + (f"（{suffix}）" if suffix else "")
            )
        return (
            f"和风天气接口返回 HTTP {status}"
            + (f"（{title}）" if title else "")
            + "：请求未被接受，请稍后重试或检查账号状态。"
            + (f"（{suffix}）" if suffix else "")
        )

    # ------------------------------------------------------------------ 输出

    def _success(self, data: dict[str, Any], arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            ok=True,
            content=self._describe(data, arguments),
            tool_name=self.name,
            arguments=arguments,
            # 归一化后的实况字段（location/temp/text/humidity/...）：
            # 出行流水线直接用它填 Itinerary.weather，不再解析中文句子。
            data=data,
        )

    def _failure(self, error: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, error=error, tool_name=self.name, arguments=arguments)

    def _describe(self, data: dict[str, Any], arguments: dict[str, Any]) -> str:
        """拼一段通顺的中文，而不是原始 JSON——这段文字会回灌给 LLM 再组织语言。"""
        place = data.get("location") or f"坐标 {_fmt_coord(float(arguments['lat']))}, {_fmt_coord(float(arguments['lon']))}"
        clauses: list[str] = []

        temp = data.get("temp")
        if temp is not None:
            clauses.append(f"{_fmt_number(temp)}{data.get('temp_unit') or '°C'}")
        if data.get("text"):
            clauses.append(str(data["text"]))
        feels_like = data.get("feels_like")
        if feels_like is not None:
            clauses.append(f"体感 {_fmt_number(feels_like)}{data.get('feels_like_unit') or '°C'}")
        if data.get("humidity"):
            clauses.append(f"湿度 {data['humidity']}")
        if data.get("wind_dir") or data.get("wind_scale"):
            wind = str(data.get("wind_dir") or "风向未知")
            if data.get("wind_scale"):
                wind = f"{wind} {data['wind_scale']} 级"
            clauses.append(wind)
        if data.get("obs_time"):
            clauses.append(f"观测时间 {data['obs_time']}")

        lines: list[str] = []
        if clauses:
            lines.append(f"{place}当前" + "，".join(clauses) + "。")
        forecast = _describe_forecast(
            data.get("daily"),
            until=data.get("forecast_until"),
            truncated=bool(data.get("forecast_truncated")),
        )
        if forecast:
            lines.append(forecast)
        if not lines:
            return f"{place}的天气数据为空，暂时无法描述。"
        return "".join(lines) + "数据来源：和风天气。"
