#!/usr/bin/env python
"""最小 MCP client：起一个真的 `scripts/mcp_server.py` 子进程，走 stdio 做一次完整握手
（initialize → tools/list → tools/call → 退出）。

单元测试用内存流验证协议处理，这里验证真子进程真收发。
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
ROOT = Path(__file__).resolve().parent.parent


class Client:
    """按行收发 JSON-RPC 的子进程客户端（够用就行，不追求协议完备）。"""

    def __init__(self, server: Path, timeout: float, raw: bool) -> None:
        self.timeout = timeout
        self.raw = raw
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self.proc = subprocess.Popen(
            [sys.executable, str(server)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # ---- 内部：读线程 -------------------------------------------------
    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # 服务端关了 stdout

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            sys.stderr.write(f"[server] {line}")

    # ---- 对外 ---------------------------------------------------------
    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._write(message)

        while True:
            line = self._read_line(method)
            reply = json.loads(line)
            if reply.get("id") == self._next_id:
                return reply
            # 服务端主动发的通知（目前没有）就跳过

    def _write(self, message: dict) -> None:
        if self.raw:
            print(f">>> {json.dumps(message, ensure_ascii=False)}")
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_line(self, stage: str) -> str:
        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty:
            raise SystemExit(
                f"✗ {stage}：{self.timeout:.0f} 秒内没收到响应"
                f"（子进程还在跑吗？退出码 {self.proc.poll()}）"
            ) from None
        if line is None:
            code = self.proc.wait(timeout=5)
            raise SystemExit(f"✗ {stage}：服务端提前退出（退出码 {code}）")
        line = line.strip()
        if not line:
            return self._read_line(stage)
        if self.raw:
            print(f"<<< {line}")
        return line

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _print_recipe_names(result: dict) -> int:
    """把 tools/call 的返回摊平成"几道菜 + 名字"，返回条数。"""
    if "error" in result:
        print(f"  ✗ 工具报错：{result['error']}")
        return -1

    payload = result.get("result", {})
    content = payload.get("content") or []
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            break
    print(f"  isError={payload.get('isError', False)}，文本 {len(text)} 字")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  （返回的不是 JSON，前 200 字）{text[:200]}")
        return 0 if text else -1

    items = data.get("items") or []
    names = [str(item.get("name", "?")) for item in items]
    print(f"  结果 {len(items)} 道：{'、'.join(names) if names else '（空）'}")
    note = data.get("vector_note") or ""
    if note:
        print(f"  注：{note}")
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="真起一个 MCP 子进程，走一遍 initialize → tools/list → tools/call。"
    )
    parser.add_argument(
        "--server",
        default=str(ROOT / "scripts" / "mcp_server.py"),
        help="服务端脚本路径（默认仓库里的 scripts/mcp_server.py）",
    )
    parser.add_argument(
        "--params",
        default='{"keyword": "下酒"}',
        help='search_recipes 的参数（JSON），默认 {"keyword": "下酒"}',
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="每步等待秒数")
    parser.add_argument("--json", action="store_true", help="打印收发的原始 JSON")
    args = parser.parse_args(argv)

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as error:
        print(f"✗ --params 不是合法 JSON：{error}")
        return 1
    if not isinstance(params, dict):
        print("✗ --params 必须是一个 JSON 对象")
        return 1

    client = Client(Path(args.server), args.timeout, args.json)
    try:
        print("1/4 initialize")
        reply = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pet-probe", "version": "1.0"},
            },
        )
        info = reply.get("result", {})
        print(
            f"  协议 {info.get('protocolVersion')}"
            f" | 服务端 {info.get('serverInfo', {}).get('name')}"
            f" 能力 {sorted(info.get('capabilities', {}))}"
        )

        print("2/4 notifications/initialized")
        client.notify("notifications/initialized")

        print("3/4 tools/list")
        reply = client.request("tools/list")
        tools = reply.get("result", {}).get("tools", [])
        print(f"  {len(tools)} 个工具：{[tool.get('name') for tool in tools]}")
        if not any(tool.get("name") == "search_recipes" for tool in tools):
            print("✗ 暴露的工具里没有 search_recipes")
            return 1

        print(f"4/4 tools/call search_recipes {json.dumps(params, ensure_ascii=False)}")
        reply = client.request(
            "tools/call", {"name": "search_recipes", "arguments": params}
        )
        if _print_recipe_names(reply) < 0:
            return 1

        print("✓ 四步全通：真子进程 + 真 stdio + 真检索")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
