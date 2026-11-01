"""上传图片识菜的接口：与主检索链路完全隔离。

失败也返回 200 + `status`，只有请求本身不合法（不是图片、太大）才 400。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.recipe_library import list_aversions
from core.recipe_photo import (
    FAILURE_COPY,
    PhotoOutcome,
    PhotoRecognizer,
    decode_image,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recipe", tags=["recipe-photo"])

#: 整个流程的超时（实测：上传 1~2s + 识图 12~15s + 搜索 2s + 模型 2s）。
#: 给到 60 秒，前端那边是 90 秒，保证后端先超时、能给出明确文案。
FLOW_TIMEOUT = 60.0


class PhotoRequest(BaseModel):
    #: 图片的 base64（可以带 `data:image/webp;base64,` 前缀）
    image_base64: str = Field(min_length=1, max_length=12 * 1024 * 1024)
    #: 用户随图片附的一句话（可选），目前只用于日志
    note: str = Field(default="", max_length=200)


class PhotoResponse(BaseModel):
    status: str
    reply: str
    dish: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


def _recognizer(request: Request) -> PhotoRecognizer | Any:
    """优先用测试注入的识别器，否则按配置现搭一个。"""
    injected = getattr(request.app.state, "photo_recognizer", None)
    if injected is not None:
        return injected

    from tools.serpapi_lens import SerpApiClient

    settings = getattr(request.app.state, "settings", None)
    http_client = getattr(request.app.state, "http_client", None)
    llm = getattr(request.app.state, "llm", None)
    storage = getattr(request.app.state, "storage", None)
    if settings is None or http_client is None or llm is None:
        raise HTTPException(status_code=503, detail="识图功能未初始化")

    serp = SerpApiClient(settings.serpapi_api_key, http_client)
    return PhotoRecognizer(
        serp,
        llm,
        aversions_provider=(lambda: list_aversions(storage)) if storage else None,
    )


@router.post("/photo", response_model=PhotoResponse)
async def recognize_photo(payload: PhotoRequest, request: Request) -> PhotoResponse:
    """上传一张菜的照片，返回"这是什么 + 简要做法 + 链接"。"""
    try:
        data, fmt = decode_image(payload.image_base64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 配置错误要**先说清楚**（第 80 步）。
    # 以前这里直接 _recognizer()，而它缺 llm 会抛 503 —— 于是"没配识图 key"
    # 这个最该被明确告知的情况，反而被一句笼统的"未初始化"盖住了。
    # 文案复用 core.recipe_photo.FAILURE_COPY，不在路由里另写一份。
    # 只在**没有注入识别器**时才提前判：注入了就说明能力由调用方提供，
    # key 该不该有是它自己的事（测试用注入跑完整流程，这里判早了会误伤）。
    if getattr(request.app.state, "photo_recognizer", None) is None:
        settings = getattr(request.app.state, "settings", None)
        if settings is not None and not str(settings.serpapi_api_key or "").strip():
            return PhotoResponse(status="no_key", reply=FAILURE_COPY["no_key"])

    recognizer = _recognizer(request)
    try:
        outcome: PhotoOutcome = await asyncio.wait_for(
            recognizer.run(data, fmt), timeout=FLOW_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning("识图整体超时（%.0fs）", FLOW_TIMEOUT)
        outcome = PhotoOutcome(
            status="timeout",
            reply="识图服务超时了，这张图没认出来。稍后再试一次吧。",
        )

    return PhotoResponse(
        status=outcome.status,
        reply=outcome.reply,
        dish=outcome.dish,
        alternatives=outcome.alternatives,
        sources=outcome.sources,
    )
