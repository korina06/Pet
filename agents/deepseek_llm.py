"""DeepSeek（OpenAI 兼容）客户端，实现 `agents.llm.LanguageModel`。

客户端可注入以便离线测试；失败统一收敛成 `LLMError` 且不含 key；工具参数的原始 JSON
字符串必须原样回灌，重新序列化会改变空白与键序。
"""

from __future__ import annotations

import asyncio
import json
import time
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agents.llm import LLMError, StreamEvent
from agents.providers import (
    DEFAULT_PROVIDER,
    Provider,
    find_provider,
    providers_from_settings,
)
from core.config import Settings, get_settings
from core.schemas import Message, ToolCall

logger = logging.getLogger(__name__)

#: LLM 响应普遍较慢，超时给足 60 秒（连接/读写都按这个值）。
TIMEOUT_SECONDS = 60.0

#: 连接类错误的**总尝试次数**。只对"根本没连上 / 连上又被掐断"重试：
#: 桌面端网络会瞬时抖动（实测同一天 00:50 连不上 api.deepseek.com、00:53 又正常）。
#: 4xx/5xx **不重试** —— 那是请求本身的问题，重试只会多花钱、还会掩盖真因。
NETWORK_ATTEMPTS = 3
#: 第 n 次失败后等这么久再试（线性退避，单位秒）
RETRY_BACKOFF_SECONDS = 0.4

#: 这些异常值得重试：连接失败 / 连接超时 / 读写超时 / 连接被对端关掉
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

#: 错误详情与响应预览的截断长度，避免把整页网关 HTML 塞进日志。
MAX_DETAIL_CHARS = 500


def _truncate(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（已截断）"


def _preview(value: Any) -> str:
    """把任意值转成可读的短字符串，用于错误信息。"""
    if isinstance(value, str):
        return _truncate(value)
    try:
        return _truncate(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - 兜底，几乎不会走到
        return _truncate(repr(value))


def _redact(text: str, *secrets: str) -> str:
    """抹掉文案里的敏感串。

    错误信息会同时出现在服务端日志和前端提示里，api key 一旦被
    上游回显（401 的报文经常带上 key）就会泄露，所以统一在这里兜底。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _extract_detail(response: httpx.Response) -> str:
    """尽力从错误响应里挖出可读的 detail。

    DeepSeek 的错误体形如 ``{"error": {"message": "...", "type": "..."}}``，
    但网关/代理可能返回别的形状，所以逐层降级，最后退回截断后的原文。
    """
    try:
        data = response.json()
    except ValueError:
        return _truncate(response.text)

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "msg"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate(value)
        elif isinstance(error, str) and error.strip():
            return _truncate(error)
        for key in ("message", "detail", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate(value)
    return _preview(data)


class DeepSeekLLM:
    """OpenAI 兼容的 Chat Completions 客户端（第 98 步起可指定提供方）。

    默认仍然打 DeepSeek；传入 `provider` 就换成它的 base_url / model / key。
    改名会牵动一堆文档与测试，而它做的确实是同一件事（同一份协议），所以名字保留。
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
        provider: Provider | None = None,
        storage: Any = None,
        temperature: float | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        # 采样温度（第 202 步 / 评价16）：只给**评测用的裁决者**传 0，
        # 让 `--vectors` 的数字可复现。应用路径不传 = 保持服务端默认，行为不变。
        self._temperature = temperature
        # 账本（第 111 步）：不传就只记日志、不落库（测试里多数不需要）
        self._storage = storage
        # 不传 provider 时保持老行为：打 DeepSeek
        self._provider = provider or find_provider(
            providers_from_settings(self._settings), DEFAULT_PROVIDER
        )
        # 注入进来的 client 由调用方负责关闭；自建的才由 aclose() 回收。
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=TIMEOUT_SECONDS)

    # ------------------------------------------------------------------ 基础属性

    @property
    def provider(self) -> Provider | None:
        return self._provider

    @property
    def _max_output_tokens(self) -> int:
        """单次输出上限（第 112 步）。下限 1，避免误配成 0 让接口报错。"""
        return max(1, int(getattr(self._settings, "max_output_tokens", 1000) or 1000))

    @property
    def _model(self) -> str:
        return str(self._provider.model if self._provider else "")

    def _require_key(self) -> str:
        """取当前提供方的 key；没配就抛 ValueError（配置错误，不是调用失败）。

        错误里要带**提供方名字**：切到 GLM 之后报"未配置 DEEPSEEK_API_KEY"
        会让人以为切错了地方。
        """
        label = self._provider.label if self._provider else "模型"
        key = str(self._provider.api_key if self._provider else "").strip()
        if not key:
            raise ValueError(f"未配置 {label} 的 API key，请在 .env 里填好再切换。")
        return key

    @property
    def label(self) -> str:
        """当前提供方的显示名（没传 provider 时就是 DeepSeek）。

        第 128 步：所有**用户可见**的报错都要用它 —— 切到 GLM 之后
        报"调用 DeepSeek 失败"会让人以为切换没生效（用户实测撞到）。
        """
        return self._provider.label if self._provider else "DeepSeek"

    @property
    def env_key_name(self) -> str:
        """该提供方在 .env 里的变量名（`DEEPSEEK_API_KEY` / `GLM_API_KEY`）。"""
        name = self._provider.name if self._provider else "deepseek"
        return f"{str(name).upper()}_API_KEY"

    @property
    def client(self) -> httpx.AsyncClient:
        """当前使用的 httpx 客户端（便于调试与测试）。"""
        return self._client

    @property
    def endpoint(self) -> str:
        """规范化为 ``{base_url}/chat/completions``，容忍 base_url 带结尾斜杠。"""
        base_url = str(self._provider.base_url if self._provider else "").strip().rstrip("/")
        return f"{base_url}/chat/completions"

    async def aclose(self) -> None:
        """仅在客户端由本实例创建时才真正关闭，注入的客户端归调用方管。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DeepSeekLLM:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 主流程

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        """调用 DeepSeek，返回一条 assistant 消息（可能携带 tool_calls）。

        失败一律抛 :class:`LLMError`；未配置 key 时沿用
        ``Settings.require_deepseek_key()`` 抛出的 ValueError（配置错误，
        调用方按配置问题处理，不应被当成"模型调用失败"）。
        """
        api_key = self._require_key()

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.to_api() for message in messages],
            "stream": False,
            # 第 112 步：输出上限。不设的话模型可以一路写下去，
            # 而输出是按 token 计费的大头（食谱实测 59–147，1000 足够宽裕）。
            "max_tokens": self._max_output_tokens,
        }
        # 没有 tools 时绝不能带这个字段：部分网关把空列表理解成
        # "声明了工具但一个都没选"，会直接影响模型是否输出 tool_calls。
        if tools:
            payload["tools"] = tools
        # 只有显式传了才带：默认不传 = 服务端默认温度（与第 202 步之前的请求一模一样）。
        if self._temperature is not None:
            payload["temperature"] = self._temperature

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        try:
            response = await self._post_with_retry(payload, headers)
        except httpx.TimeoutException as exc:  # 必须排在 HTTPError 之前：它是 HTTPError 的子类
            raise LLMError(
                f"调用{self.label}超时（超过 {TIMEOUT_SECONDS:.0f} 秒未返回），"
                f"已尝试 {NETWORK_ATTEMPTS} 次。",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            detail = _redact(str(exc), api_key)
            raise LLMError(
                f"调用{self.label}失败：网络错误（{type(exc).__name__}）：{detail}",
                retryable=True,
            ) from exc

        if not response.is_success:
            raise LLMError(self._http_error_message(response, api_key))

        self._log_usage(
            response, stream=False, latency_ms=int((time.monotonic() - started) * 1000)
        )
        return self._parse_reply(response)

    async def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用；**连接类错误在"还没吐出任何字"之前会重试**（第 127 步）。

        为什么只在没吐字之前重试：一旦已经把字送给用户，重试会导致
        同一句话出现两遍。那种情况宁可报错，让上层决定怎么解释。

        为什么值得加：Agent 的 `run_stream()` 用的就是这条路 ——
        瞬时 `ConnectError` 原来会让整轮对话直接失败（用户实测撞到）。
        """
        yielded = False
        for attempt in range(1, NETWORK_ATTEMPTS + 1):
            try:
                async for event in self._complete_stream_once(messages, tools):
                    yielded = True
                    yield event
                return
            except LLMError as exc:
                text = str(exc)
                # 第 156 步（评价11 P3-4）：用异常自带的机器可读标记，不再匹配中文文案
                if yielded or not exc.retryable or attempt >= NETWORK_ATTEMPTS:
                    raise
                logger.warning(
                    "流式调用网络中断（第 %d/%d 次）：%s，%.1f 秒后重试",
                    attempt,
                    NETWORK_ATTEMPTS,
                    text[:80],
                    RETRY_BACKOFF_SECONDS * attempt,
                )
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

    async def _complete_stream_once(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 DeepSeek：边到边产出 ``delta``，最后产出一条 ``done``。

        为什么要真的流式（第 73 步）：`stream=False` 时用户点完要**干等整段回答**
        生成完，界面上再一次性冒出来 —— 长回答（尤其是食谱那种带理由的）能有十几秒。
        流式之后第一句话几乎立刻出现。

        协议是 OpenAI 兼容的 SSE：每行 `data: {...}`，最后一行 `data: [DONE]`。
        两个坑：
        1. `tool_calls` 是**分片**来的，按 `index` 拼；`arguments` 更是被切成多段
           JSON 片段，必须原样拼接后**整段** `json.loads`（中途解析一定失败）。
        2. 出错时 `stream()` 还没读响应体，得先 `aread()` 才能拿到错误详情。
        """
        api_key = self._require_key()

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.to_api() for message in messages],
            "stream": True,
            "max_tokens": self._max_output_tokens,
            # 第 102 步：让服务端在**最后一块**带上 usage。实测 DeepSeek 与
            # GLM 都支持（各自返回 prompt/completion/total）—— 食谱主路径正是流式，
            # 不补这条，食谱的用量就完全没记录，拿它定预算会系统性低估。
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        content_parts: list[str] = []
        raw_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_seen: dict[str, Any] | None = None
        started = time.monotonic()

        try:
            async with self._client.stream(
                "POST", self.endpoint, json=payload, headers=headers
            ) as response:
                if not response.is_success:
                    # 流式响应还没读体，先把错误体读出来再整理文案
                    await response.aread()
                    raise LLMError(self._http_error_message(response, api_key))

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        # 心跳/空包之类的非 JSON 行：跳过，不中断整条流
                        continue
                    # usage 那块**没有 choices**，必须在下面的 continue 之前处理
                    usage = chunk.get("usage")
                    if isinstance(usage, dict) and usage:
                        usage_seen = usage

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    reason = choices[0].get("finish_reason")
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
                    delta = choices[0].get("delta") or {}

                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        content_parts.append(piece)
                        yield StreamEvent(kind="delta", text=piece)

                    for raw in delta.get("tool_calls") or []:
                        if not isinstance(raw, dict):
                            continue
                        try:
                            index = int(raw.get("index") or 0)
                        except (TypeError, ValueError):
                            index = 0
                        slot = raw_calls.setdefault(
                            index,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        if raw.get("id"):
                            slot["id"] = str(raw["id"])
                        function = raw.get("function") or {}
                        name_piece = function.get("name")
                        if isinstance(name_piece, str) and name_piece:
                            slot["function"]["name"] += name_piece
                        args_piece = function.get("arguments")
                        if isinstance(args_piece, str) and args_piece:
                            slot["function"]["arguments"] += args_piece
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"调用{self.label}超时（超过 {TIMEOUT_SECONDS:.0f} 秒未返回），"
                f"已尝试 {NETWORK_ATTEMPTS} 次。",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            detail = _redact(str(exc), api_key)
            raise LLMError(
                f"调用{self.label}失败：网络错误（{type(exc).__name__}）：{detail}",
                retryable=True,
            ) from exc
        except (asyncio.CancelledError, GeneratorExit):
            # 第 116 步：客户端断开 / 上层取消。**输入的 token 已经花掉了**，
            # 但我们拿不到 usage —— 记一行 missing + finish_reason=cancelled，
            # 否则"取消"在账本里等于没发生过（账本会系统性少算）。
            self._log_usage_dict(
                None,
                stream=True,
                finish_reason="cancelled",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise

        self._log_usage_dict(
            usage_seen,
            stream=True,
            finish_reason=finish_reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

        assembled = [raw_calls[i] for i in sorted(raw_calls)]
        yield StreamEvent(
            kind="done",
            message=Message(
                role="assistant",
                content="".join(content_parts),
                tool_calls=self._parse_tool_calls(assembled) if assembled else [],
            ),
        )
    # ------------------------------------------------------------------ 错误整理

    def _http_error_message(self, response: httpx.Response, api_key: str) -> str:
        status = response.status_code
        detail = _redact(_extract_detail(response), api_key)

        label = self.label
        if status == 401:
            head = (
                f"{label}鉴权失败（HTTP 401）：API key 无效或已过期，"
                f"请检查 .env 中的 {self.env_key_name}。"
            )
        elif status == 402:
            # 充值入口各家不同：只有 DeepSeek 的地址是确定的，别给别家编一个
            site = "https://platform.deepseek.com" if label == "DeepSeek" else "它的控制台"
            head = f"{label}拒绝请求（HTTP 402）：账户余额不足，请前往 {site} 充值。"
        elif status == 429:
            head = f"{label}限流（HTTP 429）：请求过于频繁，请稍后重试。"
        else:
            head = f"{label}返回异常状态码 HTTP {status}。"

        if detail:
            return f"{head} 详情：{detail}"
        return head

    # ------------------------------------------------------------------ 响应解析

    async def _post_with_retry(self, payload: dict[str, Any], headers: dict[str, str]):
        """发请求；**只对连接类错误**重试（第 127 步）。

        为什么需要：桌面端网络会瞬时抖一下 —— 实测同一天 00:50 连不上
        `api.deepseek.com`、00:53 又完全正常。之前一次 `ConnectError` 就让整轮对话
        报"网络错误"，用户毫无办法。

        为什么不重试 4xx/5xx：那是请求本身的问题（key 错、模型名错、余额不足），
        重试只会多花钱，还会把真正的原因埋在"尝试了 N 次"后面。
        """
        last: Exception | None = None
        for attempt in range(1, NETWORK_ATTEMPTS + 1):
            try:
                return await self._client.post(
                    self.endpoint, json=payload, headers=headers
                )
            except _RETRYABLE as exc:
                last = exc
                if attempt >= NETWORK_ATTEMPTS:
                    break
                logger.warning(
                    "调用模型网络中断（第 %d/%d 次）：%s，%.1f 秒后重试",
                    attempt,
                    NETWORK_ATTEMPTS,
                    type(exc).__name__,
                    RETRY_BACKOFF_SECONDS * attempt,
                )
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        assert last is not None
        raise last

    def _log_usage(
        self,
        response: httpx.Response,
        *,
        stream: bool,
        latency_ms: int | None = None,
    ) -> None:
        """非流式调用的用量入口：从响应体里取 usage 与 finish_reason。"""
        try:
            body = response.json()
        except ValueError:              # 不是 JSON：交给 _parse_reply 去报错
            return
        if not isinstance(body, dict):
            return
        finish_reason = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            if isinstance(reason, str) and reason:
                finish_reason = reason
        self._log_usage_dict(
            body.get("usage") if isinstance(body.get("usage"), dict) else None,
            stream=stream,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
        )

    def _log_usage_dict(
        self,
        usage: dict[str, Any] | None,
        *,
        stream: bool,
        finish_reason: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """把 usage 写日志 + 落账本（流式与非流式共用，第 111 步）。

        - 日志给人看：一行，带模型/功能/会话（第 110 步）；
        - 账本给统计看：`core/usage.py`，一行一次调用，`turn_id` 可加总成轮。

        没有 usage 时**照样记一行**（`usage_source=missing`）——
        "这次不知道花了多少"与"这次花了 0"是两件事。
        """
        from core import log_context, usage as usage_store

        feature, session_short = log_context.current()
        if isinstance(usage, dict) and usage:
            logger.info(
                "token 用量：prompt=%s completion=%s total=%s（model=%s, feature=%s%s）",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                self._model,
                feature,
                f", session={session_short}" if session_short else "",
            )
            if finish_reason == "length":
                # 被 max_tokens 截断了：日志要看得见，否则只会看到"解析失败"
                logger.warning(
                    "输出被 max_tokens 截断（model=%s, feature=%s）", self._model, feature
                )

        usage_store.record(
            self._storage,
            provider=str(self._provider.name if self._provider else ""),
            model=self._model,
            feature=feature,
            session_id=log_context.full_session(),
            turn_id=log_context.turn_id(),
            step=log_context.step(),
            stream=stream,
            usage=usage if isinstance(usage, dict) else None,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            # 第 112 步：日用量告警（只告警不拒绝，阈值 0 = 关闭）
            warn_at=int(getattr(self._settings, "daily_token_warn", 0) or 0),
            # 第 123 步：这一轮用的是哪版提示词（Agent 在轮开始时设的）
            prompt_version=log_context.prompt_version(),
        )

    def _parse_reply(self, response: httpx.Response) -> Message:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(
                f"{self.label}返回的内容不是合法 JSON（{type(exc).__name__}）：{_truncate(response.text)}"
            ) from exc

        if not isinstance(data, dict):
            raise LLMError(f"{self.label}响应结构不符合预期（顶层不是对象）：{_preview(data)}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"{self.label}响应缺少可用的 choices 字段：{_preview(data)}")

        first = choices[0]
        if not isinstance(first, dict):
            raise LLMError(f"{self.label}响应的 choices[0] 不是对象：{_preview(first)}")

        raw_message = first.get("message")
        if not isinstance(raw_message, dict):
            raise LLMError(
                f"DeepSeek 响应的 choices[0] 缺少 message 字段：{_preview(first)}"
            )

        content = raw_message.get("content")
        if content is None:
            # 只调工具不说话的回复 content 就是 None，统一归一化成空串，
            # 免得上层到处写 ``or ""``。
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        return Message(
            role="assistant",
            content=content,
            tool_calls=self._parse_tool_calls(raw_message.get("tool_calls")),
        )

    def _parse_tool_calls(self, raw_calls: Any) -> list[ToolCall]:
        if raw_calls is None:
            return []
        if not isinstance(raw_calls, list):
            raise LLMError(f"DeepSeek 响应的 tool_calls 不是数组：{_preview(raw_calls)}")

        calls: list[ToolCall] = []
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                raise LLMError(f"DeepSeek 响应的 tool_calls[{index}] 不是对象：{_preview(item)}")

            function = item.get("function")
            if not isinstance(function, dict):
                raise LLMError(
                    f"DeepSeek 响应的 tool_calls[{index}] 缺少 function 字段：{_preview(item)}"
                )

            raw_arguments = function.get("arguments")
            if raw_arguments is None:
                raw_arguments = ""
            elif not isinstance(raw_arguments, str):
                # 少数兼容网关直接返回对象；序列化成字符串，保证
                # raw_arguments 始终是能原样回灌的 JSON 文本。
                raw_arguments = json.dumps(raw_arguments, ensure_ascii=False)

            call_id = item.get("id")
            name = function.get("name")

            calls.append(
                ToolCall(
                    id=call_id if isinstance(call_id, str) else "",
                    name=name if isinstance(name, str) else "",
                    arguments=self._parse_arguments(raw_arguments),
                    raw_arguments=raw_arguments,
                )
            )
        return calls

    def _parse_arguments(self, raw_arguments: str) -> dict[str, Any]:
        """把模型给的 arguments 字符串解析成 dict。

        模型会幻觉出不合法 JSON（常见于长参数被截断）。这时不能让整条请求失败：
        ``arguments`` 退回 ``{}``，``raw_arguments`` 保留原文，并留下 WARNING 日志。
        上层仍能正常处理——Agent 把空参数交给
        :meth:`tools.base.BaseTool.safe_run`，必填校验会生成可读错误回灌给模型，
        模型就有机会重新生成参数，而不是整个对话 500。
        """
        text = raw_arguments.strip()
        if not text:
            return {}

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "%s 返回的 tool arguments 不是合法 JSON（%s），原始内容：%s",
                self.label,
                exc,
                _truncate(raw_arguments),
            )
            return {}

        if not isinstance(parsed, dict):
            logger.warning(
                "%s 返回的 tool arguments 不是 JSON 对象（实际为 %s），原始内容：%s",
                self.label,
                type(parsed).__name__,
                _truncate(raw_arguments),
            )
            return {}

        return parsed
