"""邮箱接口层：参数校验与响应组装，IMAP 逻辑在 `core/mailbox.py`。

邮箱操作是阻塞 IO，一律 `asyncio.to_thread`；授权码只在登录请求体里出现一次，不落盘、不写日志。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.mailbox import (
    MailError,
    MailboxClient,
    provider_by_key,
    provider_for_email,
    providers_public,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail", tags=["mail"])


# --------------------------------------------------------------- 请求/响应


class ProviderOut(BaseModel):
    key: str
    label: str
    host: str
    port: int
    help_text: str = ""
    domains: list[str] = []


class ProvidersResponse(BaseModel):
    providers: list[ProviderOut]
    #: 域名认不出来时前端拿它显示提示
    default: str


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


class LoginRequest(SessionRequest):
    address: str = Field(min_length=3, max_length=254)
    #: 授权码。**不回传、不进日志**；上限给得宽松些，各家长度不一。
    secret: str = Field(min_length=1, max_length=256)
    provider: str = Field(default="", max_length=32)


class AccountOut(BaseModel):
    address: str
    provider: str = ""
    provider_label: str = ""
    host: str = ""
    logged_in: bool = True


class StatusResponse(BaseModel):
    logged_in: bool
    account: AccountOut | None = None


class FolderOut(BaseModel):
    key: str
    label: str
    mailbox: str | None = None
    unread: int = 0
    total: int = 0


class FoldersResponse(BaseModel):
    folders: list[FolderOut]


class MessageOut(BaseModel):
    uid: str
    #: 真实邮箱名（星标要用它定位），不是逻辑 key
    mailbox: str
    folder: str
    from_name: str
    from_address: str = ""
    subject: str
    snippet: str = ""
    #: ISO 8601 带时区；前端按浏览器本地时间分「今天 / 昨天 / 星期X」
    received: str | None = None
    unread: bool = False
    flagged: bool = False


class MessagesResponse(BaseModel):
    folder: str
    messages: list[MessageOut]
    #: 这个文件夹里一共能列出多少封（用于"已经到底了"与进度感知）
    total: int = 0
    #: 前面还有更早的没给 → 前端可以继续往下翻
    has_more: bool = False


class CheckResponse(BaseModel):
    folder: str
    #: 比 since_uid 更新的邮件（虚拟文件夹例外，见 reset）
    messages: list[MessageOut]
    total: int = 0
    unread: int = 0
    #: True = 前端应**整份替换**第一页，而不是插到列表前面
    reset: bool = False


class FlagRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    mailbox: str = Field(min_length=1, max_length=512)
    uid: str = Field(min_length=1, max_length=32)
    flagged: bool


class FlagResponse(BaseModel):
    uid: str
    flagged: bool


class LogoutResponse(BaseModel):
    status: str
    session_id: str


# --------------------------------------------------------------- 依赖


def _store(request: Request):
    store = getattr(request.app.state, "mail_store", None)
    if store is None:
        # 正常情况下 main.py 的 lifespan 一定建好了；这里防的是"直接构造 app"的测试
        raise HTTPException(status_code=503, detail="邮箱功能未初始化")
    return store


def _require_client(request: Request, session_id: str) -> MailboxClient:
    client = _store(request).get(session_id)
    if client is None:
        # 401 而不是 404：语义是"没登入"，前端据此退回登入页
        raise HTTPException(status_code=401, detail="尚未登入邮箱，请先添加邮箱")
    return client


async def _call(func, *args, **kwargs):
    """把一次阻塞的邮箱操作丢进线程，并把 MailError 翻成 400。"""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except MailError as exc:
        logger.info("邮箱操作失败：%s", exc.message)
        raise HTTPException(status_code=400, detail=exc.message) from exc


def _account_out(client: MailboxClient) -> AccountOut:
    return AccountOut(
        address=client.address,
        provider=client.provider.key,
        provider_label=client.provider.label,
        host=client.host,
    )


# --------------------------------------------------------------- 接口


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """服务商清单。前端不硬编码任何服务商名或服务器地址。

    本轮只有 QQ 邮箱（用户要求"先暂时只用 qq 邮箱登入"）；
    以后加一家只需要动 core/mailbox.py 的 PROVIDERS，前端会自动多一个选项。
    """
    return ProvidersResponse(
        providers=[ProviderOut(**row) for row in providers_public()],
        default="qq",
    )


@router.post("/login", response_model=AccountOut)
async def login(payload: LoginRequest, request: Request) -> AccountOut:
    """用"邮箱账号 + 授权码"连一次 IMAP，成功后把客户端留在内存里。"""
    address = payload.address.strip()
    secret = payload.secret.strip()
    if not address or "@" not in address:
        raise HTTPException(status_code=400, detail="请填写完整的邮箱地址，例如 12345@qq.com")
    if not secret:
        raise HTTPException(status_code=400, detail="请填写授权码")

    # 服务商一律**按邮箱域名判定**。让前端选的覆盖域名，就会出现
    # "拿 qq 的服务器去登 gmail" 这种必然失败、报错还看不懂的请求。
    provider = provider_for_email(address)
    if provider is None:
        supported = "、".join(row["label"] for row in providers_public())
        raise HTTPException(
            status_code=400,
            detail=f"这个邮箱暂时还不支持，目前只能登入：{supported}。",
        )

    # 前端选的那个只做一致性校验：UI 与地址对不上时早点说出来
    chosen = provider_by_key(payload.provider)
    if chosen is not None and chosen.key != provider.key:
        raise HTTPException(
            status_code=400,
            detail=f"邮箱地址与所选服务商对不上：{address} 属于 {provider.label}。",
        )

    client = await _call(
        _store(request).login, payload.session_id, address, secret, provider
    )
    logger.info(
        "会话 %s 已登入邮箱 %s（%s）", payload.session_id, address, provider.label
    )
    return _account_out(client)


@router.post("/logout", response_model=LogoutResponse)
async def logout(payload: SessionRequest, request: Request) -> LogoutResponse:
    # 未登入时退出不当作错误：用户想回到登入页，结果一样
    await asyncio.to_thread(_store(request).logout, payload.session_id)
    return LogoutResponse(status="logged_out", session_id=payload.session_id)


@router.get("/status", response_model=StatusResponse)
async def status(session_id: str, request: Request) -> StatusResponse:
    """前端打开窗口时问一句：这个会话还登着吗？

    服务重启后内存清空，这里会回 `logged_in=false`，
    前端于是退回「添加邮箱」页——**而不是留一个点不动的收件箱**。
    """
    client = _store(request).get(session_id)
    if client is None:
        return StatusResponse(logged_in=False)
    return StatusResponse(logged_in=True, account=_account_out(client))


@router.get("/folders", response_model=FoldersResponse)
async def folders(session_id: str, request: Request) -> FoldersResponse:
    client = _require_client(request, session_id)
    rows = await _call(client.list_folders)
    return FoldersResponse(folders=[FolderOut(**row) for row in rows])


@router.get("/messages", response_model=MessagesResponse)
async def messages(
    session_id: str,
    request: Request,
    folder: str = Query(default="inbox", max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> MessagesResponse:
    """取一页邮件。

    `offset` 是"已经给过多少封"——**没有它，超过 limit 的历史邮件就永远取不到**。
    前端滚到底时带着新的 offset 再问一次，于是整个邮箱都能翻到。
    """
    client = _require_client(request, session_id)
    page = await _call(client.list_messages, folder, limit, offset)
    return MessagesResponse(
        folder=folder,
        messages=[MessageOut(**row) for row in page["messages"]],
        total=int(page.get("total") or 0),
        has_more=bool(page.get("has_more")),
    )


@router.get("/check", response_model=CheckResponse)
async def check_new(
    session_id: str,
    request: Request,
    folder: str = Query(default="inbox", max_length=32),
    since_uid: int = Query(default=0, ge=0, le=1_000_000_000),
) -> CheckResponse:
    """"有没有新邮件"的便宜探针（第 36 步）。

    切走再切回来时前端先拿缓存渲染，然后调这里。**没有新邮件的话后端只发一次
    STATUS**（不 SELECT、不 FETCH），所以来回点文件夹不会反复下载邮件。
    `since_uid` 是前端手上最大的那个 UID；`reset=true` 表示整份替换。
    """
    client = _require_client(request, session_id)
    result = await _call(client.check_new, folder, since_uid)
    return CheckResponse(
        folder=folder,
        messages=[MessageOut(**row) for row in result["messages"]],
        total=int(result.get("total") or 0),
        unread=int(result.get("unread") or 0),
        reset=bool(result.get("reset")),
    )


@router.post("/flag", response_model=FlagResponse)
async def set_flag(payload: FlagRequest, request: Request) -> FlagResponse:
    """点星标 → 改 IMAP 的 \\Flagged。

    图 1 每行右侧有星标、左侧有「星标邮件」文件夹；星标要是只能看不能点，
    那个文件夹永远不会新增内容。（这条是超出原文的推断，README 里记了。）
    """
    client = _require_client(request, payload.session_id)
    flagged = await _call(client.set_flagged, payload.mailbox, payload.uid, payload.flagged)
    return FlagResponse(uid=payload.uid, flagged=flagged)
