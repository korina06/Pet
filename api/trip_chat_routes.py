"""出行窗口的聊天式规划：模型自己问缺的信息、选工具、写行程。

回复会过 `core/trip_verify` 把工具里没查到的地名挑出来交给前端高亮；它只提示，不改回答。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agents.agent import Agent, build_travel_prompt
from api.auth import owner_of, scoped_session_id
from core.config import get_settings
from core.schemas import Message
from core.trip_verify import allowed_names, suspect_places

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trip", tags=["trip"])

#: 会话 id 前缀：聊天与出行的历史都落在同一个 SessionStore 里，
#: 靠前缀分开，免得"出行"的对话跑到聊天窗口里去。
TRIP_SESSION_PREFIX = "trip:"

_WEEKDAYS = "一二三四五六日"


class TripChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=64, default="default")


class TripRouteImage(BaseModel):
    """一条"已经查过、所以可以画出来"的路线。

    为什么由服务端给出 `url` 而不让前端自己拼：坐标 → 图片地址是**我们的**接口形状，
    前端拼地址等于把接口细节复制一份（改一处忘一处）；而且「这条路线真的查过」
    这个前提只有服务端知道（工具返回成功才算）。
    """

    mode: str
    url: str
    #: 工具那句人话（"驾车约 144.0 公里，约 102 分钟…"），当图注用
    text: str = ""


class TripChatResponse(BaseModel):
    session_id: str
    content: str
    #: 回答里「像地名但工具没返回过」的名字。空列表 ≠ 证明没有编造。
    suspects: list[str] = Field(default_factory=list)
    #: 这一轮真实调用过的工具名（按顺序），前端用来显示"查了什么"。
    tools_used: list[str] = Field(default_factory=list)
    #: 这一轮查过、并且能画成图的路线（第 227 步）。前端直接 `<img src>`。
    routes: list[TripRouteImage] = Field(default_factory=list)


#: 能画进静态图的通行方式。公交（transit）不画：高德的公交线是另一套数据，
#: 硬画出来会和实际换乘路线不一致（这点在 `api/map_routes.py` 里也写了）。
MAP_MODES: frozenset[str] = frozenset({"driving", "walking", "bicycling"})
#: 一轮最多给几张图：用户问一次行程通常只要 1–2 条路线，多了就是刷屏。
MAX_ROUTE_IMAGES = 3


def _coords(arguments: dict[str, object], lat_key: str, lon_key: str) -> str | None:
    """把两个坐标字段拼成 `经度,纬度`；缺一个或不是数字就返回 None（不编坐标）。"""
    try:
        lat = float(arguments[lat_key])  # type: ignore[arg-type]
        lon = float(arguments[lon_key])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return f"{lon:.6f},{lat:.6f}"


def _route_images(invocations: list) -> list[TripRouteImage]:
    """从这一轮的工具调用里挑出「查成功了、有坐标、能画」的路线。

    只认 `ok=True` 的结果：查失败的路线画出来就是一条假的直线。
    """
    seen: set[tuple[str, str, str]] = set()
    images: list[TripRouteImage] = []
    for item in invocations or []:
        if getattr(item, "tool_name", "") != "driving_route":
            continue
        if not getattr(item, "ok", False):
            continue
        arguments = getattr(item, "arguments", None) or {}
        data = getattr(item, "data", None) or {}
        mode = str(arguments.get("mode") or data.get("mode") or "driving").strip().lower()
        if mode not in MAP_MODES:
            continue
        origin = _coords(arguments, "origin_lat", "origin_lon")
        destination = _coords(arguments, "dest_lat", "dest_lon")
        if not (origin and destination):
            continue
        key = (mode, origin, destination)
        if key in seen:
            continue
        seen.add(key)
        images.append(
            TripRouteImage(
                mode=mode,
                url=f"/api/map/route.png?{urlencode({'origin': origin, 'destination': destination, 'mode': mode})}",
                text=str(getattr(item, "content", "") or "")[:120],
            )
        )
        if len(images) >= MAX_ROUTE_IMAGES:
            break
    return images


def _today_line() -> str:
    """给模型一个真实的"今天"，否则它算不出「下周末」。"""
    today = date.today()
    return f"【今天】{today.isoformat()}（周{_WEEKDAYS[today.weekday()]}）"


def _setup(request: Request, payload: TripChatRequest) -> dict:
    """/chat 与 /chat/stream 共用的装配（两条路必须完全一致）。"""
    from core import usage as usage_store
    from core.log_context import set_context as set_log_context
    from tools.registry import feature_registry_for

    set_log_context("trip", payload.session_id, uuid4().hex[:12])
    usage_store.reset_turn_tokens()

    llm = getattr(request.app.state, "llm", None)
    registry = feature_registry_for(request.app.state, "travel_registry", feature="出行")
    store = getattr(request.app.state, "session_store", None)
    if llm is None or registry is None or store is None:
        raise HTTPException(status_code=503, detail="出行对话未初始化（缺少模型或工具）")

    settings = get_settings()
    agent = Agent(
        llm=llm,
        registry=registry,
        # 出行一条完整规划要 定位 → 天气 → 景点 → 餐馆 → 车次 → 作答，
        # 所以步数走自己的配置（第 216 步），不共用聊天的 4 步。
        max_steps=settings.trip_chat_max_steps,
        # 第 256 步：出行自己的 token 预算。共用聊天的 20K 会在第 5 步前停下
        # （实测一轮 4 次调用已经 25,952 token）。
        turn_token_budget=settings.trip_chat_turn_token_budget,
        system_prompt=build_travel_prompt(
            registry.names(), address=settings.pet_address
        ),
    )
    return {"store": store, "agent": agent, "registry": registry, "settings": settings}


def _session_key(payload: TripChatRequest, owner: str = "local") -> str:
    # 第 251 步：多用户模式下会话 id 带 owner 前缀（本机模式原样）
    return f"{TRIP_SESSION_PREFIX}{scoped_session_id(payload.session_id, owner)}"


def _verify(content: str, invocations: list) -> list[str]:
    return suspect_places(content, allowed_names(invocations))


def _tools_used(invocations: list) -> list[str]:
    return [str(getattr(item, "tool_name", "") or "") for item in invocations if getattr(item, "tool_name", "")]


@router.post("/chat", response_model=TripChatResponse)
async def trip_chat(payload: TripChatRequest, request: Request) -> TripChatResponse:
    setup = _setup(request, payload)
    store = setup["store"]
    session_key = _session_key(payload, owner_of(request))

    stored = await store.get_history(session_key)
    user_message = Message(role="user", content=payload.message)
    working = [
        *stored,
        # 只改**发给模型**的副本：库里存的仍是用户原话。
        Message(role="user", content=f"{_today_line()}\n{payload.message}"),
    ]

    deadline = float(setup["settings"].trip_chat_deadline_seconds or 150.0)
    try:
        async with asyncio.timeout(deadline):
            reply = await setup["agent"].run(working)
    except TimeoutError as exc:
        logger.warning("出行对话超过总预算 %.0f 秒，已中止", deadline)
        raise HTTPException(
            status_code=504,
            detail=f"这次规划超过 {deadline:.0f} 秒还没想完，先停下来了。可以把条件说少一点再试。",
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 兜底成用户可读的 500
        logger.exception("出行对话失败（会话 %s）", payload.session_id)
        raise HTTPException(status_code=500, detail=f"服务内部错误：{type(exc).__name__}") from exc

    await store.append(session_key, user_message)
    if reply.content:
        await store.append(session_key, Message(role="assistant", content=reply.content))

    if reply.error:
        logger.warning("出行对话模型失败：%s", reply.error)

    return TripChatResponse(
        session_id=payload.session_id,
        content=reply.content,
        suspects=_verify(reply.content, reply.tool_invocations),
        tools_used=_tools_used(reply.tool_invocations),
        routes=_route_images(reply.tool_invocations),
    )
