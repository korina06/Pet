"""食谱窗口的 HTTP 接口：schema、对话式检索 `/ask`、会话读取与重置。

`/ask` 把 Agent 循环、`search_recipes` 工具与数据库串起来：模型选菜并说明理由，
菜谱事实一律来自库。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.agent import Agent, build_recipe_context, build_recipe_prompt
from api.auth import owner_of, scoped_session_id
from api.recipe_library_routes import RecipeOut
from core.config import get_settings
from core.recipe_library import (
    add_aversions,
    get_recipe,
    list_aversions,
    list_recipes,
)
from core.recipes import (
    DISCLAIMER,
    GREETING,
    SAMPLE_MESSAGE,
    RecipeSessionStore,
    extract_negative_taste,
    extract_turn_exclude,
    wanted_constraints,
    extract_stated_aversions_detailed,
    mentioned_tags,
    session_payload,
    SESSION_MAX_MESSAGES,
)
from api.streaming import client_gone, remember_question
from core.schemas import AgentReply, Message, ToolResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recipe", tags=["recipe"])


# --------------------------------------------------------------- 响应模型


class SchemaResponse(BaseModel):
    """界面文案。**全部由后端给**，前端不硬编码中文。"""

    greeting: str
    #: 输入框里的示例句
    example: str
    #: 免责声明：界面上已不再展示（第 50 步去掉），字段留着给别处用
    disclaimer: str


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


class AskRequest(SessionRequest):
    #: 用户这一句原话，例如「晚饭想吃鱼」
    message: str = Field(min_length=1, max_length=500)
    #: 右栏「这次想要」勾着的标签（第 50 步 50e）。**只影响这一顿**，
    #: 与「一直记住 · 不吃」（存库的忌口词条）不是一回事。
    wanted: list[str] = Field(default_factory=list, max_length=12)


class AskResponse(BaseModel):
    """对话式检索的返回。

    **卡片数据一律由后端按 id 从库里取**（不让模型输出菜的内容）：
    模型只负责"选哪几道 + 说一句为什么"，事实部分始终来自数据库。
    这样它编不出库里没有的菜——`recipes` 里的每一条都真的在库里。
    """

    session_id: str
    reply: str
    recipes: list[RecipeOut] = []
    #: 命中的工具名（调试/测试用：能看出它到底查了没）
    tool_calls: list[str] = []


class MessageOut(BaseModel):
    role: str
    content: str
    #: 这条回答给出的菜 id（第 137 步）：前端恢复会话时用它把卡片也画回来
    recipes: list[int] = []


class SessionResponse(BaseModel):
    session_id: str
    messages: list[MessageOut] = []


class ResetResponse(BaseModel):
    status: str
    session_id: str


# --------------------------------------------------------------- 依赖


def _store(request: Request) -> RecipeSessionStore:
    store = getattr(request.app.state, "recipe_store", None)
    if store is None:
        # 正常情况下 main.py 的 lifespan 已经建好了；这里防的是"直接构造 app"的测试
        raise HTTPException(status_code=503, detail="食谱功能未初始化")
    return store


def _llm(request: Request) -> Any:
    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        raise HTTPException(status_code=503, detail="食谱功能未初始化（缺少模型客户端）")
    return llm


# --------------------------------------------------------------- 接口


@router.get("/schema", response_model=SchemaResponse)
async def schema() -> SchemaResponse:
    """界面文案（开场白 / 示例句 / 免责声明）。静态内容，不查库。"""
    return SchemaResponse(
        greeting=GREETING,
        example=SAMPLE_MESSAGE,
        disclaimer=DISCLAIMER,
    )


@router.get("/session", response_model=SessionResponse)
async def read_session(session_id: str, request: Request) -> SessionResponse:
    """恢复对话记录。没聊过就返回空列表（不是 404——空会话是正常状态）。"""
    payload = session_payload(
        _store(request).get(scoped_session_id(session_id, owner_of(request))),
        session_id,
    )
    return SessionResponse(**payload)


@router.post("/reset", response_model=ResetResponse)
async def reset(payload: SessionRequest, request: Request) -> ResetResponse:
    """清掉这个会话（"创建新会话"的地基：前端换个 session_id 即可）。"""
    _store(request).reset(scoped_session_id(payload.session_id, owner_of(request)))
    return ResetResponse(status="cleared", session_id=payload.session_id)


def _registry_with_wanted(
    registry: Any,
    wanted: list[str],
    *,
    aversions: list[str] | None = None,
    forbid_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    turn_exclude: list[str] | None = None,
) -> Any:
    """把右栏勾选作为强约束挂到检索工具上；没有约束时原样返回。

    实现方式是**克隆一份注册表**、只替换 `search_recipes`——
    不去改全局的那个实例（它是应用级的，不能被单个请求污染）。
    """
    from tools.recipe_search_tool import with_enforced_constraints

    tool = registry.get("search_recipes")
    if tool is None:
        return registry
    constrained = with_enforced_constraints(
        tool,
        wanted,
        aversions=aversions,
        forbid_tags=forbid_tags,
        exclude_tags=exclude_tags,
        turn_exclude=turn_exclude,
    )
    if constrained is tool:
        return registry            # 没有约束，不用克隆

    clone = registry.clone()
    clone.replace(constrained)
    logger.info("本次勾选已强制注入检索：%s", getattr(constrained, "applied", {}))
    return clone


def _payload_of(invocation: Any) -> dict[str, Any] | None:
    """取一次检索调用的结构化结果（第 211 步）。

    为什么先看 `data`：这一步把**给模型看的紧凑文本**和**给代码看的结构**
    分开了（模型那边省了 ~60% 的 token，JSON 键名的重复属于纯噪音）。
    老记录/别的工具没有 `data`，就退回解析 `content` 的 JSON —— 兼容路径，
    也让"某天有人又写了 dump JSON 的工具"不至于静默丢卡片。
    """
    data = getattr(invocation, "data", None)
    if isinstance(data, dict):
        return data
    as_payload = getattr(invocation, "as_payload", None)
    if callable(as_payload):
        return as_payload()
    try:
        return json.loads(getattr(invocation, "content", "") or "{}")
    except ValueError:
        return None


def _collect_recipe_ids(tool_invocations: list[Any], reply_text: str = "") -> list[int]:
    """从工具调用记录里捞出检索到的菜 id（保序、去重）。

    只认 `search_recipes` 的成功结果；`nearby` 也算——库里没有完全匹配时，
    模型会从 nearby 里推荐，那些也应当是真实存在的菜。

    **第 82 步收口**：优先只保留"回答里点过名"的菜。模型有时会查两次
    （先窄后宽），以前会把两次结果全画成卡片 —— 界面上的卡比回答里说的多。
    现在按菜名过滤；如果一个都对不上（模型只说"上面这几道"），**退回全集**，
    绝不让卡片凭空消失。

    取舍：模型用简称（"鲈鱼" vs "清蒸鲈鱼"）时那张卡会少一张。
    少一张比多一张安全 —— 卡片的承诺是"回答里提到的菜在这里"，不是"库里还有别的"。
    """
    pairs: list[tuple[int, str]] = []
    for invocation in tool_invocations:
        if getattr(invocation, "tool_name", "") != "search_recipes":
            continue
        if not getattr(invocation, "ok", False):
            continue
        payload = _payload_of(invocation)
        if not isinstance(payload, dict):
            continue
        for key in ("items", "nearby"):
            for row in payload.get(key) or []:
                if not isinstance(row, dict):
                    continue
                recipe_id = row.get("id")
                name = str(row.get("name") or "")
                if isinstance(recipe_id, int) and all(
                    recipe_id != seen for seen, _ in pairs
                ):
                    pairs.append((recipe_id, name))

    if reply_text:
        mentioned = [
            (recipe_id, name)
            for recipe_id, name in pairs
            if name and name in reply_text
        ]
        if mentioned:
            return [recipe_id for recipe_id, _ in mentioned]
    return [recipe_id for recipe_id, _ in pairs]


def _ids_mentioned_in(text: str, storage: Any, *, limit: int = 6) -> list[int]:
    """回答里点名、库里确实有的菜 → id（按出现位置排序）。

    用途（第 137 步）：模型**不调工具**、凭上下文直接回答时，这一轮的
    `tool_invocations` 是空的，卡片会全部消失（"问第二遍就不给卡片了"）。
    既然回答里点了名、库里也有，就按名字补回来 ——
    卡片仍然只来自数据库，不存在"编一道菜"。
    """
    if not text:
        return []
    try:
        rows = list_recipes(storage, limit=500)
    except Exception:  # noqa: BLE001 —— 补卡片失败不该影响整轮回答
        return []

    found: list[tuple[int, int]] = []
    # 长名字优先：避免"红烧肉"抢占"本帮红烧肉"的位置
    for recipe in sorted(rows, key=lambda item: -len(item.name)):
        index = text.find(recipe.name)
        if index < 0:
            continue
        if all(index != seen for seen, _ in found):
            found.append((index, recipe.id))
    found.sort()
    return [recipe_id for _, recipe_id in found][:limit]


@router.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest, request: Request) -> AskResponse:
    """对话式检索：「这顿吃什么」→ 查库 → 给几道 → 卡片由库里按 id 渲染。

    这是第 50 步真正把对话变成 **RAG** 的地方：模型先调 `search_recipes`，
    读到真实菜谱再回答。三个刻意的决定：

    1. **走 Agent 循环**（不是手写两段 prompt）：工具编排交给现成的循环，
       它本来就有 max_steps、失败回灌这些护栏；
    2. **卡片数据来自数据库**：模型只决定"选哪几道"，菜名/材料/做法一律回库取，
       所以它编不出库里没有的菜；
    3. **不追问**：用户说"晚饭想吃鱼"就该先看到鱼，缺什么由模型在回答里顺口带一句，
       不再像旧流程那样一步步把 13 个维度问完（用户明确要求去掉那套）。
    """
    setup = _setup_ask(payload, request)
    deadline = float(get_settings().recipe_deadline_seconds or 35.0)
    try:
        async with asyncio.timeout(deadline):
            reply = await setup["agent"].run(setup["working"])
    except TimeoutError as exc:
        # 第 83 步：服务端自己的预算。以前只有前端那道 45 秒 abort ——
        # 那只是客户端放弃，服务端还在跑，而且用户看不到任何可读的收尾。
        logger.warning("食谱问答超过总预算 %.0f 秒，已中止", deadline)
        raise HTTPException(
            status_code=504,
            detail=(
                f"这次想了超过 {deadline:.0f} 秒还没想完，先停下来了。"
                "稍后再问一次；如果一直这样，多半是模型服务那边在限流。"
            ),
        ) from exc
    except ValueError as exc:      # 未配置 DEEPSEEK_API_KEY 这类配置错误
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 这里**故意没有** `except LLMError`（第 66 步）：`Agent.run()` 会把模型故障
    # 收敛成一句人话，所以那个分支永远进不来（曾经是死代码）。
    # 而且"200 + 抱歉"本身是刻意的——聊天里弹错误框比一句道歉更差。
    # 要区分"模型挂了"和"模型正常道歉"，看 `reply.error`：
    # 前者会在这里留下一条 warning，后者不会。
    if reply.error:
        logger.warning("模型调用失败，已回退成人话回复：%s", reply.error)
        reply = await _fallback_reply(payload, setup, reply)

    return _finish_ask(payload, setup, reply)


@router.post("/ask/stream")
async def ask_stream(payload: AskRequest, request: Request):
    """流式版 /ask：边生成边把字吐给浏览器（第 73 步）。

    协议（SSE，每条一个 JSON）：

    - ``{"type":"delta","text":"…"}``  模型刚吐出来的一小段；
    - ``{"type":"reset"}``            刚才那些 delta 不算答案（这一轮其实要调工具），
                                      前端把气泡清空；
    - ``{"type":"error","message":"…"}`` 模型挂了，人话已经给好；
    - ``{"type":"meta","payload":{…}}``  收尾，**与 /ask 的响应体同构**
      （reply / recipes / tool_calls 都在里面）。

    为什么收尾还要再发一次完整内容：delta 是"边到边显示"，可能存在截断、
    或前端中途重连。以 ``meta`` 为准**覆盖**一次，前端就不必担心拼错。

    为什么 ``meta`` 里的卡片仍然是回库取的：流式只改变"什么时候把字送出去"，
    不改变"事实从哪来"。
    """

    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    setup = _setup_ask(payload, request)

    deadline = float(get_settings().recipe_deadline_seconds or 35.0)

    async def events():
        invocations: list[ToolResult] = []
        content = ""
        failure: str | None = None
        cancelled = False

        try:
            # 流式也必须**自己收尾**（第 83 步）：超时不能把连接吊在那里，
            # 前端会一直等。超时后照样发 error + meta，让气泡有结论。
            async with asyncio.timeout(deadline):
                async for event in setup["agent"].run_stream(setup["working"]):
                    # 第 116 步：先看人还在不在。`asyncio.timeout` 只能兜"太慢"，
                    # 兜不住"没人要了" —— 不检查的话会一直跑到 deadline 继续烧 token。
                    if await client_gone(request):
                        cancelled = True
                        logger.info(
                            "客户端断开，已停止本轮（session=%s）", payload.session_id
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
        except TimeoutError:
            # 超时：既不能挂着连接，也不能静默结束 —— 发 error + meta 收尾
            failure = f"超过 {deadline:.0f} 秒还没想完"
            content = f"这次想了超过 {deadline:.0f} 秒，先停下来。稍后再问一次。"
            logger.warning("食谱流式超过总预算 %.0f 秒，已中止", deadline)
            yield sse({"type": "error", "message": content})
        except asyncio.CancelledError:
            # 外层（Starlette / uvicorn）取消了这个任务：同样只记提问、不写半句回答
            logger.info("流式任务被取消（session=%s）", payload.session_id)
            remember_question(setup["store"], setup["session_key"], payload.message)
            raise
        except ValueError as exc:      # 配置错误（没填 key）
            yield sse({"type": "error", "message": str(exc)})
            return

        if cancelled:
            # 断开之后**不再发任何事件**（连接都没了），也不写助手消息；
            # 只把用户问的那句留在历史里，免得他以为自己的问题丢了。
            remember_question(setup["store"], setup["session_key"], payload.message)
            return

        if failure is not None and not content:
            content = f"抱歉，我这边出了点问题：{failure}"
            yield sse({"type": "error", "message": content})

        reply = AgentReply(
            content=content, tool_invocations=invocations, error=failure
        )
        if failure is not None:
            # A1：流式也要兜底，但只能**在 meta 之前**替换内容，
            # 因为前端是拿 meta 覆盖气泡的（第 74 步）。
            reply = await _fallback_reply(payload, setup, reply)
        response = _finish_ask(payload, setup, reply)
        yield sse({"type": "meta", "payload": response.model_dump()})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反向代理（nginx）默认会缓冲响应体：不关掉的话"流式"会被攒成一坨
            "X-Accel-Buffering": "no",
        },
    )


def _setup_ask(payload: AskRequest, request: Request) -> dict:
    """把 /ask 与 /ask/stream 共用的装配收在一处。

    两条路必须有**完全一样**的服务端注入（忌口、历史标签、系统提示），
    否则流式版会悄悄少一层约束 —— 那种偏差在测试里最难发现。
    """
    # 第 110 步：这一轮的日志带上"食谱 / 会话"标签（两条路共用，所以只写一处）
    from core.log_context import set_context as set_log_context
    from core import usage as usage_store
    from uuid import uuid4

    set_log_context("recipe", payload.session_id, uuid4().hex[:12])
    usage_store.reset_turn_tokens()

    store = _store(request)
    llm = _llm(request)
    # 食谱只拿到自己那一组（第 85 步）：search_recipes + 天气 + geocode，
    # **不包含 find_nearby_places**；缺了就回退但会 warning（第 89 步）。
    from tools.registry import feature_registry_for

    registry = feature_registry_for(
        request.app.state, "recipe_registry", feature="食谱"
    )
    storage = getattr(request.app.state, "storage", None)
    if registry is None or storage is None:
        raise HTTPException(status_code=503, detail="食谱功能未初始化（缺少工具或存储）")

    # 第 251 步：多用户模式下会话 id 带 owner 前缀；本机模式原样
    session_key = scoped_session_id(payload.session_id, owner_of(request))
    session = store.ensure(session_key)
    # 显示上限（200）与上下文上限（40）不是一回事（第 91 步 / A3）：
    # 库里/内存里留着 200 条供界面翻，喂给模型只给最近 40 条。
    history = list(session.messages)[-SESSION_MAX_MESSAGES:]
    first_turn = not any(message.role == "user" for message in history)

    # 三条"已知状态"都在服务端注入，不靠模型记忆（第 55–57 步）：
    #   1. 右栏这次勾的标签/时间（用户点明的）；
    #   2. 长期记住的忌口（存库，安全约束，每轮都注入）；
    #   3. 上一轮用过、这轮既没勾也没提的标签（不许模型拿历史条件来补）。
    aversions = list_aversions(storage)

    # 第 63 步：用户**在这句话里说出来的**过敏原要落库，不能只靠模型自觉。
    stated, unrecognized = extract_stated_aversions_detailed(payload.message)
    if stated:
        before = aversions
        aversions = add_aversions(storage, stated)
        added = [term for term in aversions if term not in before]
        if added:
            logger.info("从对话里记下忌口：%s（现存 %s）", added, aversions)
    if unrecognized:
        # 第 79 步：**部分识别必须上报**。用户说了「花生和巧克力蛋糕」，
        # 只记下花生却什么都不说的话，他会以为两样都生效了 —— 那比漏记更危险。
        logger.warning("忌口没识别完整，未识别：%s", unrecognized)

    mentioned = mentioned_tags(payload.message)
    forbid_tags = [
        tag for tag in session.recent_tags
        if tag not in payload.wanted and tag not in mentioned
    ]
    # 这一轮说过的否定口味（第 77 步）：服务端抽，不靠模型传参
    exclude_tags = extract_negative_taste(payload.message)
    if exclude_tags:
        logger.info("本轮否定口味：%s", exclude_tags)
    # 这一轮说的「别放葱」：**不落库**，但本次请求要生效（第 90 步 / A2）
    turn_exclude = extract_turn_exclude(payload.message)
    if turn_exclude:
        logger.info("本轮临时排除（不写长期）：%s", turn_exclude)
    registry = _registry_with_wanted(
        registry,
        payload.wanted,
        aversions=aversions,
        forbid_tags=forbid_tags,
        exclude_tags=exclude_tags,
        turn_exclude=turn_exclude,
    )

    # 忌口变了就得在对话里说一声（第 57 步）：只改系统提示不够——
    # 模型会拿自己上一轮说过的"你对奶忌口"当事实继续拒绝。
    # 这条说明排在**当前这句之前**，比旧话更新，模型才会改口。
    refresh: list[Message] = []
    if history and session.aversions_snapshot != aversions:
        current = "、".join(aversions) if aversions else "没有"
        refresh.append(
            Message(
                role="user",
                content=f"（系统更新：我的忌口现在是：{current}。以这条为准，之前的说法作废。）",
            )
        )
    session.aversions_snapshot = list(aversions)

    # 没识别出来的部分要在**这句之前**告诉模型，让它请用户确认
    # （和"忌口已更新"那条同一个位置：比用户这轮的话更早，模型才会照做）。
    if unrecognized:
        refresh.append(
            Message(
                role="user",
                content=(
                    "（系统提示：我刚才提到的"
                    f"「{'、'.join(unrecognized)}」没能识别成忌口词条。"
                    "请提醒我自己再确认一下具体是什么，或者让我去右栏勾选；"
                    "**不要**说已经帮我避开了它。）"
                ),
            )
        )

    agent = Agent(
        llm=llm,
        registry=registry,
        # 走配置而不是写死 4（第 69 步，第 73 步补了测试）：
        # 这里写死会让"改一个环境变量"只对聊天生效、对食谱不生效。
        max_steps=get_settings().agent_max_steps,
        turn_token_budget=get_settings().turn_token_budget,
        # 第 212 步：系统提示只放**逐字不变**的规则，本轮条件走下面那条消息。
        # 这样系统提示 + 历史构成稳定前缀，DeepSeek 的前缀缓存才记得住。
        system_prompt=build_recipe_prompt(),
    )
    return {
        "store": store,
        "storage": storage,
        "session": session,
        # 第 251 步：落历史/记提问都用带 owner 的 key，与 ensure 时一致
        "session_key": session_key,
        "agent": agent,
        # A1：模型挂了时要用它做本地兜底。它是**约束已注入**的那一个，
        # 所以兜底查询不可能绕过忌口/否定口味/勾选条件。
        "registry": registry,
        "working": [
            *history,
            *refresh,
            # 本轮条件：**并进这一轮的提问**（第 213 步），不再单独发一条消息。
            # 为什么改了：单独一条消息时它不进历史，下一轮的前缀在系统提示之后
            # 立刻分歧，命中就只剩系统提示本身（实测 1024 token、四轮不变）；
            # 并进提问后，历史与上一轮请求逐字一致，命中随轮数增长
            # （实测 1280 → 1536 → 1664，未命中 2510 → 2158）。
            # 只改**发给模型**的副本：落库的仍是用户原话（见下方 store.append）。
            Message(
                role="user",
                content=(
                    f"{payload.message}\n\n"
                    + build_recipe_context(
                        first_turn=first_turn,
                        wanted=payload.wanted,
                        aversions=aversions,
                        mentioned=mentioned,
                    )
                ),
            ),
        ],
    }


async def _fallback_reply(
    payload: AskRequest, setup: dict, failed: AgentReply
) -> AgentReply:
    """模型不可用时，**在能判定条件的前提下**退回本地检索（A1-最小）。

    为什么只在"有正向条件"时才给卡片：勾了标签/时间才知道用户想要什么；
    只有忌口或"不要辣的"时，查出来的菜和这句话**没有关系** ——
    给出来就是"库里随便挑几道还假装懂你"，比一句道歉更糟。

    安全性：用的是 `_setup_ask` 里那个**约束已注入**的检索工具（strict=True），
    所以忌口、本轮否定口味、勾选条件全都照旧生效，兜底不可能绕过它们。
    """
    constraints = wanted_constraints(payload.wanted)
    has_positive = bool(constraints.get("tags")) or any(
        constraints.get(key) is not None
        for key in ("min_minutes", "max_minutes")
    )
    if not has_positive:
        return failed

    tool = (setup.get("registry") or {}).get("search_recipes") if setup.get("registry") else None
    if tool is None:
        return failed

    result = await tool.run()          # 模型不在，参数只能来自服务端注入
    if not result.ok:
        return failed
    data = _payload_of(result)
    if not isinstance(data, dict):
        return failed
    items = (data.get("items") if isinstance(data, dict) else None) or []
    names = [str(item.get("name")) for item in items[:3] if item.get("name")]
    if not names:
        return failed

    logger.warning("模型不可用，已退回本地检索（%s 道）", len(names))
    return AgentReply(
        content=(
            "我这边暂时连不上模型，先按你勾的条件从库里挑了几道："
            + "、".join(names)
            + "。"
        ),
        tool_invocations=[result],
        steps=failed.steps,
        error=failed.error,
    )


def _finish_ask(payload: AskRequest, setup: dict, reply: AgentReply) -> AskResponse:
    """两条路共用的收尾：回库取卡片、记账、落历史。

    关键不变式：**卡片数据只从数据库来**，模型说的话一个字都不进卡片。
    流式与非流式都走这一段，所以这条约束不会被其中一条路绕过。
    """
    storage = setup["storage"]
    session = setup["session"]
    store = setup["store"]

    ids = _collect_recipe_ids(reply.tool_invocations, reply.content or "")
    if not ids:
        # 第 137 步：模型有时**不调工具**、凭上下文直接回答（同一类问题问第二遍时
        # 特别常见），这时这一轮没有任何工具调用，卡片就会全没了。
        # 既然回答里点了名、库里也有，就按菜名补回来 —— 事实仍然只来自数据库。
        ids = _ids_mentioned_in(reply.content or "", storage)
    recipes = []
    for recipe_id in ids:
        recipe = get_recipe(storage, recipe_id)
        if recipe is not None:
            recipes.append(RecipeOut.of(recipe))

    # 记住这一轮实际用过的标签，供下一轮判断"哪些是历史遗留"
    used_tags: list[str] = list(payload.wanted)
    for invocation in reply.tool_invocations:
        if getattr(invocation, "tool_name", "") != "search_recipes":
            continue
        for tag in (getattr(invocation, "arguments", {}) or {}).get("tags") or []:
            text = str(tag)
            if text not in used_tags:
                used_tags.append(text)
    session.recent_tags = (session.recent_tags + used_tags)[-12:]
    # 第 102 步：这两个字段改完就落库 —— 否则重启后消息回来了、约束没回来
    store.save_meta(session)

    store.append(
        setup["session_key"],
        Message(role="user", content=payload.message),
        # 第 137 步：把这一轮给出的菜 id 一起存下来。
        # 没有它，关掉窗口再打开就只剩文字、卡片全没了（用户实测报的问题）。
        Message(
            role="assistant",
            content=reply.content,
            recipes=[recipe.id for recipe in recipes],
        ),
    )

    return AskResponse(
        session_id=payload.session_id,
        reply=reply.content,
        recipes=recipes,
        tool_calls=[inv.tool_name for inv in reply.tool_invocations],
    )
