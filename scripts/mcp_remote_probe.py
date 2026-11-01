"""手工验证外部 MCP 服务器，并把它们的工具清单写进启动缓存 `data/mcp_tools.json`。

启动只读缓存，所以服务器慢不影响开机、服务器挂也不影响起不来；改了地址要重跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_settings  # noqa: E402
from core.console import force_utf8, safe_print  # noqa: E402
from tools.mcp_catalog import (  # noqa: E402
    McpConfigError,
    McpServerSpec,
    ToolCache,
    cache_path,
    load_cache,
    save_cache,
    servers_from_settings,
    stamp_now,
)
from tools.mcp_remote import (  # noqa: E402
    McpUnavailable,
    call_remote_tool,
    list_remote_tools,
    transport_for,
)

ROOT = Path(__file__).resolve().parent.parent


def _first_line(text: str, limit: int = 100) -> str:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return lines[0][:limit] if lines else ""


def _describe(tool) -> list[str]:
    """工具摘要：名字 + 必填/可选参数 + 描述首行。参数名决定模型会不会传错。"""
    schema = tool.parameters or {}
    required = schema.get("required") or []
    keys = list((schema.get("properties") or {}).keys())
    optional = [key for key in keys if key not in required]
    bits = []
    if required:
        bits.append("必填 " + "/".join(str(name) for name in required))
    if optional:
        bits.append(
            "可选 " + "/".join(optional[:6]) + ("…" if len(optional) > 6 else "")
        )
    lines = [f"  - {tool.name}" + (f"（{'; '.join(bits)}）" if bits else "（无参数）")]
    head = _first_line(tool.description)
    if head:
        lines.append(f"      {head}")
    return lines


async def _probe(url: str, token: str, timeout: float) -> tuple[list | None, str]:
    """列工具；成功返回（工具列表, ""），失败返回（None, 人话原因）。"""
    try:
        transport_for(url)
    except McpUnavailable as error:
        return None, f"地址有问题：{error}"
    try:
        tools = await list_remote_tools(url, timeout=timeout, token=token)
    except McpUnavailable as error:
        return None, f"连不上：{error}"
    return tools, ""


async def _run_url_mode(
    url: str, token: str, tool: str, arguments: dict, timeout: float
) -> int:
    """第一种用法：验一台、可选试调一次。**不写缓存**（地址是临时的）。"""
    try:
        mode = transport_for(url)
    except McpUnavailable as error:
        safe_print(f"地址有问题：{error}")
        return 2
    safe_print(f"地址：{url}")
    safe_print(f"传输：{mode}")
    safe_print("")

    tools, problem = await _probe(url, token, timeout)
    if tools is None:
        safe_print(problem)
        return 1
    if not tools:
        safe_print("连上了，但它一个工具都没声明。")
        return 1

    safe_print(f"它声明了 {len(tools)} 个工具：")
    for item in tools:
        for line in _describe(item):
            safe_print(line)

    if not tool:
        return 0

    safe_print("")
    safe_print(f"试调 {tool}，参数 {json.dumps(arguments, ensure_ascii=False)}")
    try:
        text = await call_remote_tool(
            url, tool, arguments, timeout=timeout, token=token
        )
    except McpUnavailable as error:
        safe_print(f"调用失败：{error}")
        return 1
    safe_print("--- 返回 ---")
    safe_print(text[:2000] if text else "（空）")
    return 0


def _entries(settings) -> list[McpServerSpec]:
    """待抓取清单：`MCP_SERVERS` + 单独配置的 12306（后者用手写工具 + 白名单附加工具）。

    和 `main.py` 共用 `servers_from_settings`，两边不可能配出不一样的服务器集合。
    """
    return servers_from_settings(settings)


def _run_config_mode(names: list[str], cache_file: str, write: bool) -> int:
    settings = get_settings()
    path = cache_path(cache_file or settings.mcp_tools_cache, root=ROOT)
    cache = load_cache(path)

    try:
        specs = _entries(settings)
    except McpConfigError as error:
        safe_print(f"配置有问题：{error}")
        return 2

    if not specs:
        safe_print("没有配置任何 MCP 服务器。")
        safe_print(
            "在 .env 里写一行（JSON）：\n"
            '  MCP_SERVERS=[{"name":"bazi","url":"https://…/sse","token":"…"}]'
        )
        return 2

    if names:
        known = {spec.name for spec in specs}
        missing = [name for name in names if name not in known]
        if missing:
            safe_print(f"配置里没有这些服务器：{', '.join(missing)}")
            safe_print(f"现在配了：{', '.join(sorted(known))}")
            return 2
        specs = [spec for spec in specs if spec.name in names]

    # 只问一部分时，保留缓存里没被问到的服务器
    updated = dict(cache)
    failed = 0
    for spec in specs:
        safe_print(f"检查 {spec.name}：{spec.url}")
        if spec.token:
            safe_print("  带令牌（Authorization: Bearer …）")
        tools, problem = asyncio.run(_probe(spec.url, spec.token, spec.timeout))
        if tools is None:
            safe_print(f"  {problem}")
            failed += 1
            continue
        if not tools:
            safe_print("  连上了，但它一个工具都没声明")
            failed += 1
            continue
        safe_print(f"  {len(tools)} 个工具：")
        for item in tools:
            for line in _describe(item):
                safe_print(line)
        updated[spec.name] = ToolCache(
            url=spec.url,
            fetched_at=stamp_now(),
            tools=[
                {
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.parameters,
                }
                for item in tools
            ],
        )
        safe_print("")

    if write:
        save_cache(path, updated)
        safe_print(f"已写入缓存：{path}")
        safe_print("重启桌宠（python scripts/start.ps1）后这些工具才会进模型的能力表。")
    else:
        safe_print("--dry-run：没有写缓存")

    if failed:
        safe_print(f"有 {failed} 台失败，它们的工具不会出现（其余照常）。")
        return 1
    return 0


def _show_cache(cache_file: str) -> int:
    settings = get_settings()
    path = cache_path(cache_file or settings.mcp_tools_cache, root=ROOT)
    cache = load_cache(path)
    safe_print(f"缓存：{path}")
    if not cache:
        safe_print("  （空的，先跑一次不带 --show 的命令）")
        return 0
    for name, entry in sorted(cache.items()):
        safe_print(f"  [{name}] {entry.url}")
        safe_print(f"      抓取于 {entry.fetched_at}，{len(entry.tools)} 个工具")
        for tool in entry.tools:
            safe_print(f"      - {tool.get('name')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="验证外部 MCP 服务器，并抓工具清单写缓存")
    parser.add_argument(
        "target",
        nargs="*",
        help="一个 http(s) 地址（验一台），或若干服务器名（按配置抓这几台）",
    )
    parser.add_argument("--token", default="", help="Bearer 令牌（地址模式用）")
    parser.add_argument("--call", default="", help="试调的工具名（地址模式用）")
    parser.add_argument("--args", default="{}", help="试调参数（JSON），默认 {}")
    parser.add_argument("--timeout", type=float, default=20.0, help="超时秒数，默认 20")
    parser.add_argument("--cache", default="", help="缓存文件路径（默认取配置）")
    parser.add_argument("--show", action="store_true", help="只打印缓存，不联网")
    parser.add_argument("--dry-run", action="store_true", help="联网但不写缓存")
    args = parser.parse_args(argv)

    if args.show:
        return _show_cache(args.cache)

    targets = list(args.target)
    is_url_mode = bool(targets) and str(targets[0]).lower().startswith(
        ("http://", "https://")
    )

    if is_url_mode:
        try:
            arguments = json.loads(args.args)
        except ValueError as error:
            safe_print(f"--args 不是合法 JSON：{error}")
            return 2
        if not isinstance(arguments, dict):
            safe_print("--args 必须是一个 JSON 对象")
            return 2
        return asyncio.run(
            _run_url_mode(
                targets[0], args.token, args.call, arguments, args.timeout
            )
        )

    return _run_config_mode(targets, args.cache, write=not args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
