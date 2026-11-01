"""对话与工具调用的数据模型：消息、工具调用、工具描述与事件。

用不带业务逻辑的 dataclass 而不是 pydantic：Agent 循环里要频繁构造与比较，轻量即可；
要暴露给 HTTP 的地方在 `api/` 单独转成响应模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # 模型返回的原始 JSON 字符串，回灌给模型时要原样传回
    raw_arguments: str = "{}"


@dataclass
class Message:
    """一条对话消息。

    role=assistant 时可能同时带 content 和 tool_calls；
    role=tool 时必须带 tool_call_id。

    `recipes`（第 137 步）：这条回答当时给出的菜 id，**只用于前端恢复卡片**。
    刻意不进 `to_api()` —— 模型不需要知道，也不该让历史里的 id 影响下一轮。
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    #: 这条回答给出的菜 id（保序）。恢复会话时前端按它重新画卡片。
    recipes: list[int] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的请求体格式（DeepSeek 同协议）。"""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments,
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload


@dataclass
class ToolResult:
    """工具执行结果。

    ok=False 时 error 一定非空；这个结果不会被抛给上层，
    而是作为 tool 消息回灌给模型，让模型自己决定如何向用户解释。
    """

    ok: bool
    content: str = ""
    error: str | None = None
    # 便于前端展示"宠物在做什么"，以及面试时讲清调用链路
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    #: 结构化结果（第 41 步）。
    #:
    #: content 是给**模型**看的通顺中文，data 是给**代码**看的原始字段。
    #: 出行的行程流水线要拿真实坐标、评分、营业时间去算路线与可行性，
    #: 靠正则去解析 content 里的中文句子既脆弱又没法单测，所以直接从源头取结构。
    #: 默认 None：不填的工具与既有调用完全不受影响。
    data: dict[str, Any] | None = None

    def as_tool_content(self) -> str:
        """转成回灌给模型的内容。"""
        if self.ok:
            return self.content
        return f"工具执行失败：{self.error}"

    def as_payload(self) -> dict[str, Any] | None:
        """结构化结果（第 211 步）。

        第 211 步把 `search_recipes` 的 `content` 换成了**给人/模型看的紧凑文本**
        （纯数据 JSON 的键名重复占掉了 60% 的 token），结构化数据改放 `data`。
        于是"谁要结构"就从这里取，别再自己 `json.loads(content)` —— 那会
        在文本形态下静默抛出/拿到空表，卡片会无声消失。

        兼容：`data` 没填的工具（天气、地点等本来就是自然语言结果）退回解析
        `content`，解析不出就返回 None，让调用方按"没有结构"处理。
        """
        if isinstance(self.data, dict):
            return self.data
        if not self.ok or not (self.content or "").strip():
            # 失败结果没有结构；空 content 也不该被 `"" or "{}"` 变出一个空字典
            return None
        try:
            payload = json.loads(self.content)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


@dataclass
class AgentReply:
    """一次 Agent 调用的最终产物。"""

    content: str
    tool_invocations: list[ToolResult] = field(default_factory=list)
    steps: int = 0
    #: 模型调用失败的原因（第 66 步）。
    #:
    #: 为什么需要它：`content` 在失败时也是一句人话（"抱歉，我这边出了点问题…"），
    #: 于是**监控分不清"模型挂了"和"模型正常道歉"**——两者都是 HTTP 200、都是一段文字。
    #: 有了这个字段，上层可以继续返回友好的 200（聊天场景不该弹错误框），
    #: 同时据它打一条可区分的日志/告警。
    error: str | None = None
    #: 这一轮走过的状态序列（第 136 步）：plan / act / observe / answer / degrade。
    #:
    #: 只做**观测**：不影响返回内容，也不影响步数；有了它，
    #: 评测与日志就能断言"这一轮走了哪几步"，而不只是"最后说了什么"。
    states: tuple[str, ...] = ()
