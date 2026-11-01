"""DeepSeekLLM 的单元测试。

全部用 ``httpx.MockTransport`` 打桩，不发起任何真实网络请求。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from agents.deepseek_llm import TIMEOUT_SECONDS, DeepSeekLLM
from agents.llm import LLMError, LanguageModel
from core.config import Settings
from core.schemas import Message, ToolCall

API_KEY = "sk-unit-test-secret-key-0123456789"
BASE_URL = "https://api.deepseek.test"
MODEL = "deepseek-chat"

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


# --------------------------------------------------------------------- 测试脚手架


def make_settings(**overrides: Any) -> Settings:
    """构造一份测试用配置；显式传参优先级高于 .env，保证测试可复现。"""
    values: dict[str, Any] = {
        "deepseek_api_key": API_KEY,
        "deepseek_base_url": BASE_URL,
        "deepseek_model": MODEL,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def chat_response(message: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def tool_call_payload(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


# --------------------------------------------------------------------- 正常解析


@pytest.mark.asyncio
async def test_plain_text_reply_becomes_assistant_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({"role": "assistant", "content": "今天北京晴，26℃。"})

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        reply = await llm.complete([Message(role="user", content="北京天气怎么样？")])

    assert reply.role == "assistant"
    assert reply.content == "今天北京晴，26℃。"
    assert reply.tool_calls == []
    assert reply.tool_call_id is None


@pytest.mark.asyncio
async def test_tool_call_reply_parses_arguments_and_keeps_raw_string() -> None:
    # 刻意写成"非标准"的空白格式：如果实现重新序列化，字符串就不相等了。
    raw_arguments = '{ "city" : "北京" ,"days":3 }'

    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call_payload("call_weather_1", "get_weather", raw_arguments)],
            }
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        reply = await llm.complete(
            [Message(role="user", content="北京未来三天天气")], tools=[WEATHER_TOOL]
        )

    assert reply.role == "assistant"
    assert len(reply.tool_calls) == 1

    call = reply.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.id == "call_weather_1"
    assert call.name == "get_weather"
    assert isinstance(call.arguments, dict)
    assert call.arguments == {"city": "北京", "days": 3}
    assert call.raw_arguments == raw_arguments


@pytest.mark.asyncio
async def test_multiple_tool_calls_are_all_parsed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call_payload("call_1", "get_weather", '{"city": "北京"}'),
                    tool_call_payload("call_2", "get_weather", '{"city": "上海"}'),
                ],
            }
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        reply = await llm.complete(
            [Message(role="user", content="北京和上海天气")], tools=[WEATHER_TOOL]
        )

    assert [call.id for call in reply.tool_calls] == ["call_1", "call_2"]
    assert [call.name for call in reply.tool_calls] == ["get_weather", "get_weather"]
    assert [call.arguments for call in reply.tool_calls] == [{"city": "北京"}, {"city": "上海"}]
    assert [call.raw_arguments for call in reply.tool_calls] == [
        '{"city": "北京"}',
        '{"city": "上海"}',
    ]


@pytest.mark.asyncio
async def test_none_content_is_normalized_to_empty_string() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call_payload("call_1", "get_weather", '{"city": "北京"}')],
            }
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        reply = await llm.complete([Message(role="user", content="天气")], tools=[WEATHER_TOOL])

    assert reply.content == ""
    assert isinstance(reply.content, str)


@pytest.mark.asyncio
async def test_invalid_json_arguments_do_not_break_the_request(caplog: pytest.LogCaptureFixture) -> None:
    broken = '{"city": "北京", "days":}'  # 模型幻觉出的半截 JSON

    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call_payload("call_broken", "get_weather", broken)],
            }
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with caplog.at_level(logging.WARNING, logger="agents.deepseek_llm"):
            reply = await llm.complete([Message(role="user", content="天气")], tools=[WEATHER_TOOL])

    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == broken  # 原文保留，便于回灌与排查
    assert call.name == "get_weather"
    assert any("arguments" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_non_object_json_arguments_fall_back_to_empty_dict() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call_payload("call_1", "get_weather", "[1, 2, 3]")],
            }
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        reply = await llm.complete([Message(role="user", content="天气")], tools=[WEATHER_TOOL])

    assert reply.tool_calls[0].arguments == {}
    assert reply.tool_calls[0].raw_arguments == "[1, 2, 3]"


# --------------------------------------------------------------------- 请求体正确性


@pytest.mark.asyncio
async def test_request_body_and_headers_follow_openai_protocol() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return chat_response({"role": "assistant", "content": "ok"})

    history = [
        Message(role="system", content="你是一只桌宠"),
        Message(role="user", content="北京天气"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="get_weather", raw_arguments='{"city":"北京"}')],
        ),
        Message(role="tool", content="晴 26℃", tool_call_id="call_1", name="get_weather"),
    ]

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        await llm.complete(history, tools=[WEATHER_TOOL])

    body = captured["body"]
    assert captured["url"] == f"{BASE_URL}/chat/completions"
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["messages"] == [message.to_api() for message in history]
    # tool_call 的 arguments 必须原样回灌，而不是重新 dumps
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"city":"北京"}'
    assert body["tools"] == [WEATHER_TOOL]

    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["authorization"] == f"Bearer {API_KEY}"
    assert headers["content-type"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [None, []])
async def test_tools_field_is_omitted_without_tools(tools: list[dict[str, Any]] | None) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return chat_response({"role": "assistant", "content": "ok"})

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        await llm.complete([Message(role="user", content="你好")], tools=tools)

    assert "tools" not in captured["body"]


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_normalized() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return chat_response({"role": "assistant", "content": "ok"})

    async with make_client(handler) as client:
        llm = DeepSeekLLM(
            client=client,
            settings=make_settings(deepseek_base_url=f"{BASE_URL}/"),
        )
        await llm.complete([Message(role="user", content="你好")])

    assert captured["url"] == f"{BASE_URL}/chat/completions"


@pytest.mark.asyncio
async def test_default_client_uses_sixty_second_timeout() -> None:
    llm = DeepSeekLLM(settings=make_settings())
    try:
        assert llm.client.timeout == httpx.Timeout(TIMEOUT_SECONDS)
    finally:
        await llm.aclose()


# --------------------------------------------------------------------- 错误处理


@pytest.mark.asyncio
async def test_http_401_becomes_llm_error_without_leaking_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        # 上游常把 key 回显在错误详情里，这里还原这种最坏情况
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": f"Authentication Fails, Your api key: {API_KEY} is invalid",
                    "type": "authentication_error",
                }
            },
        )

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError) as excinfo:
            await llm.complete([Message(role="user", content="你好")])

    message = str(excinfo.value)
    assert API_KEY not in message
    assert "401" in message
    assert "API key" in message
    assert "DEEPSEEK_API_KEY" in message
    assert "***" in message  # 详情被脱敏而不是整段丢弃


@pytest.mark.asyncio
async def test_timeout_becomes_llm_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError, match="超时"):
            await llm.complete([Message(role="user", content="你好")])


@pytest.mark.asyncio
async def test_network_error_becomes_llm_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError, match="网络错误"):
            await llm.complete([Message(role="user", content="你好")])


@pytest.mark.asyncio
async def test_http_500_includes_upstream_detail() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "internal server error"}})

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError) as excinfo:
            await llm.complete([Message(role="user", content="你好")])

    assert "500" in str(excinfo.value)
    assert "internal server error" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"id": "chatcmpl-test"},  # 完全没有 choices
        {"id": "chatcmpl-test", "choices": []},  # 空数组
        {"id": "chatcmpl-test", "choices": [{"index": 0}]},  # 缺 message
    ],
)
async def test_malformed_response_becomes_llm_error(payload: dict[str, Any]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError):
            await llm.complete([Message(role="user", content="你好")])


@pytest.mark.asyncio
async def test_non_json_response_becomes_llm_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        with pytest.raises(LLMError):
            await llm.complete([Message(role="user", content="你好")])


# --------------------------------------------------------------------- 契约


@pytest.mark.asyncio
async def test_implements_language_model_protocol() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return chat_response({"role": "assistant", "content": "ok"})

    async with make_client(handler) as client:
        llm = DeepSeekLLM(client=client, settings=make_settings())
        assert isinstance(llm, LanguageModel)


@pytest.mark.asyncio
async def test_temperature_is_sent_only_when_explicitly_set() -> None:
    """评测要可复现（第 202 步 / 评价16）：`temperature` 只在显式传了才进请求体。

    为什么只做"可选"：应用路径不传 = 服务端默认温度，请求体与第 202 步之前一字不差；
    只有 `scripts/eval_retrieval.py` 的裁决者传 0，让 `--vectors` 的数字可复现。
    """
    bodies: dict[str, Any] = {}

    def make_handler(key: str):
        async def handler(request: httpx.Request) -> httpx.Response:
            bodies[key] = json.loads(request.content)
            return chat_response({"role": "assistant", "content": "ok"})

        return handler

    for key, temperature in (("default", None), ("pinned", 0.0)):
        async with make_client(make_handler(key)) as client:
            kwargs: dict[str, Any] = {}
            if temperature is not None:
                kwargs["temperature"] = temperature
            llm = DeepSeekLLM(client=client, settings=make_settings(), **kwargs)
            await llm.complete([Message(role="user", content="北京天气")])

    assert "temperature" not in bodies["default"], "不传就别带 —— 否则等于悄悄改了应用的采样"
    assert bodies["pinned"]["temperature"] == 0.0