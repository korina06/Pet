"""流式响应的两个小工具：判断客户端是否还在，以及断流时只把用户问的那句话写进历史。
"""

from __future__ import annotations

from typing import Any

from core.schemas import Message


async def client_gone(request: Any) -> bool:
    """客户端是否已经断开。

    拿不到状态（不是真的 Request、或底层不支持）时返回 False ——
    **宁可多跑一会儿，也别误杀一个还连着的请求**。
    """
    checker = getattr(request, "is_disconnected", None)
    if checker is None:
        return False
    try:
        return bool(await checker())
    except Exception:            # noqa: BLE001 —— 判活失败不该影响业务
        return False


def remember_question(store: Any, session_id: str, message: str) -> None:
    """断流时只把用户问的那句落历史。

    为什么写：用户在界面上看到了自己的提问，历史里却查不到，会以为丢了。
    为什么不写回答：它没生成完，写进去就是半句废话 ——
    **历史里不该出现"半句回答"**。
    """
    if store is None:
        return
    store.append(session_id, Message(role="user", content=message))
