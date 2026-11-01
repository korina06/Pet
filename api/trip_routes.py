"""出行接口层：表单式规划，业务在 `core/trip_planner.py`。

不走 `/api/chat`；输入不满足时明确 400；拿不到真实数据时返回 failed 的行程而不是一份假行程。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.config import get_settings
from core.itinerary import validate_trip_request
from core.trip_planner import TripRequest, build_itinerary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trip", tags=["trip"])

#: 流水线需要的工具名。缺任何一个都说明装配出了问题，直接报 500 而不是硬跑。
_REQUIRED_TOOLS = ("geocode_city", "get_weather", "find_nearby_places")


class _Observed:
    """给工具套一层"计时 + 记 span"（第 124 步）。

    出行的四个工具由流水线直接调用（不经过 Agent 的 `_run_tool`），
    所以它们的耗时与成败原来在诊断页上是**空白**的。这个适配器补上，
    失败也照样回灌（异常照抛，只是先记一笔）。
    """

    def __init__(self, tool: Any, name: str) -> None:
        self._tool = tool
        self.name = name

    async def run(self, **kwargs: Any) -> Any:
        from core import log_context, observability
        import time

        started = time.monotonic()
        ok = False
        error = ""
        try:
            result = await self._tool.run(**kwargs)
            ok = bool(getattr(result, "ok", False))
            error = str(getattr(result, "error", "") or "")
            return result
        except Exception as exc:      # noqa: BLE001 —— 先记一笔，再照原样抛
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            observability.record_tool(
                name=self.name,
                ok=ok,
                ms=observability.monotonic_ms(started),
                error=error,
                request_id=log_context.request_id(),
                feature=log_context.current()[0] or "travel",
                session=log_context.full_session(),
                turn=log_context.turn_id(),
                step=0,
            )


class TripCreateRequest(BaseModel):
    """出行窗口提交的表单。字段与 index.html 里的一一对应。"""

    session_id: str = Field(default="", max_length=128)
    city: str = Field(default="", max_length=64)
    #: 出发地（第 231 步）：可选。只记录与展示，不改排程。
    origin_city: str = Field(default="", max_length=64)
    start_date: str = Field(default="", max_length=32)
    end_date: str = Field(default="", max_length=32)
    transport: str = Field(default="", max_length=32)
    stay: str = Field(default="", max_length=32)
    preferences: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=500)


class TripResponse(BaseModel):
    """一份行程。

    ``itinerary`` 直接给结构化 dict（core/itinerary.py 的 Itinerary.to_dict），
    而不是再包一层重复的模型——重复定义两份字段必然会漂移。
    """

    trip_id: str
    itinerary: dict[str, Any]


@router.post("", response_model=TripResponse)
async def create_trip(
    payload: TripCreateRequest, request: Request, response: Response
) -> TripResponse:
    """生成一份行程。**只用真实数据**，拿不到就明说，不编。"""
    errors = validate_trip_request(payload.city, payload.start_date, payload.end_date)
    if errors:
        # 用「；」拼成一句人话给用户，而不是把 pydantic 的数组丢到界面上
        # （第 37 步踩过：422 的 detail 是数组，界面里就冒出英文校验文案）。
        raise HTTPException(status_code=400, detail="；".join(errors))

    # 出行只拿到自己那一组（第 85 步）；缺了就回退但会 warning（第 89 步）
    from tools.registry import feature_registry_for

    registry = feature_registry_for(
        request.app.state, "travel_registry", feature="出行"
    )
    tools = {name: registry.get(name) for name in _REQUIRED_TOOLS}
    route_tool = getattr(request.app.state, "route_tool", None)
    # 第 124 步：出行**不走 Agent**，是流水线直接调工具 —— 所以原来这四个调用
    # 没有 span（评价8 指出的观测盲区）。套一层计时，让诊断页也能看到它们。
    from core.log_context import set_context as set_log_context
    from uuid import uuid4

    set_log_context("travel", payload.session_id, uuid4().hex[:12])
    if route_tool is not None:
        route_tool = _Observed(route_tool, "driving_route")

    tools = {
        name: (_Observed(tool, name) if tool is not None else None)
        for name, tool in tools.items()
    }
    missing = [name for name, tool in tools.items() if tool is None]
    if missing or route_tool is None:
        logger.error("出行流水线缺少工具：%s", ", ".join(missing) or "driving_route")
        raise HTTPException(
            status_code=500,
            detail="服务内部错误：出行所需的工具没有装配好。",
        )

    request_body = TripRequest(
        city=payload.city,
        origin_city=payload.origin_city,
        start_date=payload.start_date,
        end_date=payload.end_date,
        transport=payload.transport,
        stay=payload.stay,
        preferences=list(payload.preferences),
        note=payload.note,
        session_id=payload.session_id,
    )

    # 第 70 步：给整条流水线一个**总预算**。逐次调用都有超时与重试，
    # 但没有总预算时，最坏情况（每次都要重试）会拖到几十秒 —— 用户只能干等，
    # 运维也说不清 p95 到底是多少。
    deadline = float(get_settings().trip_deadline_seconds or 25.0)
    try:
        async with asyncio.timeout(deadline):
            itinerary = await build_itinerary(
                request_body,
                geocoder=tools["geocode_city"],
                weather_tool=tools["get_weather"],
                poi_tool=tools["find_nearby_places"],
                route_tool=route_tool,
            )
    except TimeoutError as exc:
        logger.warning("行程生成超过总预算 %.0f 秒，已中止", deadline)
        raise HTTPException(
            status_code=504,
            detail=(
                f"这次生成超过了 {deadline:.0f} 秒还没跑完，可能有外部服务卡住了。"
                "稍后再试一次；如果一直这样，多半是某个数据源在限流。"
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底成用户可读的 500
        logger.exception("生成行程时发生未捕获异常")
        raise HTTPException(
            status_code=500,
            detail=f"服务内部错误：{type(exc).__name__}",
        ) from exc

    request.app.state.trip_store.put(itinerary)
    # 第 145 步：**业务失败不缓存**。
    # 幂等中间件只认 HTTP 状态码，而"没生成出行程"也是 200 ——
    # 不特别说明的话，同一个 key 重试 10 分钟内都只会拿到那份失败结果（评价9 P2）。
    if itinerary.status == "failed":
        response.headers["Idempotency-Store"] = "no"
    return TripResponse(trip_id=itinerary.trip_id, itinerary=itinerary.to_dict())


@router.get("/{trip_id}", response_model=TripResponse)
async def get_trip(trip_id: str, request: Request) -> TripResponse:
    """读一份已生成的行程（前端刷新后恢复用）。"""
    itinerary = request.app.state.trip_store.get(trip_id)
    if itinerary is None:
        raise HTTPException(
            status_code=404,
            detail="找不到这份行程：可能服务重启过（本轮行程只存在内存里）。",
        )
    return TripResponse(trip_id=itinerary.trip_id, itinerary=itinerary.to_dict())


class TripTextRequest(BaseModel):
    """把前端手里的行程 JSON 渲染成文本（第 130 步）。"""

    itinerary: dict[str, Any] = Field(default_factory=dict)


class TripTextResponse(BaseModel):
    text: str = ""


@router.post("/text", response_model=TripTextResponse)
async def trip_as_text(payload: TripTextRequest) -> TripTextResponse:
    """行程导出成可粘贴的文本。

    为什么由**服务端**渲染：前端自己拼会变成两套格式（导出一套、界面一套），
    改一处忘一处。这里只留一套。

    为什么收的是 JSON 而不是 trip_id：行程存在内存里，重启就没了；
    而界面上还拿着那份数据 —— 直接渲染它更可靠，也不依赖 store。
    """
    from core.trip_text import render_itinerary

    return TripTextResponse(text=render_itinerary(payload.itinerary))


class TripMarkdownResponse(BaseModel):
    """导出成文件用（第 218 步）：前端拿它做 Blob 下载。"""

    filename: str = ""
    markdown: str = ""


@router.post("/markdown", response_model=TripMarkdownResponse)
async def trip_as_markdown(payload: TripTextRequest) -> TripMarkdownResponse:
    """行程导出成 Markdown（带高德地图链接）。

    仍然由**服务端**渲染：和「复制行程」共用 `core/trip_text.py`，
    两种导出永远说同一件事。文件名也在服务端拼 —— 城市名里的 `/`、`:` 之类
    在 Windows 上直接存不下来，这种清洗只该有一处。
    """
    from core.trip_text import markdown_filename, render_itinerary_markdown

    trip = payload.itinerary if isinstance(payload.itinerary, dict) else {}
    return TripMarkdownResponse(
        filename=markdown_filename(trip),
        markdown=render_itinerary_markdown(trip),
    )
