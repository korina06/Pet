"""天气工具：全部用 MockTransport 伪造 HTTP，不发真实请求。

`retry_backoff=0.0` 是必须的，否则超时与 5xx 用例要真睡；async 用例显式标 asyncio。
"""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from typing import Any, Callable

import httpx
import pytest

from core.config import Settings
from tools.weather_tool import WeatherTool

HOST = "mock1234.qweatherapi.com"
API_KEY = "test-api-key"
LAT = 39.92
LON = 116.41

Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: Any) -> Settings:
    """构造测试用配置：``_env_file=None`` 保证不读取仓库里的真实 .env。"""
    values: dict[str, Any] = {"hefeng_api_host": HOST, "hefeng_api_key": API_KEY}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def current_body(**now_overrides: Any) -> dict[str, Any]:
    """新版接口的响应形状：外层 code 是字符串，数据在 data.now，字段值也是字符串。"""
    now: dict[str, Any] = {
        "temp": "8",
        "text": "晴",
        "feelsLike": "6",
        "humidity": "32",
        "windDir": "西北风",
        "windScale": "3",
        "obsTime": "2024-06-01T12:00+08:00",
    }
    now.update(now_overrides)
    return {"code": "200", "data": {"now": now}}


def make_tool(handler: Handler, **kwargs: Any) -> WeatherTool:
    """把 MockTransport 注入成客户端，工具本身完全不知道自己在被测试。"""
    kwargs.setdefault("retry_backoff", 0.0)
    kwargs.setdefault("settings", make_settings())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WeatherTool(client=client, **kwargs)


def test_tool_schema_contract() -> None:
    """工具名 / JSON Schema / description 的契约。"""
    tool = WeatherTool()

    assert tool.name == "get_weather"
    schema = tool.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_weather"

    params = tool.parameters
    assert params["type"] == "object"
    # 第 83 步改成可选：模型不知道坐标时**宁可别传**，服务端用默认城市。
    assert set(params["required"]) == set()
    assert params["properties"]["lat"]["type"] == "number"
    assert params["properties"]["lon"]["type"] == "number"
    assert params["properties"]["lang"]["type"] == "string"
    assert params["properties"]["lang"]["default"] == "zh"

    # description 要讲清"什么时候用"和"没给城市时用默认经纬度"，而不是城市名参数
    assert "默认经纬度" in tool.description
    assert "经纬度" in tool.description


@pytest.mark.asyncio
async def test_success_parses_realtime_weather() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is True
    assert result.error is None
    assert result.tool_name == "get_weather"
    assert result.arguments == {"lat": LAT, "lon": LON, "lang": "zh"}

    # content 必须是给人/模型看的中文句子，且带上关键信息
    assert "8°C" in result.content
    assert "晴" in result.content
    assert "体感 6°C" in result.content
    assert "湿度 32%" in result.content
    assert "西北风 3 级" in result.content
    assert "观测时间 2024-06-01 12:00+08:00" in result.content
    assert "{" not in result.content  # 不是原始 JSON dump

    # 新版契约：经纬度走路径参数，认证走请求头，key 不出现在 URL 里
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == f"/weather/v1/current/{LAT}/{LON}"
    assert request.url.host == HOST
    assert request.url.params["lang"] == "zh"
    assert "key" not in request.url.params
    assert request.headers["X-QW-Api-Key"] == API_KEY
    assert API_KEY not in str(request.url)


@pytest.mark.asyncio
async def test_gzip_response_is_decompressed() -> None:
    """响应体是 gzip 压缩的（Content-Encoding: gzip），不能被解压两次。"""
    raw = json.dumps(current_body(temp="31.71", text="少云"), ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=compressed,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, lang="en")

    assert result.ok is True, result.error
    assert "31.7°C" in result.content
    assert "少云" in result.content


@pytest.mark.asyncio
async def test_cache_hit_sends_single_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)

    first = await tool.run(lat=LAT, lon=LON)
    second = await tool.run(lat=LAT, lon=LON)

    assert first.ok is True and second.ok is True
    assert first.content == second.content
    assert calls == 1  # 第二次命中缓存，没有发请求

    # 缓存 key 含语言：换语言要重新请求
    await tool.run(lat=LAT, lon=LON, lang="en")
    assert calls == 2

    # 显式绕过缓存
    await tool.run(lat=LAT, lon=LON, use_cache=False)
    assert calls == 3

    # 清空缓存后重新请求
    tool.clear_cache()
    await tool.run(lat=LAT, lon=LON)
    assert calls == 4


@pytest.mark.asyncio
async def test_missing_api_key_returns_configuration_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler, settings=make_settings(hefeng_api_key=""))
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "配置" in result.error
    assert "HEFENG_API_KEY" in result.error
    assert result.tool_name == "get_weather"
    assert calls == 0  # 缺 key 时根本不该发请求


@pytest.mark.asyncio
async def test_missing_host_returns_configuration_error() -> None:
    tool = make_tool(lambda request: httpx.Response(200, json=current_body()), settings=make_settings(hefeng_api_host=""))
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "HEFENG_API_HOST" in result.error
    assert "console.qweather.com/setting" in result.error


@pytest.mark.asyncio
async def test_invalid_host_403_is_translated_to_actionable_hint() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            json={
                "error": {
                    "status": 403,
                    "title": "Invalid Host",
                    "detail": "the host is not valid for this account",
                }
            },
        )

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "Invalid Host" in result.error
    assert "console.qweather.com" in result.error
    assert "HEFENG_API_HOST" in result.error
    assert calls == 1  # 4xx（非 429）不重试


@pytest.mark.asyncio
async def test_business_code_not_200_returns_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": "404", "data": {}})

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "404" in result.error

    # 失败结果不进缓存：再调一次仍会重新发请求
    await tool.run(lat=LAT, lon=LON)
    assert calls == 2


@pytest.mark.asyncio
async def test_timeout_is_retried_and_finally_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("simulated timeout")

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "超时" in result.error
    assert calls == 3  # 1 次原始请求 + 最多 2 次重试


@pytest.mark.asyncio
async def test_retryable_5xx_is_retried_until_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": {"status": 503, "title": "Service Unavailable"}})
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is True, result.error
    assert calls == 3
    assert "8°C" in result.content


@pytest.mark.asyncio
async def test_network_error_is_reported_readably() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is False
    assert result.error is not None
    assert "网络" in result.error
    assert calls == 3  # 网络错误同样重试


@pytest.mark.asyncio
async def test_documented_nested_v1_shape_is_also_supported() -> None:
    """兼容官方文档里的嵌套结构（没有 data.now，湿度是 0~1 的小数，风向是方位代码）。"""
    payload = {
        "metadata": {"tag": "abc"},
        "condition": {"text": "少云", "code": "102"},
        "temperature": {"value": 31.71, "unit": "°C"},
        "feelsLike": {"value": 33.64, "unit": "°C"},
        "humidity": 0.69,
        "wind": {"direction": {"degree": 226, "compass": "sw"}, "speed": {"value": 4.74, "unit": "m/s"}, "scale": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON)

    assert result.ok is True, result.error
    assert "31.7°C" in result.content
    assert "少云" in result.content
    assert "体感 33.6°C" in result.content
    assert "湿度 69%" in result.content
    assert "西南风 3 级" in result.content


async def test_missing_coordinates_fall_back_to_the_configured_default_city() -> None:
    """不传坐标时用**配置里的**默认城市，而不是硬编码北京（第 83 步）。

    以前 lat/lon 是必填：模型不知道配置值，只能按工具描述里的"例如北京 39.92"
    编一个。你不在北京时，"今天天气"就是错的，而且**看不出来**。

    这里把默认城市设成杭州（30.25/120.16），断言实际请求真的用了它 ——
    如果有人把默认值写死成北京，这条会红。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=current_body())

    tool = make_tool(
        handler,
        settings=make_settings(default_city_lat=30.25, default_city_lon=120.16),
    )

    result = await tool.run()          # 完全不传坐标

    assert result.ok is True
    assert result.arguments["lat"] == 30.25
    assert result.arguments["lon"] == 120.16
    assert "30.25/120.16" in str(seen[0].url), f"实际请求：{seen[0].url}"


# ------------------------------------------------------- 逐日预报（第 234 步）


def daily_body(dates: list[str]) -> dict[str, Any]:
    """逐日预报的真实形状（2026-10-30 真调用的返回结构，字段取用到的部分）。

    顶层是 ``days`` 数组、**没有 data 外壳也没有 code**；
    ``localTime=true`` 时 ``forecastStartTime`` 带当地偏移。
    """
    return {
        "metadata": {"tag": "mock"},
        "days": [
            {
                "forecastStartTime": f"{d}T00:00+08:00",
                "forecastEndTime": f"{d}T23:59+08:00",
                "temperatureMax": {"value": 21.34, "unit": "°C"},
                "temperatureMin": {"value": 12.5, "unit": "°C"},
                "daytime": {
                    "condition": {"text": "阴", "code": "104"},
                    "precipitation": {
                        "amount": {"value": 0.4, "unit": "mm"},
                        "probability": 0.64,
                        "type": "rain",
                    },
                },
                "nighttime": {"condition": {"text": "小雨", "code": "305"}},
            }
            for d in dates
        ],
    }


def split_handler(daily: Callable[[], httpx.Response]) -> Handler:
    """按路径分流：``/daily/`` 走逐日，其余走实况。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/daily/" in request.url.path:
            return daily()
        return httpx.Response(200, json=current_body())

    return handler


@pytest.mark.asyncio
async def test_daily_forecast_uses_local_time_and_is_returned_by_day() -> None:
    seen: list[httpx.Request] = []
    daily = daily_body(["2026-10-30", "2026-10-31", "2026-11-01"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=daily)
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, days=3)

    assert result.ok is True, result.error
    assert result.arguments["days"] == 3
    forecasts = result.data["daily"]
    assert [item["date"] for item in forecasts] == [
        "2026-10-30",
        "2026-10-31",
        "2026-11-01",
    ], "日期要取当地日期，不能错位"
    assert forecasts[0]["temp_max"] == 21.34
    assert forecasts[0]["temp_min"] == 12.5
    assert forecasts[0]["precip_prob"] == 0.64
    assert forecasts[0]["text_day"] == "阴"
    assert forecasts[0]["text_night"] == "小雨"

    # localTime=true 不能省：不带的话 forecastStartTime 是 UTC，按日期映射会错 8 小时
    daily_request = next(r for r in seen if "/daily/" in r.url.path)
    assert daily_request.url.params["localTime"] == "true"
    assert daily_request.url.params["days"] == "3"
    assert daily_request.headers["X-QW-Api-Key"] == API_KEY
    assert "key" not in daily_request.url.params

    # 文案：预报不是观测，必须写明白；具体数字来自返回，不四舍五入成整数
    assert "逐日预报（预报，不是观测" in result.content
    assert "截至" in result.content, "要写清覆盖到哪天"
    assert "2026-10-30" in result.content
    assert "12.5~21.3°C" in result.content
    assert "降水概率 64%" in result.content
    assert "小雨" in result.content


@pytest.mark.asyncio
async def test_days_out_of_range_is_rejected_before_any_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    for bad in (0, -1, 11, "abc"):
        result = await tool.run(lat=LAT, lon=LON, days=bad)
        assert result.ok is False, f"days={bad!r} 应当被拒绝"
        assert result.error is not None
        assert "days" in result.error

    assert calls == 0, "参数不合法时一个请求都不该发"


@pytest.mark.asyncio
async def test_daily_failure_still_returns_the_current_weather() -> None:
    def daily() -> httpx.Response:
        return httpx.Response(503, json={"error": {"status": 503, "title": "Service Unavailable"}})

    tool = make_tool(split_handler(daily))
    result = await tool.run(lat=LAT, lon=LON, days=3)

    assert result.ok is True, "预报挂了不该把实况一起丢掉"
    assert "8°C" in result.content
    assert "晴" in result.content
    assert result.data.get("forecast_error")
    assert "daily" not in result.data


@pytest.mark.asyncio
async def test_current_failure_still_returns_the_forecast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=daily_body(["2026-10-30"]))
        return httpx.Response(
            403,
            json={"error": {"status": 403, "title": "Invalid Host", "detail": "no"}},
        )

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, days=2)

    assert result.ok is True, "预报还在就还能回答"
    assert result.data.get("current_error")
    assert "逐日预报（预报，不是观测" in result.content
    assert "当前" not in result.content, "没拿到实况就不能写一句『当前』"


@pytest.mark.asyncio
async def test_forecast_cache_is_keyed_by_day_count() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=daily_body(["2026-10-30", "2026-10-31"]))
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    await tool.run(lat=LAT, lon=LON, days=2)
    await tool.run(lat=LAT, lon=LON, days=2)  # 命中缓存

    daily_calls = [path for path in paths if "/daily/" in path]
    assert len(daily_calls) == 1

    await tool.run(lat=LAT, lon=LON, days=3)  # 天数不同必须重新请求
    assert len([path for path in paths if "/daily/" in path]) == 2


# ------------------------------- end_date：从今天起算，不是「行程几天」（第 236 步）

TODAY = date(2026, 10, 30)


def window_daily(days: int) -> dict[str, Any]:
    """从 TODAY 起的逐日预报，和风真实返回的顺序。"""
    return daily_body([(TODAY + timedelta(days=i)).isoformat() for i in range(days)])


@pytest.mark.asyncio
async def test_end_date_counts_from_today_not_from_the_trip_start() -> None:
    """行程 11-04 开始、今天 10-30 → 要 days=6，不是「行程 1 天」。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=window_daily(6))
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, end_date="2026-11-04", today=TODAY)

    assert result.ok is True, result.error
    daily_request = next(r for r in seen if "/daily/" in r.url.path)
    assert daily_request.url.params["days"] == "6", "要覆盖今天到 11-04，共 6 天"
    assert result.data["forecast_from"] == "2026-10-30"
    assert result.data["forecast_until"] == "2026-11-04"
    assert result.data["daily"][-1]["date"] == "2026-11-04", "最后一天正是行程首日"


@pytest.mark.asyncio
async def test_end_date_beyond_ten_days_is_capped_and_says_so() -> None:
    """行程 11-04~11-10：今天起最多 10 天，只能到 11-08，必须明说截断。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=window_daily(10))
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, end_date="2026-11-10", today=TODAY)

    assert result.ok is True, result.error
    daily_request = next(r for r in seen if "/daily/" in r.url.path)
    assert daily_request.url.params["days"] == "10", "不能超过接口上限"
    assert result.data["forecast_until"] == "2026-11-08"
    assert result.data["forecast_truncated"] is True
    assert "截至 2026-11-08" in result.content
    assert "和风最多预报 10 天" in result.content
    dates = [item["date"] for item in result.data["daily"]]
    assert "2026-11-06" in dates, "11-06 本来查得到，不能被当成没有"


@pytest.mark.asyncio
async def test_end_date_today_means_realtime_only() -> None:
    """end_date=今天 → days=1，只发实况请求（没有逐日预报可要）。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, end_date="2026-10-30", today=TODAY)

    assert result.ok is True, result.error
    assert not [path for path in paths if "/daily/" in path]
    assert result.arguments["end_date"] == "2026-10-30"


@pytest.mark.asyncio
async def test_end_date_in_the_past_is_rejected_before_any_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(lat=LAT, lon=LON, end_date="2026-10-29", today=TODAY)

    assert result.ok is False
    assert result.error is not None
    assert "不能早于今天" in result.error
    assert calls == 0


@pytest.mark.asyncio
async def test_end_date_format_is_validated() -> None:
    tool = make_tool(lambda request: httpx.Response(200, json=current_body()))
    result = await tool.run(lat=LAT, lon=LON, end_date="11/04", today=TODAY)

    assert result.ok is False
    assert result.error is not None
    assert "YYYY-MM-DD" in result.error


@pytest.mark.asyncio
async def test_end_date_wins_over_days() -> None:
    """两个都给了以 end_date 为准：它语义明确，days 容易带错口径。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/daily/" in request.url.path:
            return httpx.Response(200, json=window_daily(6))
        return httpx.Response(200, json=current_body())

    tool = make_tool(handler)
    result = await tool.run(
        lat=LAT, lon=LON, days=2, end_date="2026-11-04", today=TODAY
    )

    assert result.ok is True, result.error
    assert next(r for r in seen if "/daily/" in r.url.path).url.params["days"] == "6"
