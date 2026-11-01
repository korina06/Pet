"""聊天与会话的 HTTP 接口层：参数校验、依赖组装与响应模型。

持久化只留 user 与带正文的 assistant 消息；带 `tool_call_id` 的消息不跨轮次，
否则历史被截断后会出现孤儿 tool 消息，接口直接 400。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import asyncio
import json

from core.log_context import set_context as set_log_context
from core import usage as usage_store
from uuid import uuid4
from core.config import get_settings
from core.schemas import Message, ToolResult
from api.auth import owner_of, scoped_session_id
from api.streaming import client_gone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)


class ToolInvocationOut(BaseModel):
    tool_name: str
    ok: bool
    arguments: dict[str, Any] = {}
    error: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    content: str
    tool_invocations: list[ToolInvocationOut] = []
    steps: int = 0


class ChatMessageOut(BaseModel):
    """一条历史消息（打开聊天窗口时恢复用）。"""

    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    session_id: str
    messages: list[ChatMessageOut] = []
    #: 历史为空时的开场白（第 143 步）；有历史时是空串 —— 不能每开一次窗都念一遍。
    greeting: str = ""


class ToolInfo(BaseModel):
    name: str
    label: str
    description: str
    #: 点击该工具时前端发送的自然语言意图
    intent: str


class ToolsResponse(BaseModel):
    tools: list[ToolInfo]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/tools", response_model=ToolsResponse)
async def list_tools(request: Request) -> ToolsResponse:
    """给前端轮盘菜单用的**菜单项**清单。

    只返回 ``show_in_menu=True`` 的工具。当前默认工具集（天气、周边）都是 False，
    所以这个接口在默认工具集下返回空列表 —— 它们靠对话自动调用。

    第 24 步起，聊天框上方的胶囊改成"打开一个功能"的入口、不再由工具驱动，
    所以这里去掉了第 23 步临时加的 ``menu`` 字段，回到"只给轮盘用"的单一职责。

    想枚举全部已注册工具请用 ``registry.names()``。
    label 与 intent 由工具自己提供，前端不需要硬编码任何工具名或中文文案。
    """
    registry = request.app.state.registry
    tools = []
    for name in registry.menu_names():
        tool = registry.get(name)
        if tool is not None:
            tools.append(
                ToolInfo(
                    name=tool.name,
                    label=tool.display_label,
                    description=tool.description,
                    intent=tool.intent_text(),
                )
            )
    return ToolsResponse(tools=tools)


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    # 第 110 步：给这一轮的日志打上"聊天 / 会话"标签，方便按功能核对 token 用量
    set_log_context("chat", payload.session_id, uuid4().hex[:12])
    usage_store.reset_turn_tokens()

    agent_factory = request.app.state.agent_factory
    store = request.app.state.session_store
    # 第 251 步：多用户模式下会话 id 带 owner 前缀；本机模式原样（见 api/auth.py）
    session_key = scoped_session_id(payload.session_id, owner_of(request))

    stored = await store.get_history(session_key)
    user_message = Message(role="user", content=payload.message)
    # 只在本轮使用的工作副本，包含完整轨迹
    working: list[Message] = [*stored, user_message]

    agent = agent_factory()

    deadline = float(get_settings().chat_deadline_seconds or 90.0)
    try:
        # 第 156 步（评价11 P3-1）：服务端自己的总预算。以前这里只有模型侧
        # "单次 60 秒" + 最多 4 步 = 最坏 4 分钟，而食谱/出行都有整体 deadline。
        async with asyncio.timeout(deadline):
            reply = await agent.run(working)
    except TimeoutError as exc:
        logger.warning("聊天超过总预算 %.0f 秒，已中止", deadline)
        raise HTTPException(
            status_code=504,
            detail=(
                f"这次想了超过 {deadline:.0f} 秒还没想完，先停下来了。"
                "稍后再问一次；如果一直这样，多半是模型服务那边在限流。"
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底成用户可读的 500
        logger.exception("处理会话 %s 时发生未捕获异常", payload.session_id)
        raise HTTPException(
            status_code=500,
            detail=f"服务内部错误：{type(exc).__name__}",
        ) from exc

    if reply.error:
        # 第 66 步：模型挂了也照旧回 200 + 一句人话（桌宠不该弹错误框），
        # 但必须留下**可区分的**痕迹，否则监控里它和"模型正常道歉"长得一模一样。
        logger.warning(
            "会话 %s：模型调用失败，已回退成人话回复：%s",
            payload.session_id,
            reply.error,
        )

    # 只持久化安全的对话消息：用户提问 + 助手最终回答（不含 tool 消息）
    await store.append(session_key, user_message)
    if reply.content:
        await store.append(
            session_key,
            Message(role="assistant", content=reply.content),
        )

    return ChatResponse(
        session_id=payload.session_id,
        content=reply.content,
        tool_invocations=[
            ToolInvocationOut(
                tool_name=inv.tool_name,
                ok=inv.ok,
                arguments=inv.arguments,
                error=inv.error,
            )
            for inv in reply.tool_invocations
        ],
        steps=reply.steps,
    )


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request):
    """流式版 /chat（第 117 步 / 评价7 P0-1）。

    事件：``delta`` / ``reset`` / ``error`` / ``meta`` —— 与食谱流式同一套协议，
    ``meta.payload`` 与 ``/chat`` 的响应体**同构**（前端拿它覆盖一次气泡）。

    两条契约（与第 116 步的取消语义配套）：

    - **用户消息开始时落库**：断流时历史里也要有"我问过什么"；
    - **助手消息只在 final 之后落库**：半句回答不是回答，不写进历史。

    为什么要单独一个端点而不是给 /chat 加参数：两条路的**落库时机不同**
    （一次性返回可以最后一起写；流式必须开始就写用户那句），
    把差异藏在同一个函数的 if 里，比放在两个端点更难读。
    """

    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    # 与 /chat 完全一致：同一套日志上下文与轮级计量
    set_log_context("chat", payload.session_id, uuid4().hex[:12])
    usage_store.reset_turn_tokens()

    agent_factory = request.app.state.agent_factory
    store = request.app.state.session_store
    # 第 251 步：多用户模式下会话 id 带 owner 前缀；本机模式原样（见 api/auth.py）
    session_key = scoped_session_id(payload.session_id, owner_of(request))

    stored = await store.get_history(session_key)
    user_message = Message(role="user", content=payload.message)
    working: list[Message] = [*stored, user_message]
    # 开始就写用户那句（断流时也留得下）
    await store.append(session_key, user_message)

    async def events():
        invocations: list[ToolResult] = []
        content = ""
        failure: str | None = None
        cancelled = False
        agent = agent_factory()

        deadline = float(get_settings().chat_deadline_seconds or 90.0)

        try:
            # 第 156 步（评价11 P3-1）：流式同样要有总预算。
            # 没有它的时候，"每步都磨满 60s 超时"能让连接挂 4 分钟且一直烧 token。
            stream = agent.run_stream(working)
            try:
                async with asyncio.timeout(deadline):
                    async for event in stream:
                        if await client_gone(request):
                            cancelled = True
                            logger.info(
                                "聊天流式：客户端断开（session=%s）", payload.session_id
                            )
                            break
                        if event.type == "delta":
                            yield sse({"type": "delta", "text": event.detail})
                        elif event.type == "reset":
                            yield sse({"type": "reset"})
                        elif event.type == "tool_result":
                            if event.result is not None:
                                invocations.append(event.result)
                        elif event.type == "final":
                            content = event.detail
                        elif event.type == "error":
                            failure = event.detail
            finally:
                # 断开/超时后显式关闭：否则 run_stream 里的 gather 任务不会被取消
                await stream.aclose()
        except TimeoutError:
            logger.warning("聊天流式超过总预算 %.0f 秒，已中止（session=%s）", deadline, payload.session_id)
            failure = f"这一轮超过 {deadline:.0f} 秒还没想完，先停下来了。"
        except asyncio.CancelledError:
            # 外层取消（客户端真的走了）：用户那句已经写过，助手一句都不写
            logger.info("聊天流式：任务被取消（session=%s）", payload.session_id)
            raise
        except Exception as exc:  # noqa: BLE001 —— 流已经开始，不能抛 500
            logger.exception("聊天流式异常（session=%s）", payload.session_id)
            failure = f"{type(exc).__name__}"

        if cancelled:
            return

        if failure is not None and not content:
            content = f"抱歉，我这边出了点问题：{failure}"
            yield sse({"type": "error", "message": content})

        if content:
            await store.append(
                session_key, Message(role="assistant", content=content)
            )

        response = ChatResponse(
            session_id=payload.session_id,
            content=content,
            tool_invocations=[
                ToolInvocationOut(
                    tool_name=inv.tool_name,
                    ok=inv.ok,
                    arguments=inv.arguments,
                    error=inv.error,
                )
                for inv in invocations
            ],
            steps=len(invocations),
        )
        yield sse({"type": "meta", "payload": response.model_dump()})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/session/{session_id}", response_model=ChatHistoryResponse)
async def read_session(
    session_id: str, request: Request, limit: int = 200
) -> ChatHistoryResponse:
    """聊天历史：打开窗口时用它把之前的对话画回来（第 93 步 / S3）。

    两个约定：

    - **空会话返回 `[]`，不是 404** —— "还没聊过"是正常状态，不是错误；
    - `limit` 会被夹在 `1..MAX_ROWS_PER_SESSION` 之间。界面能翻 200 条，
      但**喂给模型的仍然只有 40 条**（`SessionStore.get_history`），
      这两个上限不是一回事。
    """
    from core import history

    store = request.app.state.session_store
    capped = max(1, min(int(limit), history.MAX_ROWS_PER_SESSION))
    messages = await store.history_for_display(
        scoped_session_id(session_id, owner_of(request)), capped
    )
    # 第 143 步：**只在历史为空时**给开场白。
    # 有历史还念一遍的话，每次打开窗口都会多一句"你回来啦"，很吵。
    from core.persona import chat_greeting

    greeting = chat_greeting() if not messages else ""
    return ChatHistoryResponse(
        session_id=session_id,
        messages=[
            ChatMessageOut(role=str(message.role), content=str(message.content or ""))
            for message in messages
        ],
        greeting=greeting,
    )


@router.delete("/sessions/{session_id}")
async def reset_session(session_id: str, request: Request) -> dict[str, str]:
    await request.app.state.session_store.reset(
        scoped_session_id(session_id, owner_of(request))
    )
    return {"status": "cleared", "session_id": session_id}


# --------------------------------------------------------------- 景点配图

class PhotoOut(BaseModel):
    """一张图的地址 + 必须展示的署名信息。

    署名不是可选装饰：Unsplash 的 API 条款要求使用时标注摄影师并链回其主页，
    同时链回 Unsplash。所以后端把要展示的文字和链接一起给出，
    前端照着渲染就行，免得漏掉。
    """

    url: str            # 展示用图（urls.regular，约 1080px 宽）
    thumb: str          # 缩略图（urls.small，约 400px 宽）
    alt: str            # 图片描述，用作 alt 文本
    author: str         # 摄影师名
    author_url: str     # 摄影师主页
    source_url: str = "https://unsplash.com/?utm_source=pet_desktop&utm_medium=referral"


class PhotoResponse(BaseModel):
    #: 没找到图时为 None —— 前端据此不渲染图片区，而不是显示破图
    photo: PhotoOut | None = None
    #: 同一关键词下的**候选**图（第 44 步）。`photo` 就是 `candidates[0]`，
    #: 保留 `photo` 是为了不破坏既有调用方与测试。
    #: 给多张的原因：同一份行程里泛化关键词很容易搜到同一张典型图，
    #: 前端要能在"没用过的"里面挑一张。
    candidates: list[PhotoOut] = []


#: 搜索结果缓存：Unsplash 的 Demo 应用每小时只有 50 次搜索额度，
#: 而且官方文档也要求"把结果缓存起来"。这里做进程内的小缓存，重启即清空。
#: 缓存的是**候选列表**（None = 没搜到/失败），而不是选好的那一张。
_PHOTO_CACHE: dict[str, tuple[float, list[PhotoOut] | None]] = {}
#: 一次最多返回几张候选图。给多张是为了让前端在"同一份行程内不重复"时有得挑：
#: "公园 北京""广场 北京"这种泛化关键词，Unsplash 的第一张经常是同一张典型图。
PHOTO_CANDIDATES: Final[int] = 5
PHOTO_CACHE_TTL: Final[float] = 24 * 3600
#: 缓存条目上限（第 179 步，评价13 N-2）：每个唯一关键词原本会**永久**驻留一条
#: （过期只会触发重新 fetch 覆盖，从不淘汰）。250 条 × 几条 URL 的内存可以忽略，
#: 但"没有上限"本身是个隐患，超了就丢最久没被写的那条。
PHOTO_CACHE_MAX: Final[int] = 250
#: 失败（含限流）只缓存一小会儿：限流是临时的，缓存一整天会让当天再也试不出来。
#: 第 32 步用户报"图没了"时发现的问题。
PHOTO_FAIL_TTL: Final[float] = 120.0
PHOTO_TIMEOUT: Final[float] = 10.0


def clear_photo_cache() -> None:
    """清空配图缓存（测试与手动刷新用）。"""
    _PHOTO_CACHE.clear()


def _photo_response(photos: list[PhotoOut] | None) -> PhotoResponse:
    """把候选列表包成响应；空/None 一律是"没有图"，而不是报错。"""
    if not photos:
        return PhotoResponse()
    return PhotoResponse(photo=photos[0], candidates=list(photos))


@router.get("/photo", response_model=PhotoResponse)
async def search_photo(query: str, request: Request) -> PhotoResponse:
    """按关键词找配图（出行计划里的景点卡片用），返回若干张候选。

    没有 key、没有结果、请求失败时都返回 ``{"photo": null, "candidates": []}``，
    前端据此显示"暂无图片"占位——**不让配图问题把整页搞崩，也不留一片空白让人以为坏了**。
    """
    keyword = (query or "").strip()[:120]
    if not keyword:
        return PhotoResponse()

    cached = _PHOTO_CACHE.get(keyword)
    if cached:
        ttl = PHOTO_CACHE_TTL if cached[1] else PHOTO_FAIL_TTL
        if time.time() - cached[0] < ttl:
            logger.debug("配图命中缓存（%s，找到=%s）", keyword, bool(cached[1]))
            return _photo_response(cached[1])

    settings = get_settings()
    if not settings.unsplash_api_key.strip():
        logger.warning("未配置 UNSPLASH_API_KEY，景点配图不可用")
        return PhotoResponse()

    client = getattr(request.app.state, "http_client", None)
    photos = await _fetch_photos(keyword, settings, client)
    _PHOTO_CACHE[keyword] = (time.time(), photos)
    # 超出上限时淘汰最老的（字典按插入顺序，但重写会保留原位置，
    # 所以这里按**存的时间戳**找最老的，语义更明确）
    while len(_PHOTO_CACHE) > PHOTO_CACHE_MAX:
        oldest = min(_PHOTO_CACHE, key=lambda key: _PHOTO_CACHE[key][0])
        _PHOTO_CACHE.pop(oldest, None)
    return _photo_response(photos)


async def _fetch_photos(
    keyword: str, settings: Any, client: Any
) -> list[PhotoOut] | None:
    """调 Unsplash 搜索接口取若干张横图。失败一律返回 None（不抛异常）。

    返回**多张候选**而不是一张（第 44 步）：同一份行程里"公园 北京""广场 北京"
    这类泛化关键词很容易搜到同一张典型图，后端多给几张，前端才有得挑。

    失败时**必须打日志**：前端拿不到图是"安静地不显示"，如果后端也不出声，
    排查时就完全摸黑（第 32 步用户报"图没了"，就是靠这些日志定位的）。
    """
    params = {
        "query": keyword,
        "per_page": PHOTO_CANDIDATES,
        "orientation": "landscape",
        "content_filter": "high",   # 过滤掉不合适的内容
    }
    headers = {
        "Authorization": f"Client-ID {settings.unsplash_api_key.strip()}",
        "Accept-Version": "v1",
    }

    async def do_request(http: Any) -> Any:
        return await http.get(
            "https://api.unsplash.com/search/photos",
            params=params,
            headers=headers,
            timeout=PHOTO_TIMEOUT,
        )

    try:
        if client is not None:
            response = await do_request(client)
        else:
            async with httpx.AsyncClient() as http:
                response = await do_request(http)
        if response.status_code != 200:
            # 401=key 类型不对；403/429=额度用完（Demo 应用每小时 50 次搜索）
            logger.warning(
                "Unsplash 返回 HTTP %s（关键词 %s）：%s",
                response.status_code,
                keyword,
                str(response.text)[:200],
            )
            return None
        response.raise_for_status()
        results = (response.json() or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001 - 配图失败不该影响主流程
        logger.warning("取景点配图失败（%s）：%s: %s", keyword, type(exc).__name__, exc)
        return None

    if not results:
        logger.info("Unsplash 没有匹配的图（关键词 %s）", keyword)
        return None

    photos: list[PhotoOut] = []
    for item in results[:PHOTO_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        urls = item.get("urls") or {}
        user = item.get("user") or {}
        links = user.get("links") or {}
        regular = urls.get("regular") or urls.get("small")
        if not regular:
            # 没有可用地址的条目直接跳过，而不是塞一个空 url 给前端
            continue
        photos.append(
            PhotoOut(
                url=regular,
                thumb=urls.get("small") or regular,
                alt=(item.get("alt_description") or keyword)[:160],
                author=user.get("name") or "Unsplash 摄影师",
                author_url=links.get("html") or "https://unsplash.com",
            )
        )

    if not photos:
        logger.warning("Unsplash 返回了结果但没有可用地址（关键词 %s）", keyword)
        return None
    return photos
