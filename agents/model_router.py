"""把模型请求转发给当前选中的提供方。

切换提供方不重建客户端；不可用的提供方切不过去，切换时直接报错。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from agents.deepseek_llm import DeepSeekLLM
from agents.llm import LLMError, StreamEvent
from agents.providers import (
    DEFAULT_PROVIDER,
    Provider,
    find_provider,
    providers_from_settings,
)
from core import app_settings
from core.schemas import Message

logger = logging.getLogger(__name__)


class ModelRouter:
    """在多个 OpenAI 兼容提供方之间切换。"""

    def __init__(
        self,
        settings: Any,
        *,
        client: Any = None,
        storage: Any = None,
        active: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._storage = storage
        self._providers = providers_from_settings(settings)
        self._cache: dict[str, DeepSeekLLM] = {}
        self._active = self._initial_active(active)

    # ------------------------------------------------------------ 选择状态

    def _initial_active(self, explicit: str | None) -> str:
        """优先级：显式参数 > 库里的记忆 > 默认（DeepSeek）。

        库里的记忆可能是**已经不可用**的那一家（比如 key 被删了）——
        这时候回落到默认，而不是让整个应用起不来。
        """
        remembered = explicit or (
            app_settings.get_setting(self._storage, app_settings.KEY_ACTIVE_PROVIDER, "")
            if self._storage is not None
            else ""
        )
        for candidate in (remembered, DEFAULT_PROVIDER):
            provider = find_provider(self._providers, candidate) if candidate else None
            if provider is not None and provider.available:
                if remembered and candidate != remembered:
                    logger.warning(
                        "上次选的是 %s，但它现在不可用，回落到 %s", remembered, candidate
                    )
                return provider.name
        # 一家都不可用也别崩：挑第一个，失败时由调用方给出人话错误
        logger.warning("没有任何可用的模型提供方，请在 .env 里配置 API key")
        return self._providers[0].name

    @property
    def active(self) -> str:
        return self._active

    @property
    def providers(self) -> tuple[Provider, ...]:
        return self._providers

    def snapshot(self) -> dict[str, object]:
        """给界面的视图（**不含任何 key**）。"""
        return {
            "active": self._active,
            "providers": [provider.public() for provider in self._providers],
        }

    def switch(self, name: str) -> Provider:
        """切换当前提供方并记住它。不可用时抛 ValueError（带原因）。"""
        provider = find_provider(self._providers, name)
        if provider is None:
            raise ValueError(f"没有名为 {name!r} 的模型提供方")
        if not provider.available:
            raise ValueError(f"{provider.label} 现在用不了：{provider.reason}")
        if provider.name != self._active:
            logger.info("切换模型提供方：%s → %s", self._active, provider.name)
        self._active = provider.name
        if self._storage is not None:
            app_settings.set_setting(
                self._storage, app_settings.KEY_ACTIVE_PROVIDER, provider.name
            )
        return provider

    def current(self) -> Provider:
        provider = find_provider(self._providers, self._active)
        assert provider is not None, "内部错误：active 指向了不存在的提供方"
        return provider

    # ------------------------------------------------------------ 转发

    def llm_for(self, name: str) -> DeepSeekLLM:
        """取某一家对应的客户端（按名字缓存，切回来不重建）。"""
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        provider = find_provider(self._providers, name)
        if provider is None:
            raise LLMError(f"没有名为 {name!r} 的模型提供方")
        llm = DeepSeekLLM(
            client=self._client,
            settings=self._settings,
            provider=provider,
            storage=self._storage,          # 第 111 步：账本
        )
        self._cache[name] = llm
        return llm

    def _active_llm(self) -> DeepSeekLLM:
        return self.llm_for(self._active)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        return await self._active_llm().complete(messages, tools)

    def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._active_llm().complete_stream(messages, tools)

    async def probe(self, name: str | None = None) -> str:
        """给某个提供方发一句最小的请求，返回它的一句回话（第 99 步的"测一下"）。

        用户**主动点**才会调它 —— 不做自动探测，那会在每次启动时花钱。
        """
        provider = find_provider(self._providers, name or self._active)
        if provider is None:
            raise LLMError(f"没有名为 {name!r} 的模型提供方")
        if not provider.available:
            raise LLMError(f"{provider.label} 现在用不了：{provider.reason}")
        llm = self.llm_for(provider.name)
        reply = await llm.complete([Message(role="user", content="回复两个字：可用")])
        return str(reply.content or "").strip()

    async def aclose(self) -> None:
        for llm in self._cache.values():
            await llm.aclose()
        self._cache.clear()
