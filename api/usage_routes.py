"""用量账本的只读查询接口：按功能与模型汇总、最近若干次调用。

这里不做预算判断——那要发生在调用之前，混在一处会让查询带上副作用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class UsageRow(BaseModel):
    feature: str
    provider: str
    model: str
    calls: int
    prompt: int
    completion: int
    total: int
    missing: int


class UsageTotal(BaseModel):
    calls: int = 0
    prompt: int = 0
    completion: int = 0
    total: int = 0
    missing: int = 0


class UsageSummaryOut(BaseModel):
    since: str
    days: int
    features: list[UsageRow] = []
    total: UsageTotal = UsageTotal()


class UsageCallOut(BaseModel):
    created_at: str
    turn_id: str
    feature: str
    provider: str
    model: str
    step: int
    stream: bool
    prompt: int | None = None
    completion: int | None = None
    total: int | None = None
    usage_source: str
    finish_reason: str | None = None
    latency_ms: int | None = None


class UsageRecentOut(BaseModel):
    calls: list[UsageCallOut] = []


def _storage(request: Request) -> Any:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="这个应用没有装配数据库")
    return storage


@router.get("/usage/summary", response_model=UsageSummaryOut)
async def usage_summary(
    request: Request, days: int = 1, feature: str = ""
) -> UsageSummaryOut:
    """最近 `days` 天（含今天）的汇总。`feature` 留空表示全部。"""
    from core import usage

    capped_days = max(1, min(int(days), 90))
    # 与账本、CLI 用同一个"本地零点"（第 114 步）
    since = usage.day_start_utc(capped_days - 1)
    data = usage.summary(
        _storage(request),
        since=since,          # day_start_utc 已经返回可直接比较的 UTC ISO 字符串
        feature=feature or None,
    )
    return UsageSummaryOut(
        since=data["since"],
        days=capped_days,
        features=[UsageRow(**row) for row in data["features"]],
        total=UsageTotal(**data["total"]),
    )


@router.get("/usage/recent", response_model=UsageRecentOut)
async def usage_recent(request: Request, limit: int = 20) -> UsageRecentOut:
    from core import usage

    capped = max(1, min(int(limit), 200))
    return UsageRecentOut(
        calls=[UsageCallOut(**row) for row in usage.recent(_storage(request), capped)]
    )
