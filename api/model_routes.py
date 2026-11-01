"""模型切换接口：列出提供方、切换、以及用户主动触发的连通性探测。

切换只校验配置齐全；探测才真发一次请求，且只由用户点击触发，不做自动探测。
"""

from __future__ import annotations

from typing import Any

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from core import usage as usage_store
from core.log_context import set_context as set_log_context
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api")


class ProviderOut(BaseModel):
    name: str
    label: str
    model: str
    note: str
    available: bool
    reason: str


class ModelStateOut(BaseModel):
    active: str
    providers: list[ProviderOut]


class SwitchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class ProbeOut(BaseModel):
    ok: bool
    provider: str
    message: str


def _model_router(request: Request) -> Any:
    """取模型路由；没有就 503。

    测试里有些 app 只挂了 `app.state.llm`（假 LLM），没有路由 ——
    那种情况给一个明确的状态码，而不是 500 或空对象。
    """
    picked = getattr(request.app.state, "model_router", None)
    if picked is None:
        raise HTTPException(status_code=503, detail="这个应用没有装配模型路由")
    return picked


@router.get("/model", response_model=ModelStateOut)
async def read_model(request: Request) -> ModelStateOut:
    return ModelStateOut(**_model_router(request).snapshot())


@router.post("/model", response_model=ModelStateOut)
async def switch_model(payload: SwitchRequest, request: Request) -> ModelStateOut:
    picked = _model_router(request)
    try:
        picked.switch(payload.name)
    except ValueError as exc:
        # 400 而不是 500：这是"你选了一个用不了的东西"，带上原因让界面直接显示
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelStateOut(**picked.snapshot())


@router.post("/model/probe", response_model=ProbeOut)
async def probe_model(request: Request) -> ProbeOut:
    """给当前（或指定）提供方发一句最小请求。

    失败也返回 200 + `ok: false`：这是"探测结果"，不是"请求失败"——
    界面拿它显示 ✅ / ❌，不该走错误分支。
    """
    picked = _model_router(request)
    provider = picked.current()
    # 第 114 步：探活要**单独归因**（评价6 第 1 条）。
    # 不设的话账本里是 feature="?"、session 与 turn 都空 ——
    # 「今日」会多出一行 `?`，而"测一下"的成本也不该混进真实对话。
    set_log_context("probe", "", uuid4().hex[:12])
    usage_store.reset_turn_tokens()
    try:
        reply = await picked.probe()
    except Exception as exc:              # noqa: BLE001 —— 探测就是要吞掉一切异常
        return ProbeOut(ok=False, provider=provider.name, message=str(exc)[:200])
    return ProbeOut(
        ok=True,
        provider=provider.name,
        message=f"{provider.label}可用（回话：{reply[:20]}）",
    )
