#!/usr/bin/env python
"""把 `search_recipes` 暴露成 MCP server（stdio 传输，JSON-RPC 2.0）。

手写协议以便不引入依赖、也能在测试里用内存流真跑一遍；对外只导出允许的工具。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, TextIO

# 脚本直接跑时，把仓库根挂到 sys.path（`python scripts/mcp_server.py` 的 cwd 不确定）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.storage import Storage, resolve_db_path  # noqa: E402
from tools.recipe_search_tool import RecipeSearchTool  # noqa: E402

#: MCP 协议版本（stdio）。客户端给的版本在支持列表里就回它，否则回我们最新的。
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
#: 刻意只导出一个工具 —— 见文首第 1 条收窄
ALLOWED_TOOLS = ("search_recipes",)
SERVER_INFO = {"name": "pet-recipes", "version": "1.0.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """工具层可以直接抛它，由 `handle_message` 转成 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolHost:
    """工具宿主：一个 Storage + 收窄后的工具表。

    单进程、单线程里用（stdio server 本来就是一条请求一条响应地串行处理），
    所以不需要锁；`Storage` 自己的 `RLock` 管它自己那条连接。
    """

    def __init__(self, storage: Storage | None = None, *, vectors: bool = False) -> None:
        self.storage = storage
        embedder = judge = None
        if vectors:
            embedder, judge = _build_vector_stack()
        self.tools: dict[str, RecipeSearchTool] = {
            RecipeSearchTool.name: RecipeSearchTool(storage, embedder=embedder, judge=judge)
        }

    def list_definitions(self) -> list[dict[str, Any]]:
        """MCP 的 `tools/list` 结果。**schema 不复制**，直接取工具类上的那份。"""
        rows: list[dict[str, Any]] = []
        for name in ALLOWED_TOOLS:
            tool = self.tools.get(name)
            if tool is None:  # 收窄名单与实现不一致时宁可少给，也不给错的
                continue
            rows.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                }
            )
        return rows


def _build_vector_stack() -> tuple[Any, Any]:
    """`--vectors` 时才建的向量栈；任何一环缺失都降级成"只走字面"，并说明原因。"""
    try:
        from agents.deepseek_llm import DeepSeekLLM
        from core.embeddings import build_embedder
        from core.semantic_judge import LlmJudge

        embedder = build_embedder()
        if embedder is None:
            print("[mcp] 没配 GLM key（或关掉了 EMBEDDING_ENABLED），向量召回不启用", file=sys.stderr)
            return None, None
        return embedder, LlmJudge(DeepSeekLLM())
    except Exception as error:  # noqa: BLE001 —— 起不来就别让整个 server 挂掉
        print(f"[mcp] 向量栈没起来，本次只走字面检索：{error}", file=sys.stderr)
        return None, None


async def call_tool(host: ToolHost, name: str, arguments: Any) -> dict[str, Any]:
    """执行一次工具调用，返回 MCP 的 `tools/call` 结果。"""
    tool = host.tools.get(name)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"未知工具：{name}")
    if arguments is not None and not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments 必须是对象")

    # 只把 schema 里声明过的键交给工具：MCP client 常会塞 `_meta` 之类的东西，
    # 直接 `**arguments` 会以 TypeError 炸掉，而那本来不该是「工具失败」。
    declared = set((tool.parameters.get("properties") or {}).keys())
    kwargs = {
        key: value for key, value in (arguments or {}).items() if key in declared
    }

    result = await tool.run(**kwargs)
    return {
        "content": [{"type": "text", "text": _text_of(result)}],
        "isError": not result.ok,
    }


def _text_of(result: Any) -> str:
    """MCP 返回给**外部客户端**的内容（第 211 步）。

    为什么和喂给本地模型的不一样：本地模型读的是紧凑文本（省 token），
    而外部 client 是要解析的——它们期待结构化 JSON，压缩后的"一行一道菜"
    在那边就是坏数据。所以这里优先回 `data` 的 JSON，没有结构才回文本。
    """
    payload = result.as_payload() if hasattr(result, "as_payload") else None
    if getattr(result, "ok", False) and isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    return result.as_tool_content()


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle_message(host: ToolHost, message: Any) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；通知（没有 id）返回 None —— 不回应。"""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "消息必须是 JSON 对象")

    method = message.get("method")
    msg_id = message.get("id")
    if not isinstance(method, str):
        return _error(msg_id, INVALID_REQUEST, "缺少 method")

    if "id" not in message:  # 通知：`notifications/initialized` 走这里
        return None

    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params 必须是对象")

    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return _ok(
            msg_id,
            {
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": host.list_definitions()})

    if method == "tools/call":
        try:
            result = await call_tool(host, str(params.get("name") or ""), params.get("arguments"))
        except RpcError as error:
            return _error(msg_id, error.code, error.message)
        except Exception as error:  # noqa: BLE001 —— 工具内部异常不能掀掉整个 server
            return _error(msg_id, INTERNAL_ERROR, f"工具内部错误：{error}")
        return _ok(msg_id, result)

    return _error(msg_id, METHOD_NOT_FOUND, f"没实现的方法：{method}")


def serve(host: ToolHost, stdin: TextIO, stdout: TextIO) -> int:
    """stdio 主循环：一行一条 JSON-RPC 消息，一行一条响应。

    返回码：0 = 对端关掉了 stdin（正常退出）。
    """
    for line in stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError as error:
            _write(stdout, _error(None, PARSE_ERROR, f"JSON 解析失败：{error}"))
            continue
        response = asyncio.run(handle_message(host, message))
        if response is not None:
            _write(stdout, response)
    return 0


def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 search_recipes 暴露成 MCP server（stdio）")
    parser.add_argument("--db", default="", help="库文件路径（默认 data/pet.db，可用 PET_DB_PATH）")
    parser.add_argument("--vectors", action="store_true", help="打开向量召回（要 API key，按次计费）")
    parser.add_argument("--list", action="store_true", help="只打印工具定义然后退出")
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK：不强制 UTF-8 的话，中文描述到了 client 那边就是乱码
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    # `--list` 不该顺手在磁盘上建库，用内存库看定义就够了
    storage = Storage(":memory:") if args.list else Storage(resolve_db_path(args.db or None))
    host = ToolHost(storage, vectors=args.vectors and not args.list)
    try:
        if args.list:
            payload = {"tools": host.list_definitions()}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print("[mcp] pet-recipes 已就绪，等待 JSON-RPC 消息（Ctrl+C 退出）", file=sys.stderr)
        return serve(host, sys.stdin, sys.stdout)
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
