"""测试用的假 LLM 与假工具。

这两个 fake 是整个测试策略的核心：
不联网、不花钱、不依赖外部服务是否可用，因此可以在每次交付前都跑一遍。
"""

from __future__ import annotations

import json
from typing import Any

from core.schemas import Message, ToolCall, ToolResult
from tools.base import BaseTool


class FakeLLM:
    """按预设脚本依次返回响应的假 LLM。

    每次调用 complete() 弹出脚本里的下一条消息，
    这样就可以精确地构造"先要求调工具、再给出最终回答"这种多步场景。
    """

    def __init__(self, scripted: list[Message]) -> None:
        self.scripted = list(scripted)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[dict[str, Any]] | None] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        # 记录调用现场，方便断言"回灌给模型的内容对不对"
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if not self.scripted:
            raise AssertionError("FakeLLM 的脚本已用尽，但模型又被调用了一次")
        return self.scripted.pop(0)


class FailingLLM:
    """总是抛 LLMError 的假 LLM，用于测试错误路径。"""

    def __init__(self, message: str = "模拟的模型故障") -> None:
        self.message = message

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Message:
        from agents.llm import LLMError

        raise LLMError(self.message)


class EchoTool(BaseTool):
    """原样回显参数的假工具。"""

    name = "echo"
    description = "回显输入内容，仅用于测试"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            ok=True,
            content=f"echo: {kwargs.get('text')}",
            tool_name=self.name,
            arguments=kwargs,
        )


class BoomTool(BaseTool):
    """总是失败的假工具，用于验证"失败结果回灌给模型"这一设计。"""

    name = "boom"
    description = "总是失败，仅用于测试"
    parameters = {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            ok=False,
            error="外部服务不可用",
            tool_name=self.name,
            arguments=kwargs,
        )


def assistant_tool_call(call_id: str, name: str, arguments_json: str) -> Message:
    """构造一条"模型要求调用工具"的 assistant 消息。

    arguments 必须按真实 provider 的行为解析好：DeepSeekLLM 会把 function.arguments
    这个 JSON 字符串 json.loads 成 dict 放进 arguments，同时把原串留在 raw_arguments。
    这里如果偷懒只填 raw_arguments，Agent 拿到的就是空 dict，
    测试会和真实运行行为不一致（曾因此漏掉过 bug）。
    """
    try:
        parsed = json.loads(arguments_json)
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return Message(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments=parsed,
                raw_arguments=arguments_json,
            )
        ],
    )


def assistant_text(text: str) -> Message:
    return Message(role="assistant", content=text)
