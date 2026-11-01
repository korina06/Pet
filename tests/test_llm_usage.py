"""真实 token 用量：把 API 报的 `usage` 记进日志，流式这一路先不记。
"""

from __future__ import annotations

import asyncio
import logging

from agents.deepseek_llm import DeepSeekLLM
from core.config import Settings
from core.schemas import Message


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.is_success = True

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """只实现 `post`：`complete()` 用到的就这一个方法。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def post(self, *_args, **_kwargs) -> _FakeResponse:
        return _FakeResponse(self.payload)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # getMessage() 已经做过 % 格式化，别再套一层
        self.messages.append(record.getMessage())


def _llm(payload: dict) -> DeepSeekLLM:
    # 必须用关键字传：`DeepSeekLLM(Settings(...))` 会把 Settings 塞进 `client`
    # 位置（第 105 步）—— CI 上就是这么红的
    llm = DeepSeekLLM(
        settings=Settings(deepseek_api_key="test-key", deepseek_model="deepseek-chat")
    )
    llm._client = _FakeClient(payload)
    return llm


def _payload(usage: dict | None) -> dict:
    body = {
        "choices": [{"message": {"role": "assistant", "content": "好"}}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _run(llm: DeepSeekLLM) -> tuple[Message, list[str]]:
    logger = logging.getLogger("agents.deepseek_llm")
    # 测试进程里没有 basicConfig，logger 的有效级别是 WARNING —— 不设就收不到 info
    original_level = logger.level
    # 还要切断向 root 的传播：`main.py` 在导入时给 root 挂了一个写
    # `data/logs/app.log` 的 handler —— 测试的假 usage（prompt=120）会混进
    # 那份真日志，而用户正要拿它去定 token 预算。假数据必须留在测试里。
    original_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    collector = _Collect()
    logger.addHandler(collector)
    try:
        reply = asyncio.run(llm.complete([Message(role="user", content="你好")]))
    finally:
        logger.removeHandler(collector)
        logger.setLevel(original_level)
        logger.propagate = original_propagate
    return reply, collector.messages


def test_complete_logs_the_real_token_usage() -> None:
    """记的是 API 报的数，不是我们估算的字数。"""
    llm = _llm(
        _payload({"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128})
    )

    reply, logs = _run(llm)

    assert reply.content == "好"
    usage_logs = [line for line in logs if "token 用量" in line]
    assert usage_logs, f"没有记 token 用量，实际日志：{logs}"
    line = usage_logs[0]
    assert "prompt=120" in line
    assert "completion=8" in line
    assert "total=128" in line
    assert "deepseek-chat" in line, "要带上模型名，换模型时才对得上"


def test_a_reply_without_usage_does_not_crash_or_log() -> None:
    """有的网关不回 `usage`：不能因此报错，也不该编一条假记录。"""
    llm = _llm(_payload(None))

    reply, logs = _run(llm)

    assert reply.content == "好"
    assert not [line for line in logs if "token 用量" in line]


def test_streaming_usage_is_recorded_on_purpose_now() -> None:
    """流式**现在要记** usage（第 102 步，评价4 P1-2 推翻了第 96 步的取舍）。

    第 96 步的决定是"流式先不记，多一个协议分支，等真需要再说"。
    但食谱主路径正是流式 —— 不记的话拿这些数据定预算会**系统性低估**。
    实测 DeepSeek 与 GLM 都支持 `stream_options.include_usage`，所以补上了。

    这条测试钉的是"补上是有意的"，避免以后有人看到 `_log_usage` 出现在流式里
    以为是复制粘贴错误又删掉。
    """
    from agents import deepseek_llm as module

    source = open(module.__file__, encoding="utf-8").read()
    # 第 127 步：`complete_stream` 变成了"带重试的薄包装"，
    # 真正发请求、带 stream_options 的是 `_complete_stream_once`。断言跟着搬过去，
    # 意图不变：流式请求要 include_usage，且要落账。
    stream_start = source.index("async def _complete_stream_once(")
    tail = source[stream_start:]
    next_def = min(
        (tail.index(marker) for marker in ("\n    async def ", "\n    def ") if marker in tail),
        default=len(tail),
    )
    stream_body = tail[:next_def]
    assert "stream_options" in stream_body, "流式请求要带 include_usage"
    assert "_log_usage_dict" in stream_body, "流式也要记 usage"
    # 包装层必须存在：否则瞬时断网又会整轮失败
    assert "async def complete_stream(" in source
    assert "NETWORK_ATTEMPTS" in source


# ------------------------- 流式 usage（第 102 步）


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.is_success = True

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeStreamCtx:
    def __init__(self, lines: list[str]) -> None:
        self._response = _FakeStreamResponse(lines)

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeStreamClient:
    """只实现 `stream()`：`complete_stream()` 用到的就这一个。"""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def stream(self, *_args, **_kwargs) -> _FakeStreamCtx:
        return _FakeStreamCtx(self.lines)


def _stream_lines(usage: dict | None) -> list[str]:
    import json as _json

    lines = ['data: ' + _json.dumps({"choices": [{"delta": {"content": "好"}}]})]
    if usage is not None:
        # 真实协议里 usage 那一块 **choices 是空数组** —— 处理时必须在
        # "没有 choices 就 continue" 之前，否则永远读不到
        lines.append('data: ' + _json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return lines


def test_streaming_records_usage_too() -> None:
    """食谱主路径是流式：不记这一条，用量就会系统性低估（评价4 P1-2）。"""
    # 必须用关键字传：`DeepSeekLLM(Settings(...))` 会把 Settings 塞进 `client`
    # 位置（第 105 步）—— CI 上就是这么红的
    llm = DeepSeekLLM(
        settings=Settings(deepseek_api_key="test-key", deepseek_model="deepseek-chat")
    )
    llm._client = _FakeStreamClient(
        _stream_lines(
            {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
        )
    )

    logger = logging.getLogger("agents.deepseek_llm")
    original_level, original_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    collector = _Collect()
    logger.addHandler(collector)
    try:
        async def drain() -> list[str]:
            texts: list[str] = []
            async for event in llm.complete_stream(
                [Message(role="user", content="数到二")]
            ):
                if event.kind == "delta":
                    texts.append(event.text)
            return texts

        texts = asyncio.run(drain())
    finally:
        logger.removeHandler(collector)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    assert "".join(texts) == "好"
    usage_logs = [line for line in collector.messages if "token 用量" in line]
    assert usage_logs, f"流式也要记 usage，实际日志：{collector.messages}"
    assert "prompt=9" in usage_logs[0]
    assert "total=12" in usage_logs[0]


def test_streaming_sends_include_usage() -> None:
    """请求里必须带 `stream_options.include_usage`，否则服务端不会回 usage。"""
    sent: dict = {}

    class _Capture(_FakeStreamClient):
        def stream(self, *_args, **kwargs):
            sent.update(kwargs.get("json") or {})
            return _FakeStreamCtx(_stream_lines(None))

    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"))
    llm._client = _Capture([])

    async def drain() -> None:
        async for _event in llm.complete_stream([Message(role="user", content="hi")]):
            pass

    asyncio.run(drain())
    assert sent.get("stream_options") == {"include_usage": True}


def test_the_llm_gets_its_settings_as_a_keyword_not_positionally() -> None:
    """`DeepSeekLLM(Settings(...))` 会把 Settings 塞进 `client`（第 105 步）。

    这是 CI 上 4 条 `test_llm_usage` 报"未配置 DeepSeek 的 API key"的根因：
    本地 `.env` 里有真 key，`settings` 默认值恰好能用，所以看不出来；
    runner 上没有 `.env`，默认 `Settings()` 的 key 是空的 —— 一取就炸。

    这条测试把这个坑钉死：右半边证明"位置传参确实会传错"，左半边证明正确写法。
    """
    correct = DeepSeekLLM(
        settings=Settings(deepseek_api_key="k", deepseek_model="deepseek-chat")
    )
    assert correct._require_key() == "k"
    assert correct.provider is not None and correct.provider.api_key == "k"

    misuse = DeepSeekLLM(Settings(deepseek_api_key="k"))
    assert isinstance(misuse.client, Settings), (
        "位置传参会把 Settings 当成 httpx client —— 别再用位置参数"
    )


def test_usage_log_carries_feature_and_session() -> None:
    """token 日志要能分清是聊天还是食谱花的钱（第 110 步）。

    第 96 步只有总数：`prompt=2272 …（model=deepseek-chat）`。
    要定预算就得按功能看 —— 加了 `feature` 与 `session`（前 8 位）。
    """
    from core import log_context

    log_context.set_context("recipe", "abcdef1234567890")
    try:
        llm = _llm(_payload({"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}))
        _reply, logs = _run(llm)
    finally:
        log_context.set_context("?", "")

    usage_logs = [line for line in logs if "token 用量" in line]
    assert usage_logs, logs
    line = usage_logs[0]
    assert "feature=recipe" in line
    assert "session=abcdef12" in line, "session 只留前 8 位"


def test_usage_log_works_without_context() -> None:
    """没设置上下文（比如后台任务）时也要能记，只是标成 `feature=?`。"""
    llm = _llm(_payload({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))
    _reply, logs = _run(llm)

    line = next(line for line in logs if "token 用量" in line)
    assert "feature=?" in line
    assert "session=" not in line, "没有会话就不该硬写一个空字段"


def test_payload_carries_max_tokens_on_both_paths() -> None:
    """单次输出上限必须真的进请求（第 112 步）。

    设了它，模型才不会一路写下去 —— 而输出是计费的大头。
    """
    captured: dict = {}

    class _Capture(_FakeClient):
        async def post(self, *_a, **k):
            captured.update(k.get("json") or {})
            return await super().post(*_a, **k)

    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k", max_output_tokens=321))
    llm._client = _Capture(_payload(None))
    asyncio.run(llm.complete([Message(role="user", content="hi")]))
    assert captured.get("max_tokens") == 321

    streamed: dict = {}

    class _StreamCapture(_FakeStreamClient):
        def stream(self, *_a, **kwargs):
            streamed.update(kwargs.get("json") or {})
            return _FakeStreamCtx(_stream_lines(None))

    llm2 = DeepSeekLLM(settings=Settings(deepseek_api_key="k", max_output_tokens=321))
    llm2._client = _StreamCapture([])

    async def drain() -> None:
        async for _event in llm2.complete_stream([Message(role="user", content="hi")]):
            pass

    asyncio.run(drain())
    assert streamed.get("max_tokens") == 321


def test_length_finish_reason_raises_a_visible_warning() -> None:
    """被 max_tokens 截断时要留下 WARNING（第 112 步）。

    否则用户只会看到"食谱卡片解析失败"，没人知道是自己设的上限太小。
    """
    payload = _payload({"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 110})
    payload["choices"][0]["finish_reason"] = "length"

    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"))

    warnings: list[str] = []

    class _CollectWarn(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    logger = logging.getLogger("agents.deepseek_llm")
    original_level, original_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _CollectWarn()
    logger.addHandler(handler)

    class _Capture(_FakeClient):
        async def post(self, *_a, **_k):
            return _FakeResponse(payload)

    llm._client = _Capture(payload)
    try:
        asyncio.run(llm.complete([Message(role="user", content="hi")]))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    assert any("截断" in line for line in warnings), warnings


def test_a_call_writes_one_ledger_row_with_provider_and_meta() -> None:
    """每调用一次就落一行账（第 111 步），带上 provider/model/feature/step/turn。"""
    from core import log_context, usage
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    db = Storage(":memory:")
    ensure_schema(db)
    log_context.set_context("recipe", "sess-abcdef1234", "turn-1")
    log_context.set_step(2)
    usage.reset_turn_tokens()

    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"), storage=db)
    llm._client = _FakeClient(
        _payload({"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230})
    )
    asyncio.run(llm.complete([Message(role="user", content="hi")]))

    rows = usage.recent(db, 5)
    assert len(rows) == 1
    row = rows[0]
    assert row["feature"] == "recipe"
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-chat"
    assert row["step"] == 2
    assert row["turn_id"] == "turn-1"
    assert row["total"] == 230
    assert row["usage_source"] == "api"
    assert usage.turn_tokens() == 230


def test_a_cancelled_stream_is_still_recorded_as_missing() -> None:
    """被取消的流式调用也要落一行账（第 116 步）。

    客户端断开时**输入的 token 已经花掉了**，但拿不到 usage。
    不记的话，账本会系统性少算 —— 而"取消"本来是最该被看见的一类消耗。
    """
    import asyncio as _asyncio

    from core import log_context, usage
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    class _CancellingClient:
        def stream(self, *_a, **_k):
            class _Ctx:
                async def __aenter__(self_inner):
                    class _Resp:
                        is_success = True

                        async def aiter_lines(self_inner2):
                            yield 'data: {"choices":[{"delta":{"content":"半"}}]}'
                            raise _asyncio.CancelledError()

                        async def aread(self_inner2):
                            return b""

                    return _Resp()

                async def __aexit__(self_inner, *_exc):
                    return False

            return _Ctx()

    db = Storage(":memory:")
    ensure_schema(db)
    log_context.set_context("recipe", "sess-cancel", "turn-cancel")

    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"), storage=db)
    llm._client = _CancellingClient()

    async def drain() -> None:
        async for _event in llm.complete_stream([Message(role="user", content="hi")]):
            pass

    try:
        _asyncio.run(drain())
    except BaseException:      # noqa: BLE001 —— CancelledError 不是 Exception
        pass

    rows = usage.recent(db, 5)
    assert rows, "取消也要落账，否则账本少算"
    assert rows[0]["finish_reason"] == "cancelled"
    assert rows[0]["usage_source"] == "missing"
    assert rows[0]["total"] is None, "拿不到 usage 就不能编一个 0"
