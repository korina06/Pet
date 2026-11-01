"""FastAPI 入口：装配路由、中间件与静态资源，并导出 `app`。

本地起服务用 `scripts/start.ps1`，它把 `--host` 钉在 127.0.0.1 上 —— 手动起时也别加
`--host 0.0.0.0`，这个服务默认没有鉴权。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from agents.agent import Agent, build_system_prompt
from agents.model_router import ModelRouter
from api.checkin_routes import router as checkin_router
from api.mail_routes import router as mail_router
from api.diagnostics_routes import router as diagnostics_router
from api.model_routes import router as model_router
from api.pet_routes import router as pet_router
from api.usage_routes import router as usage_router
from api.recipe_library_routes import router as recipe_library_router
from api.recipe_photo_routes import router as recipe_photo_router
from api.recipe_routes import router as recipe_router
from api.routes import router as api_router
from api.map_routes import router as map_router
from api.trip_routes import router as trip_router
# 第 216 步：出行窗口的「聊天式规划」。它与 trip_router 那条确定性流水线并存，
# 前缀也用 /api/trip（同一个窗口），所以放在这里一起挂。
from api.trip_chat_routes import router as trip_chat_router
from core import recipe_library
from core.recipe_images import open_image_store
from core.checkin import CheckinArchive
from core.config import get_settings
from core.embeddings import build_embedder
from core.mailbox import MailSessionStore
from core.recipes import RecipeSessionStore
from core.session import SessionStore
from core.storage import display_path, open_storage
from core.trip_store import TripStore
from tools.mcp_catalog import (
    build_remote_tools,
    cache_path,
    load_cache,
    servers_from_settings,
    swap_almanac_tools,
    swap_web_search_tools,
)
from tools.mcp_remote import DiscoveredTool
from tools.registry import (
    CHAT_TOOLS,
    RECIPE_TOOLS,
    TRAVEL_OPTIONAL_TOOLS,
    TRAVEL_TOOLS,
    build_default_registry,
    build_feature_registry,
)
from tools.route_tool import RouteTool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _TolerantRotatingFileHandler(RotatingFileHandler):
    """轮转失败就跳过这一次，**不抛 "--- Logging error ---"**（第 214 步）。

    为什么必须这么做：`main.py` 会被导入不止一次（测试按路径加载它、
    启动自检、真跑服务各一次），每导入一次就多挂一个指向同一个
    `data/logs/app.log` 的 handler。文件涨到 2MB 时，一个 handler 轮转要
    `os.rename`，而其它 handler 还开着这个文件 —— Windows 不允许重命名被
    占用的文件，于是抛 `PermissionError: [WinError 32]`。logging 捕获后**每次
    写日志都打一整段 traceback**：一轮全量跑下来 5576 段、1.5MB 噪音，
    真正的失败信息全被埋掉。

    轮转少做一次只是日志文件比上限大一点，比丢一整屏有用得多。
    """

    def doRollover(self) -> None:  # noqa: D102 —— 覆盖父类，文档见类注释
        try:
            super().doRollover()
        except OSError:
            # 文件被别的进程/句柄占着（WinError 32 是最常见的一种）：这次不转。
            return


def _attach_file_log() -> Path | None:
    """再加一份落文件的日志（第 88 步 / B3）。

    为什么需要：控制台日志在关掉窗口那一刻就没了，而"昨天那次为什么失败"
    恰恰是唯一需要日志的时候。换机器时也知道该看哪个文件。

    失败**不崩**：数据目录不可写时只警告并继续 —— 和 storage 那层的取舍一致
    （桌宠是"能开就有用"的工具）。

    第 214 步补了两件事：
    1. **同一个文件只挂一次**（重复 import 不再叠 handler）；
    2. 轮转用 `_TolerantRotatingFileHandler`，占不着文件时只跳过轮转。
    """
    log_dir = Path(__file__).resolve().parent / "data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "app.log"

        for existing in logging.getLogger().handlers:
            base = getattr(existing, "baseFilename", "")
            if base and Path(base).resolve() == target.resolve():
                return target          # 已经挂过同一个文件：直接复用

        handler = _TolerantRotatingFileHandler(
            target,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
        return log_dir / "app.log"
    except OSError as exc:      # 权限/磁盘满：只警告，不影响启动
        logger.warning("日志文件开不了，只输出到控制台：%s", exc)
        return None


LOG_FILE = _attach_file_log()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_judge(embedder: object, llm: object) -> object | None:
    """挑一个真的能用的模型当语义裁决者（第 193 步 / 评价15 T1-6②）。

    第 188 步的实现是 `LlmJudge(DeepSeekLLM(...))` —— **写死了 DeepSeek**。
    于是只有 GLM key 的机器上：嵌入器建得起来（它要 GLM key），裁决者却是一次
    注定失败的调用，每次语义查询都白炸一次、留一条 warning，而向量路其实全军覆没。

    现在改成用 `ModelRouter`：它本来就按 "哪家 key 配了、哪家在用时" 选提供方，
    用户换模型（界面上的开关）时裁决者跟着换 —— 这才是"provider 独立"该有的样子。

    两个前置条件都满足才建：
    1. 有嵌入器（没有它连候选都没有，裁决无从谈起）；
    2. **至少有一家提供方可用**（否则每次 miss 都白炸一次，不如干脆关掉向量路）。
    """
    if embedder is None:
        return None

    available = [
        provider for provider in getattr(llm, "providers", ()) if provider.available
    ]
    if not available:
        logger.warning(
            "没有可用的对话模型，语义裁决关闭 —— 向量路退化为纯字面检索（不是报错）"
        )
        return None

    from core.semantic_judge import LlmJudge

    logger.info("语义裁决使用：%s", available[0].label)
    return LlmJudge(llm)


def _warn_if_no_vectors(storage: object, embedder: object, judge: object) -> None:
    """嵌入器/裁决者都在，但库里一条向量都没有 → 说清楚「语义召回现在是空转」（第 194 步）。

    第 193 步之前这条路连原话都拿不到（`query_text` 不在 schema 里），改完之后它终于
    能被触发 —— 但如果没人跑过 `scripts/build_recipe_vectors.py`，`recipe_vectors` 表是空的，
    每次语义查询都白做一次嵌入再空手回来。与其让人在日志里猜，不如启动时说一句。
    """
    if embedder is None or judge is None:
        return

    try:
        row = storage.execute("SELECT COUNT(*) FROM recipe_vectors").fetchone()  # type: ignore[attr-defined]
        count = int(row[0]) if row is not None else 0
    except Exception as error:  # noqa: BLE001 —— 只是一个提示，查不了就算了
        logger.debug("查向量条数失败（不影响启动）：%s", error)
        return

    if count == 0:
        logger.warning(
            "语义召回暂时是空转：recipe_vectors 里一条向量都没有 —— "
            "跑一次 python scripts/build_recipe_vectors.py 就会建好（离线、一次性）"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # 共享一个 httpx 客户端：复用连接池，比每次请求新建客户端省掉大量握手开销
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    # 存储基建（第 47 步）：打开 SQLite 库并让食谱 schema 就位。
    # 打开失败不中断启动：open_storage 会降级成内存库并把原因带回（记 warning）。
    #
    # 注意顺序：**必须先开库、再组装注册表**（第 50 步）——
    # 菜谱检索工具要拿到 storage 才能查库，反过来就会出现
    # "工具在、但查不了"的哑状态。
    storage, storage_note = open_storage(settings.pet_db_path or None)
    if storage_note:
        logger.warning("%s", storage_note)
    recipe_library.ensure_schema(storage)
    app.state.storage = storage

    # 菜谱图片是**独立的只读库**（第 58 步）：4.4 MB 的静态资产不混进可变数据。
    # 没有这个库也能正常启动，详情页只是不显示图。
    image_store = open_image_store()
    app.state.image_store = image_store
    if image_store is not None:
        logger.info("菜谱图片库：%s（%s 张）", image_store.path.name, image_store.count())

    # 全量工具是**实例的唯一来源**（连接池共享），但每个功能只拿到自己的那一组
    # （第 85 步）：聊天不答出行/菜谱，所以它一个工具都不给。
    # 第 98 步：一个开关管全部窗口。路由本身长得像 LLM，接口层一行不用改；
    # 选择记在 pet.db 的 app_settings 里，重启后仍然生效。
    # （第 193 步：提前到裁决者之前建 —— 裁决者要用它按 key 选提供方）
    llm = ModelRouter(
        settings, client=http_client, storage=storage
    )
    embedder = build_embedder(client=http_client)
    judge = build_judge(embedder, llm)
    _warn_if_no_vectors(storage, embedder, judge)

    # 外部 MCP 服务器（第 217 步）：配置 → 缓存 → 工具，**全程不联网**。
    # 联网发现工具那一步在 `scripts/mcp_remote_probe.py` 里，启动只读它的产物。
    # 读配置写错时宁可让启动失败（`McpConfigError` 带第几项哪个字段），也不静默少工具。
    remote_specs = servers_from_settings(settings)
    remote_tools: list[DiscoveredTool] = []
    if remote_specs:
        remote_tools, remote_notes = build_remote_tools(
            remote_specs,
            load_cache(
                cache_path(
                    settings.mcp_tools_cache, root=Path(__file__).resolve().parent
                )
            ),
        )
        for note in remote_notes:
            logger.warning("%s", note)
        remote_tools, almanac_notes = swap_almanac_tools(
            remote_tools, remote_specs, settings.mcp_almanac_server
        )
        for note in almanac_notes:
            logger.warning("%s", note)
        # 第 222 步：搜索也换压缩版（实测 bing_search 一次 3192 字，抓正文多半 403）
        remote_tools, search_notes = swap_web_search_tools(
            remote_tools, remote_specs, settings.mcp_web_search_server
        )
        for note in search_notes:
            logger.warning("%s", note)
        logger.info(
            "外部 MCP 服务器：%s 台配置，%d 个工具可用（%s）",
            len(remote_specs),
            len(remote_tools),
            "、".join(tool.name for tool in remote_tools) or "无",
        )

    # 路线工具先建出来：注册表与填表流水线共用同一个实例（路线缓存只有一份）
    route_tool = RouteTool(client=http_client)
    all_tools = build_default_registry(
        http_client=http_client,
        storage=storage,
        embedder=embedder,
        judge=judge,
        train_url=settings.mcp_12306_url,
        train_timeout=settings.mcp_12306_timeout_seconds,
        train_token=settings.mcp_12306_token,
        remote_tools=remote_tools,
        route_tool=route_tool,
    )
    chat_registry = build_feature_registry(all_tools, CHAT_TOOLS, feature="聊天")
    # 车次查询依赖外部 MCP 服务器（第 215 步）：配了地址它才存在，
    # 所以走 optional —— 没配时出行窗口照旧是"地理编码 + 天气 + 周边"三件套。
    # 第 217 步：其余服务器发现的工具同样只在出行窗口可见（聊天窗口一个工具都不给，
    # 是用户第 85 步定下的规矩；想让聊天也能搜网，改的是 `CHAT_TOOLS` 那一行）。
    travel_optional = TRAVEL_OPTIONAL_TOOLS + tuple(
        tool.name for tool in remote_tools
    )
    travel_registry = build_feature_registry(
        all_tools,
        TRAVEL_TOOLS + travel_optional,
        feature="出行",
        optional=travel_optional,
    )
    recipe_registry = build_feature_registry(all_tools, RECIPE_TOOLS, feature="食谱")
    # 提示词里的工具清单跟着**聊天自己的**工具面走，不跟全量走
    # 第 143 步：称呼来自配置（PET_ADDRESS，默认「哥哥」）
    system_prompt = build_system_prompt(
        chat_registry.names(), address=settings.pet_address
    )

    def agent_factory() -> Agent:
        return Agent(
            llm=llm,
            registry=chat_registry,
            max_steps=settings.agent_max_steps,
            system_prompt=system_prompt,
            turn_token_budget=settings.turn_token_budget,
        )

    app.state.settings = settings
    # `registry` 保留给聊天（历史命名），另外两套按功能单独挂
    app.state.registry = chat_registry
    app.state.travel_registry = travel_registry
    app.state.recipe_registry = recipe_registry
    # 第 91 步：聊天与食谱的对话都落进 pet.db（scope 区分），重启可恢复
    app.state.session_store = SessionStore(
        max_messages=settings.session_max_messages, storage=storage
    )
    app.state.agent_factory = agent_factory
    app.state.http_client = http_client
    # 邮箱登入态只放进程内存（第 33 步）：重启即失效，凭据不落盘、不进日志
    app.state.mail_store = MailSessionStore()
    # 食谱窗口直接用 LLM 出结构化菜单，不经过 Agent 的工具循环（第 37 步），
    # 所以把客户端本身也挂上去；画像与计划同样只放进程内存。
    app.state.llm = llm
    app.state.model_router = llm
    app.state.recipe_store = RecipeSessionStore(storage=storage)
    # 签到记录（第 40 步）落进同一个 pet.db（v8 迁移建表）：
    # 签过的日期与补签卡都跟着库走，重启后还在。
    # 库文件打不开时会降级成内存库（open_storage 的语义），
    # 那种情况下签到跟着退回内存模式——界面照常，只是重启即清空。
    app.state.checkin_store = CheckinArchive(storage=storage)
    # 出行流水线（第 41 步）。驾车路线工具**刻意不注册进注册表**：
    # 注册进去会让聊天里的模型也看到它，而聊天场景没有"两个景点之间"这种输入，
    # 模型拿到只会误用。这样聊天链路的工具面一字不变。
    app.state.route_tool = route_tool
    # 行程**只放进程内存**（重启即清空，`core/trip_store.py` 的既定设计），
    # 最多留 50 份；trip_id 与 session_id 无关，一次会话可以产出多份行程。
    # 注：以前的注释写"第 44 步再落 SQLite"，容易被读成"落库是待办"——其实不是。
    app.state.trip_store = TripStore()

    logger.info(
        "工具已注册：聊天[%s] 出行[%s] 食谱[%s]",
        ", ".join(chat_registry.names()) or "无",
        ", ".join(travel_registry.names()) or "无",
        ", ".join(recipe_registry.names()) or "无",
    )
    if not settings.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，/api/chat 会返回错误提示")
    if not settings.hefeng_api_host:
        logger.warning("未配置 HEFENG_API_HOST，天气工具会返回错误提示")
    # 第 51 步起食谱不再依赖 Tavily：菜谱来自本地库（core/seed_recipes.json），
    # 所以这里不再检查 TAVILY_API_KEY。

    logger.info(
        "数据库：%s（FTS5 %s）",
        display_path(storage.path),
        "可用" if storage.fts_available else "不可用，检索回退 LIKE",
    )

    try:
        yield
    finally:
        storage.close()
        logger.info("已关闭数据库")
        await http_client.aclose()
        logger.info("已关闭 HTTP 客户端")


app = FastAPI(title="桌宠 Agent", version="0.1.0", lifespan=lifespan)

# 第 251 步：最小可交付性。默认（AUTH_TOKENS 为空）完全放行，行为与以前一致；
# 配了 token 才要求 Bearer，并按 owner 做会话归属、限流与 Origin 校验。
from api.auth import AuthGate  # noqa: E402

app.add_middleware(AuthGate)

# 第 135 步：给"慢、花钱、有副作用"的那个接口加幂等键。
# 只挂 /api/trip —— 其它 POST 要么本来就幂等（建菜谱按名字去重）、
# 要么是流式（重放是另一回事），要么纯函数式。理由写在 api/idempotency.py 顶部。
# 不带 `Idempotency-Key` 头的调用完全不受影响。
from api.idempotency import IdempotencyMiddleware  # noqa: E402

app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})


@app.middleware("http")
async def request_context(request: Request, call_next):
    """给每个请求一条 request_id，并把"这一次"记进诊断缓冲（第 119 步）。

    三件事：

    1. **request_id**：响应头 `X-Request-ID` 带出去，日志/工具 span/账本也都带着它 ——
       这样"这次为什么慢/为什么失败"可以从一个 id 追到底；
    2. **耗时与状态**记进 `core/observability`（只记 `/api/`，静态文件不占缓冲）；
    3. 超过 3 秒的请求打一条 WARNING —— 排障时第一眼要看的就这几行。

    一个诚实的边界：对**流式响应**，这里测到的是"多久开始返回"，不是"多久说完" ——
    `call_next` 在响应头就绪时就返回了。真正的总时长在账本里（每次模型调用的 latency）。
    """
    from uuid import uuid4

    from core import log_context, observability

    request_id = (request.headers.get("X-Request-ID") or uuid4().hex[:12])[:64]
    log_context.set_request_id(request_id)
    started = time.monotonic()

    try:
        response = await call_next(request)
    except Exception:
        ms = observability.monotonic_ms(started)
        if request.url.path.startswith("/api/"):
            observability.record_request(
                request_id=request_id, method=request.method,
                path=request.url.path, status=500, ms=ms,
            )
        logger.exception("请求 %s %s 抛异常（request=%s）", request.method, request.url.path, request_id)
        raise

    ms = observability.monotonic_ms(started)
    response.headers["X-Request-ID"] = request_id
    if request.url.path.startswith("/api/"):
        # `feature` 用**路径**判定，不用 contextvar：路由跑在子任务里，
        # 它 set 的上下文传不回中间件（Starlette 1.6 实测）。
        # `session` / `turn` 这里也是空的 —— 它们由工具 span 与账本记录，
        # 排障时用 request_id 把三边 join 起来（这是刻意的分工，不是缺字段）。
        feature = observability.feature_for_path(request.url.path)
        session = ""
        observability.record_request(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=ms,
            feature=feature,
            session=session,
            turn=log_context.turn_id(),
        )
        if ms > 3000:
            logger.warning(
                "慢请求 %s %s 用了 %sms（request=%s）",
                request.method, request.url.path, ms, request_id,
            )
    return response


STATIC_ASSETS = ("style.css", "app.js")


def _asset_version() -> str:
    """前端资源的版本号 = 两个文件的最后修改时间。

    改了文件版本就变，浏览器会把 style.css / app.js 当新地址重新下载，
    不需要用户记着按 Ctrl+F5。
    """
    latest = 0.0
    for name in STATIC_ASSETS:
        path = STATIC_DIR / name
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    return str(int(latest))


@app.middleware("http")
async def no_store_for_frontend_files(request: Request, call_next):
    """前端文件不缓存，并给资源地址带上版本号。

    为什么必须有这条（第 26/27 步踩到的真实事故）：
    浏览器的强缓存会留下**旧的 app.js**（或旧的 index.html），于是
    "新脚本 + 旧 HTML" = 脚本去找不存在的元素、取到 null；
    而绑定走的是"缺元素就跳过"的安全助手（第 19 步的加固），
    **全程没有报错**，只是功能静默失效：
    例如「出行」表单的 submit 没被接管，点「确定」直接触发浏览器原生提交 → 整页刷新。

    两道保险一起上：
    1. 响应头 no-store —— 不让浏览器留副本；
    2. 给 index.html 里的 css/js 链接加 `?v=<修改时间>` ——
       即使副本已经在缓存里，地址变了也会重新下载。
    只改首页的 HTML；/api/* 不在此列，方便将来做接口缓存。
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        # 图片搜索例外：Unsplash 的条款明确要求"缓存结果"以节省配额，
        # 而且它的 Demo 应用每小时只有 50 次搜索额度（第 29 步）。
        if request.url.path == "/api/photo":
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"

    if request.url.path == "/":
        body = b"".join([chunk async for chunk in response.body_iterator])
        for name in STATIC_ASSETS:
            body = body.replace(
                f'"{name}"'.encode(), f'"{name}?v={_asset_version()}"'.encode()
            )
        headers = dict(response.headers)
        headers.pop("content-length", None)      # 长度变了，交回框架自己算
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    return response


DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})


@app.middleware("http")
async def only_local_host(request: Request, call_next):
    """只接受本机回环的 Host 头（第 192 步 / 评价15 T0-1）。

    这个应用**没有鉴权**（第 157 步的决定：本机自用，加 token 反而是给自己添门）。
    在那之后，"别人能不能读你的邮件、花你的额度"就靠三道墙：

    1. uvicorn 只绑 `127.0.0.1`（`scripts/start.ps1` 里写死的）—— 局域网摸不到；
    2. 所有变异端点都要 JSON 体 —— 跨站简单请求（form/plain）表达不出来，
       于是浏览器必须先发预检，而本服务不给 CORS 头，预检就被拒了；
       这一条以前是"碰巧成立"，现在由 `tests/test_api_hygiene.py` 守着；
    3. **这一条**：Host 头必须是本机名字。

    前两条都挡不住 DNS rebinding：恶意网页把 `evil.example` 解析到 127.0.0.1 之后，
    浏览器认为"我在跟自己的域名说话"，于是不发预检、不看 CORS、也不觉得跨源，
    请求带着 `Host: evil.example` 长驱直入。此时唯一还能分辨"这是谁在叫我"的就是 Host。

    放行 `testserver` 是给 FastAPI 的 TestClient 用（它不是可解析域名）；
    能自己伪造 Host 的客户端本来就能直连端口，所以这不是新开的口子。
    """
    host = (request.headers.get("host") or "").strip().lower()
    # 剥端口：`127.0.0.1:8000` → `127.0.0.1`；`[::1]:8000` → `[::1]`
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.split(":")[0]

    extra = {h.strip().lower() for h in get_settings().allowed_hosts.split(",") if h.strip()}
    if name not in DEFAULT_ALLOWED_HOSTS and name not in extra:
        logger.warning(
            "拒绝 Host=%s 的请求（%s %s）—— 只接受本机回环，疑似 DNS rebinding",
            host, request.method, request.url.path,
        )
        return JSONResponse(
            {"detail": f"只接受本机访问：Host「{host}」不在白名单里。"},
            status_code=403,
        )
    return await call_next(request)


app.include_router(api_router)
# 第 172 步：桌宠动作目录（扫 static/actions/*.json）
app.include_router(pet_router)
# 第 99 步：模型开关
app.include_router(model_router)
# 第 119 步：本地诊断（最近请求 / 工具 span / 今日用量）
app.include_router(diagnostics_router)
# 第 111 步：用量账本（只读）
app.include_router(usage_router)
app.include_router(mail_router)
app.include_router(recipe_router)
app.include_router(recipe_library_router)
# 第 60 步：上传图片识菜（独立功能，不接主链路）
app.include_router(recipe_photo_router)
app.include_router(checkin_router)
app.include_router(trip_router)
app.include_router(trip_chat_router)
# 第 224 步：出行窗口里的地图（JS API 的配置与 jscode 代理）
app.include_router(map_router)


# 挂在根路径而不是 /static：index.html 里用的是相对路径（style.css / app.js），
# 若挂到 /static 这些相对引用会解析成 /style.css 而 404，页面会没有样式、脚本也加载不了。
#
# html=True 让 StaticFiles 自己把 "/" 映射到 index.html，这样首页也走中间件，
# 能拿到带版本号的资源链接（见 no_store_for_frontend_files）。
# 挂在最后，上面的 /api/* 已经先匹配，不会被抢走。
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
