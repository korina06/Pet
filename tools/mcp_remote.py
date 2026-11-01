"""把远端 MCP 服务器的工具接进注册表：按 URL 后缀选 SSE 或 streamable HTTP，
每次调用开一次会话、用完即关。

失败统一成 `McpUnavailable` 并带人话，不静默跳过；参数照搬远端 schema，不手抄。
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)

#: 单次调用的默认超时（秒）。车次查询本身要 5~15 秒，给到 20 秒；
#: 再长就不如直接告诉用户"这次没取到"。
DEFAULT_TIMEOUT_SECONDS = 20.0


class McpUnavailable(RuntimeError):
    """连不上、超时、协议不认、远端报错 —— 一律当作"这次没拿到"，不往上抛。"""


def transport_for(url: str) -> str:
    """按 URL 后缀判断传输方式。

    为什么不用配置项显式指定：多一个开关就多一种配错的可能，而这两者的
    区分在 URL 上是**唯一且稳定**的（MCP 规范里 SSE 端点就是 `/sse`）。
    """
    text = str(url or "").strip()
    if not text:
        raise McpUnavailable("没有配置 MCP 服务器地址")
    if not text.lower().startswith(("http://", "https://")):
        raise McpUnavailable(f"MCP 地址必须是 http(s) 开头：{text}")
    return "sse" if text.rstrip("/").lower().endswith("/sse") else "streamable_http"


@dataclass(frozen=True)
class RemoteTool:
    """远端工具的形状（名字 + 描述 + JSON Schema 原样）。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@asynccontextmanager
async def open_session(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    token: str = "",
):
    """开一次 MCP 会话，用完自动关。

    出错统一包成 `McpUnavailable`：`ExceptionGroup`（anyio 的任务组）在调用方
    看起来是"一堆子异常"，原样抛出只会让人以为是我们的 bug。

    `token` 非空时带上 `Authorization: Bearer …`（第 217 步）：ModelScope 广场
    托管的 MCP 服务都要它，而且**两头都要**——SSE 的握手请求和后续消息请求
    各自是一次 HTTP 请求，只在握手时带令牌会在第二批消息上 401。
    """
    transport = transport_for(url)
    headers = {"Authorization": f"Bearer {token}"} if str(token or "").strip() else {}
    try:
        from mcp import ClientSession  # 延迟导入：没装 mcp 时只有真正用它的路径才报错
    except ImportError as error:  # pragma: no cover - 依赖缺失时给一句能照着做的错
        raise McpUnavailable(
            "没装 mcp 依赖（pip install -r requirements.txt 后重试）"
        ) from error

    try:
        async with AsyncExitStack() as stack:
            if transport == "sse":
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(
                        url, headers=headers, timeout=timeout, sse_read_timeout=timeout
                    )
                )
            else:
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(url, headers=headers, timeout=timeout)
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            yield session
    except McpUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - 远端什么都可能出错，统一成人话
        raise McpUnavailable(_explain(error, url, transport)) from error


def _explain(error: BaseException, url: str, transport: str) -> str:
    """把底层异常翻成一句用户能看懂、我们能照着排查的话。"""
    leaves: list[BaseException] = [error]
    # anyio 的 ExceptionGroup 会把真正的错包在 sub-exceptions 里
    for sub in getattr(error, "exceptions", []) or []:
        leaves.append(sub)
        leaves.extend(getattr(sub, "exceptions", []) or [])

    for leaf in leaves:
        text = f"{type(leaf).__name__}: {leaf}"
        if isinstance(leaf, (TimeoutError,)) or "timeout" in text.lower():
            return f"MCP 服务器超时（{url}，{transport}）"
        if "connection" in text.lower() or "refused" in text.lower():
            return f"连不上 MCP 服务器（{url}）：服务没起来或被防火墙挡了"
        if isinstance(leaf, AssertionError):
            return f"MCP 服务器回的内容不符合协议（{url}）"
    detail = f"{type(leaves[-1]).__name__}: {leaves[-1]}"
    return f"MCP 调用失败（{url}）：{detail}"


def _text_of(result: Any) -> str:
    """从 `CallToolResult` 里取出可读文本（远端既有 text 也有 json 块）。"""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is None:
            parts.append(str(item))
        else:
            parts.append(str(text))
    return "\n".join(part for part in parts if part).strip()


async def list_remote_tools(
    url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, token: str = ""
) -> list[RemoteTool]:
    async with open_session(url, timeout=timeout, token=token) as session:
        listed = await session.list_tools()
    return [
        RemoteTool(
            name=str(tool.name),
            description=str(getattr(tool, "description", "") or ""),
            parameters=dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}),
        )
        for tool in getattr(listed, "tools", None) or []
    ]


async def call_remote_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    token: str = "",
) -> str:
    """调一次远端工具，返回它的文本结果。失败抛 `McpUnavailable`。"""
    async with open_session(url, timeout=timeout, token=token) as session:
        result = await session.call_tool(tool_name, arguments=dict(arguments or {}))
    text = _text_of(result)
    if getattr(result, "isError", False):
        raise McpUnavailable(text or f"{tool_name} 在远端执行失败")
    return text


def _cap(text: str, limit: int, label: str) -> str:
    """远端返回太长时截断，并**明确告诉模型被截了**（不然它会以为那就是全部）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n（{label}的返回被截断：原文 {len(text)} 字符，只保留了前 {limit} 字符。"
        "需要更具体的部分请缩小查询范围。）"
    )


class McpProxyTool(BaseTool):
    """把一个远端工具包装成本项目的 `BaseTool`。

    参数 schema 直接来自远端，工具名/描述由子类给定——中文描述比远端那句英文
    更影响模型"什么时候想起来用它"。
    """

    #: MCP 服务器地址（默认空 = 未配置，调用时直接说清楚）
    url: str = ""
    #: 服务器令牌（可选，第 217 步）
    token: str = ""
    #: 远端工具名（服务器那边注册的名字，如 `query-tickets`）
    remote_name: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    #: 远端返回的硬上限（字符）。**必须有**：真实 12306 查询一次回了 82,372 个字符
    #: （306 趟车），全塞进上下文至少 4 万 token，一轮就把预算烧穿。
    #: 子类可以压得更狠（`TrainTicketTool` 会把 306 趟压成 15 趟）。
    max_chars: int = 8000

    async def run(self, **kwargs: Any) -> ToolResult:
        if not str(self.url or "").strip():
            return ToolResult(
                ok=False,
                error=f"{self.display_label}还没配置服务器地址（见 .env 的说明）",
                tool_name=self.name,
                arguments=kwargs,
            )
        try:
            text = await call_remote_tool(
                self.url,
                self.remote_name,
                kwargs,
                timeout=self.timeout,
                token=self.token,
            )
        except McpUnavailable as error:
            logger.warning("%s 调用失败：%s", self.name, error)
            return ToolResult(
                ok=False, error=str(error), tool_name=self.name, arguments=kwargs
            )
        if not text:
            return ToolResult(
                ok=False,
                error=f"{self.display_label}这次没返回内容",
                tool_name=self.name,
                arguments=kwargs,
            )
        return ToolResult(
            ok=True,
            content=_cap(self._shape(text), self.max_chars, self.display_label),
            tool_name=self.name,
            arguments=kwargs,
        )

    def _shape(self, text: str) -> str:
        """子类可在这里把远端原文压成"模型真要看的东西"。

        注意顺序：先 `_shape` 再 `_cap`。反过来会先截断 JSON、`_shape` 解析失败、
        压缩逻辑静默退回原文——第 215 步实测踩过这个坑（压缩函数写了等于没写）。
        """
        return text


def tool_name_for(server: str, remote_name: str) -> str:
    """把「服务器名 + 远端工具名」拼成模型能接受的函数名（第 217 步）。

    为什么要拼前缀：两端都可能重名——黄历服务器的 `query` 和航班服务器的 `query`
    放到同一个工具面里，后注册的会顶掉前一个，而模型只会收到"工具不存在"。
    为什么必须净身：function calling 的名字只允许 `[A-Za-z0-9_-]{1,64}`，
    远端常见 `get_almanac`、`query-tickets`、`search.web`，点号/空格/中文都会让
    整个 tools 参数被服务端 400 拒掉（第 217 步实测过 DeepSeek 的报错）。
    """
    def clean(text: str, fallback: str) -> str:
        # 连字符统一换成下划线：两边都合法，但混着出现在同一张工具表里
        # （`train_get-current-time` 和 `bazi_query_almanac`）会让模型抄名字时出错。
        kept = "".join(
            char if (char.isascii() and (char.isalnum() or char in "-_")) else "_"
            for char in str(text or "")
        ).replace("-", "_").strip("_")
        return kept or fallback

    name = f"{clean(server, 'mcp')}_{clean(remote_name, 'tool')}"
    return name[:64]


class DiscoveredTool(McpProxyTool):
    """远端声明什么，我们就注册什么（第 217 步）。

    和 `McpProxyTool` 的区别只有一个：那类工具的参数 schema 是**我们手写**的
    （车次那种，字段少、语义清楚，值得为它单独写压缩逻辑）；这里的 schema 是
    **原样照搬**远端的 `inputSchema`——黄历、航班、搜索这三台服务器的参数我们
    不熟，猜一个字段名就等于整条失败。

    描述也照搬，但只在远端没给描述时补一句兜底：模型判断"要不要用这个工具"
    全靠这段文字，空着等于给它一个哑工具。
    """

    #: 服务器名（写进日志和报错，让人一眼知道是哪台服务器出的问题）
    server: str = ""

    def __init__(
        self,
        *,
        server: str,
        url: str,
        remote_name: str,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_chars: int = 8000,
    ) -> None:
        self.server = str(server)
        self.url = str(url)
        self.remote_name = str(remote_name)
        self.token = str(token)
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)
        self.name = tool_name_for(server, remote_name)
        self.description = str(description or "").strip() or (
            f"调用外部 MCP 服务器 {server} 的工具 {remote_name}。"
        )
        schema = dict(parameters or {})
        # 远端偶尔给 `{"properties": {...}}` 而漏掉 `type`：补上，否则有的服务端
        # 会整批拒绝（OpenAI 兼容层普遍要求 parameters.type == "object"）。
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self.parameters = schema
        # 这类工具**不**进轮盘菜单：菜单是"用户点一下就能用"的入口，
        # 而远端工具的名字/参数用户看不懂，硬塞进去只会变成一堆莫名其妙的按钮。
        self.show_in_menu = False
