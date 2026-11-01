"""本地只读诊断接口：把最近的错误、工具失败与库状态整理成一份 JSON，供排查问题用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/diagnostics")
async def diagnostics(request: Request, limit: int = 20) -> dict[str, Any]:
    from core import log_context, observability, usage

    capped = max(1, min(int(limit), 200))
    storage = getattr(request.app.state, "storage", None)

    usage_today: dict[str, Any] = {"total": {}, "features": []}
    if storage is not None:
        data = usage.summary(storage, since=usage.day_start_utc(0))
        usage_today = {"total": data["total"], "features": data["features"]}

    return {
        "request_id": log_context.request_id(),
        "requests": observability.recent_requests(capped),
        "tools": observability.recent_tools(capped),
        "slow_tools": observability.slow_tools(),
        "failed_tools": observability.failed_tools(),
        "usage_today": usage_today,
    }
