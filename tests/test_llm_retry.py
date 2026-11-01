"""模型调用的网络重试：连接类错误重试、用尽后报错说清次数、4xx/5xx 不重试、流式只在没吐字前重试。
"""

from __future__ import annotations

import asyncio

import httpx

from agents import deepseek_llm as module
from agents.deepseek_llm import DeepSeekLLM
from core.config import Settings
from core.schemas import Message


def _llm(client) -> DeepSeekLLM:
    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"))
    llm._client = client
    return llm


class _Reply:
    is_success = True

    def json(self) -> dict:
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    @property
    def text(self) -> str:
        return ""


class _FlakyPost:
    """前 N 次抛 ConnectError，之后成功。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    async def post(self, *_args, **_kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise httpx.ConnectError("All connection attempts failed")
        return _Reply()

    async def aclose(self) -> None:
        return None


def _run(coro):
    """把退避时间压到 0，否则这组测试要等好几秒。"""
    original = module.RETRY_BACKOFF_SECONDS
    module.RETRY_BACKOFF_SECONDS = 0.0
    try:
        return asyncio.run(coro)
    finally:
        module.RETRY_BACKOFF_SECONDS = original


def test_a_transient_connect_error_is_retried_and_succeeds():
    client = _FlakyPost(failures=2)
    llm = _llm(client)

    reply = _run(llm.complete([Message(role="user", content="你好")]))

    assert reply.content == "好"
    assert client.attempts == 3, f"应当尝试 3 次，实际 {client.attempts}"


def test_running_out_of_attempts_raises_a_readable_error():
    client = _FlakyPost(failures=99)
    llm = _llm(client)

    error = None
    try:
        _run(llm.complete([Message(role="user", content="你好")]))
    except Exception as exc:      # noqa: BLE001 —— 这里就是要抓住它
        error = exc

    assert error is not None, "网络一直断，应该抛错"
    assert "网络错误" in str(error)
    assert client.attempts == module.NETWORK_ATTEMPTS


def test_http_4xx_is_not_retried():
    """key 错/模型名错重试没有意义：只该尝试一次，把原因直接说出来。"""

    class _Rejected:
        def __init__(self) -> None:
            self.attempts = 0

        async def post(self, *_args, **_kwargs):
            self.attempts += 1

            class _Bad:
                is_success = False
                status_code = 401
                text = '{"error": {"message": "invalid api key"}}'

                def json(self):
                    return {"error": {"message": "invalid api key"}}

            return _Bad()

        async def aclose(self) -> None:
            return None

    client = _Rejected()
    llm = _llm(client)

    error = None
    try:
        _run(llm.complete([Message(role="user", content="你好")]))
    except Exception as exc:      # noqa: BLE001
        error = exc

    assert error is not None
    assert client.attempts == 1, f"4xx 不该重试，实际尝试 {client.attempts} 次"


class _StreamCtx:
    """流式替身：构造时决定"第几次尝试才给数据"。"""

    def __init__(self, *, fail_first: int, fail_after_delta: bool) -> None:
        self.fail_first = fail_first
        self.fail_after_delta = fail_after_delta

    async def __aenter__(self):
        _StreamCtx.attempts += 1
        if _StreamCtx.attempts <= self.fail_first:
            raise httpx.ConnectError("All connection attempts failed")
        return _StreamResponse(fail_after_delta=self.fail_after_delta)

    async def __aexit__(self, *_exc) -> bool:
        return False


class _StreamResponse:
    is_success = True

    def __init__(self, *, fail_after_delta: bool) -> None:
        self.fail_after_delta = fail_after_delta

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"半"}}]}'
        if self.fail_after_delta:
            raise httpx.ReadError("connection reset by peer")
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
        yield "data: [DONE]"

    async def aread(self) -> bytes:
        return b""


class _StreamClient:
    def __init__(self, *, fail_first: int, fail_after_delta: bool = False) -> None:
        _StreamCtx.attempts = 0
        self.fail_first = fail_first
        self.fail_after_delta = fail_after_delta
        self.client = self

    def stream(self, *_args, **_kwargs):
        return _StreamCtx(
            fail_first=self.fail_first, fail_after_delta=self.fail_after_delta
        )

    async def aclose(self) -> None:
        return None


def test_a_stream_that_fails_before_any_text_is_retried():
    client = _StreamClient(fail_first=2)
    llm = _llm(client)

    async def drain() -> list[str]:
        chunks = []
        async for event in llm.complete_stream([Message(role="user", content="你好")]):
            chunks.append(getattr(event, "text", "") or "")
        return chunks

    chunks = _run(drain())

    assert "半" in "".join(chunks)
    assert _StreamCtx.attempts == 3, f"应当在吐字前重试到第 3 次，实际 {_StreamCtx.attempts}"


def test_a_stream_that_already_sent_text_never_retries():
    """已经吐过字就**不能**重试：用户会看到同一句话出现两遍。"""
    client = _StreamClient(fail_first=0, fail_after_delta=True)
    llm = _llm(client)

    async def drain() -> list[str]:
        chunks = []
        async for event in llm.complete_stream([Message(role="user", content="你好")]):
            chunks.append(getattr(event, "text", "") or "")
        return chunks

    error = None
    try:
        _run(drain())
    except Exception as exc:      # noqa: BLE001
        error = exc

    assert error is not None, "中途断流应当抛错，而不是静默结束"
    assert _StreamCtx.attempts == 1, f"吐过字之后不该再试，实际 {_StreamCtx.attempts}"
