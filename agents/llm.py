"""LLM 抽象。

Agent 只依赖这个 Protocol，因此在测试里可以塞一个 FakeLLM，
完全不联网就能验证"模型要调工具 → 执行 → 回灌 → 再问模型"的完整链路。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.schemas import Message


class LLMError(RuntimeError):
    """LLM 调用失败，已整理成可展示给用户的文案。

    ``retryable`` 是**机器可读**的标记：网络中断 / 超时这类"再试一次可能就好了"的
    失败为 True。评价11 P3-4：以前上层靠 `"网络错误" in str(exc)` 判断要不要重试 ——
    把错误分类绑在人类文案上，上游网关的错误回显碰巧含"超时"就会被误重试，
    真实网络错误文案不合也会漏重试。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class StreamEvent:
    """流式调用的一块产物（第 73 步）。

    - ``kind="delta"``：模型刚吐出来的一小段文本，放在 ``text`` 里；
    - ``kind="done"``：这一轮结束，``message`` 与 ``complete()`` 返回值**同型**
      （可能带 tool_calls）。

    两类事件顺序是有意义的：delta 可以有很多条，done 只有一条且必然在最后。
    """

    kind: str
    text: str = ""
    message: Message | None = None


@runtime_checkable
class StreamingLanguageModel(Protocol):
    """能在生成过程中吐字模型。

    单独一个 Protocol 而不是把 `LanguageModel` 改成生成器：调用方分两类 ——
    "一次拿到结果"的（后台任务、绝大多数测试）用 `complete()` 最简单；
    "边到边显示"的才需要流。`Agent` 用 `hasattr` 探测，没有就自动退回 `complete()`，
    所以所有既有的假 LLM **不需要改**。
    """

    def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ...


@runtime_checkable
class LanguageModel(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        """输入消息历史，返回一条 assistant 消息（可能携带 tool_calls）。"""
        ...
