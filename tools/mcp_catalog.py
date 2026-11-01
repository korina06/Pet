"""配置驱动的外部 MCP 服务器：读 `MCP_SERVERS`、读/写工具清单缓存、把远端工具包成工具类。

启动只读缓存不联网；缓存里没有或地址对不上时返回一句人话，而不是静默少几个工具。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.mcp_remote import DEFAULT_TIMEOUT_SECONDS, DiscoveredTool

logger = logging.getLogger(__name__)

#: 缓存文件格式版本。将来若改结构，读到不认识的版本就当没缓存（并提示重跑 probe）。
CACHE_VERSION = 1

#: 服务器名只允许这些字符：它会进工具名（`bazi_query_almanac`），而 function
#: calling 的名字只认 `[A-Za-z0-9_-]`。中文名一律拒绝并给一句能照着改的错。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,23}$")


class McpConfigError(ValueError):
    """`MCP_SERVERS` 写错了。带上"第几项、哪个字段"，否则用户只能一行行猜。"""


@dataclass(frozen=True)
class McpServerSpec:
    """一台外部 MCP 服务器的配置。"""

    name: str
    url: str
    token: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    #: 只暴露缓存里的这几个远端工具（留空 = 全部）。远端工具太多时用它收敛，
    #: 工具位是有限的：一个服务器给你 20 个工具，模型选错的概率会明显上去。
    tools: tuple[str, ...] = ()
    max_chars: int = 8000


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def parse_servers(raw: str) -> list[McpServerSpec]:
    """把 `MCP_SERVERS` 的 JSON 解析成配置对象。写错了直接抛，并指出是第几项。

    为什么抛而不是"跳过这一项"：这段配置是用户手写的，写错了只会在**几个月后**
    表现为"模型怎么不查黄历了"。启动时就炸掉，比静默半残好。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise McpConfigError(
            f"MCP_SERVERS 不是合法 JSON（{error.msg}，第 {error.pos} 个字符附近）。"
            '正确形状：[{"name": "bazi", "url": "https://…/sse", "token": "…"}]'
        ) from error

    if not isinstance(loaded, list):
        raise McpConfigError("MCP_SERVERS 必须是 JSON 数组，例如：[{\"name\": …}]")

    specs: list[McpServerSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(loaded, start=1):
        if not isinstance(item, dict):
            raise McpConfigError(f"MCP_SERVERS 第 {index} 项不是对象")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not url:
            raise McpConfigError(f"MCP_SERVERS 第 {index} 项缺 url")
        if not name:
            raise McpConfigError(f"MCP_SERVERS 第 {index} 项缺 name（它会变成工具名前缀）")
        if not _NAME_RE.match(name):
            raise McpConfigError(
                f"MCP_SERVERS 第 {index} 项的 name 只能由字母/数字/下划线/连字符组成"
                f"（收到 {name!r}）——它要拼进工具名，模型那边只认这些字符"
            )
        if name in seen:
            raise McpConfigError(f"MCP_SERVERS 里 name 重复：{name}")
        seen.add(name)
        tools = item.get("tools") or ()
        if isinstance(tools, str):
            tools = [tools]
        if not isinstance(tools, (list, tuple)):
            raise McpConfigError(f"MCP_SERVERS 第 {index} 项的 tools 必须是数组")
        specs.append(
            McpServerSpec(
                name=name,
                url=url,
                token=str(item.get("token") or "").strip(),
                timeout=_as_float(item.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
                tools=tuple(str(tool) for tool in tools),
                max_chars=int(_as_float(item.get("max_chars"), 8000)),
            )
        )
    return specs


def _split_tools(raw: str) -> tuple[str, ...]:
    """`"a, b"` → `("a", "b")`；`"all"` 与 `"none"`/`""` 有专门含义，见 `servers_from_settings`。"""
    return tuple(
        part.strip() for part in str(raw or "").replace(";", ",").split(",") if part.strip()
    )


def servers_from_settings(settings: Any) -> list[McpServerSpec]:
    """从配置里拼出所有外部 MCP 服务器（第 217 步，启动与 probe 脚本共用一份逻辑）。

    除了 `MCP_SERVERS` 里手写的那几台，还**自动**把单独配置的 12306 服务器算进来：
    它已经有一个手写工具（`search_trains`，会把 306 趟压成 15 趟），但同一台服务器
    上还有「票价」「当前时间（相对日期）」「车站搜索」「中转换乘」这些工具——
    用户明确要了票价和相对日期，而它们和车次在同一个 URL 上，不该让人再配一遍。

    多出来的工具由 `MCP_12306_EXTRA_TOOLS` 白名单控制，默认只要票价和当前时间：
    把 7 个工具全给模型，它会开始用「车次号转官方编号」这种中间步骤工具，
    既多一轮调用、又多一个出错的地方。
    """
    specs = list(parse_servers(getattr(settings, "mcp_servers", "")))
    url = str(getattr(settings, "mcp_12306_url", "") or "").strip()
    extra = _split_tools(getattr(settings, "mcp_12306_extra_tools", ""))
    lowered = {item.lower() for item in extra}
    if url and "none" not in lowered:
        specs.append(
            McpServerSpec(
                name="train",
                url=url,
                token=str(getattr(settings, "mcp_12306_token", "") or ""),
                timeout=_as_float(
                    getattr(settings, "mcp_12306_timeout_seconds", ""),
                    DEFAULT_TIMEOUT_SECONDS,
                ),
                tools=() if "all" in lowered else extra,
            )
        )
    return specs


@dataclass
class ToolCache:
    """`data/mcp_tools.json` 的内存形态：服务器名 → 地址 + 抓到时的工具清单。"""

    url: str
    fetched_at: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "fetched_at": self.fetched_at,
            "tools": self.tools,
        }


def cache_path(configured: str, *, root: Path) -> Path:
    """缓存路径：相对路径按项目根解析（和 `.env` 里的库路径同一种写法）。"""
    text = str(configured or "").strip() or "data/mcp_tools.json"
    path = Path(text)
    return path if path.is_absolute() else (root / path)


def load_cache(path: Path) -> dict[str, ToolCache]:
    """读缓存。文件不存在、格式不认识 —— 都当成"没缓存"，不抛。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("MCP 工具清单缓存读不了（%s）：%s", path, error)
        return {}

    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        logger.warning(
            "MCP 工具清单缓存版本不认识（%s），忽略；重跑 scripts/mcp_remote_probe.py 即可",
            path,
        )
        return {}

    servers = payload.get("servers")
    if not isinstance(servers, dict):
        return {}

    cache: dict[str, ToolCache] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        tools = entry.get("tools")
        cache[str(name)] = ToolCache(
            url=str(entry.get("url") or ""),
            fetched_at=str(entry.get("fetched_at") or ""),
            tools=[dict(tool) for tool in tools or [] if isinstance(tool, dict)],
        )
    return cache


def save_cache(path: Path, cache: dict[str, ToolCache]) -> None:
    """写缓存（probe 脚本用）。先写临时文件再替换，避免半截文件让下次启动读不出来。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "servers": {name: entry.as_dict() for name, entry in cache.items()},
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def stamp_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_remote_tools(
    specs: list[McpServerSpec],
    cache: dict[str, ToolCache],
) -> tuple[list[DiscoveredTool], list[str]]:
    """按配置和缓存造工具，返回（工具列表, 需要打在日志里的提示）。

    提示**不是**异常：一台服务器没缓存，不该影响另外两台能用。
    """
    tools: list[DiscoveredTool] = []
    notes: list[str] = []
    used_names: set[str] = set()

    for spec in specs:
        entry = cache.get(spec.name)
        if entry is None:
            notes.append(
                f"MCP 服务器 {spec.name} 配了地址，但还没有工具清单："
                "先跑 python scripts/mcp_remote_probe.py（它会把清单写进缓存）"
            )
            continue
        if entry.url.strip() != spec.url.strip():
            notes.append(
                f"MCP 服务器 {spec.name} 的地址变过，旧工具清单已失效："
                "重跑 python scripts/mcp_remote_probe.py"
            )
            continue

        wanted = set(spec.tools)
        picked = [
            tool
            for tool in entry.tools
            if not wanted or str(tool.get("name") or "") in wanted
        ]
        if wanted:
            missing = sorted(wanted - {str(tool.get('name') or '') for tool in picked})
            if missing:
                notes.append(
                    f"MCP 服务器 {spec.name} 上找不到这些工具：{', '.join(missing)}"
                )

        for tool in picked:
            remote_name = str(tool.get("name") or "").strip()
            if not remote_name:
                continue
            discovered = DiscoveredTool(
                server=spec.name,
                url=spec.url,
                token=spec.token,
                remote_name=remote_name,
                description=str(tool.get("description") or ""),
                parameters=tool.get("parameters") or {},
                timeout=spec.timeout,
                max_chars=spec.max_chars,
            )
            if discovered.name in used_names:
                # 两台服务器的 `query` 撞名时，后一个加序号而不是覆盖前一个
                suffix = 2
                while f"{discovered.name}_{suffix}" in used_names:
                    suffix += 1
                renamed = f"{discovered.name}_{suffix}"[:64]
                notes.append(
                    f"工具名冲突，{spec.name}/{remote_name} 注册为 {renamed}"
                )
                discovered.name = renamed
            used_names.add(discovered.name)
            tools.append(discovered)

    return tools, notes


def swap_almanac_tools(
    tools: list[DiscoveredTool],
    specs: list[McpServerSpec],
    server_name: str,
) -> tuple[list[DiscoveredTool], list[str]]:
    """把某台服务器的「一天一查」黄历换成按日期区间查的 `search_almanac`（第 221 步）。

    为什么在工具面里**换掉**而不是两个都留：远端那个 `getChineseCalendar` 一次只回一天、
    回 16 个字段；留着它，模型问「30 到 31 号适合出门吗」就会连调两次、拿到两份大部分
    无关的 JSON。同一件事给模型两条路，等于让它有机会选更差的那条。

    返回值同样带"提示"而不是抛异常：黄历没接上不该影响车次和航班。
    """
    name = str(server_name or "").strip()
    if not name:
        return tools, []

    from tools.almanac_tool import AlmanacTool

    spec = next((item for item in specs if item.name == name), None)
    if spec is None:
        return tools, [
            f"MCP_ALMANAC_SERVER 配的是 {name}，但 MCP_SERVERS 里没有这台："
            "黄历工具不可用（车次、航班等其它工具照旧）"
        ]

    kept = [
        tool
        for tool in tools
        if not (
            getattr(tool, "server", "") == name
            and getattr(tool, "remote_name", "") == AlmanacTool.remote_name
        )
    ]
    almanac = AlmanacTool(
        url=spec.url, token=spec.token, timeout=spec.timeout, max_chars=spec.max_chars
    )
    notes: list[str] = []
    if len(kept) == len(tools):
        notes.append(
            f"{name} 的工具清单里没有 {AlmanacTool.remote_name}："
            "黄历工具照样接上，但可能调不通（重跑 mcp_remote_probe.py 看看它声明了什么）"
        )
    return [almanac, *kept], notes


def swap_web_search_tools(
    tools: list[DiscoveredTool],
    specs: list[McpServerSpec],
    server_name: str,
) -> tuple[list[DiscoveredTool], list[str]]:
    """把某台服务器的 `bing_search` 换成压缩版 `search_web`，并把 `crawl_webpage` 摘掉（第 222 步）。

    两条改动都有实测依据：

    - `bing_search` 默认 10 条、每条 5 个字段，真实一次 **3192 字**（约 2000 token）；
      模型真正要看的只有标题 / 摘要 / 链接。
    - `crawl_webpage` 抓正文：首页试抓百度百科直接 `HTTP 403`；能抓到的整页是长篇正文，
      一次就能吃掉大半轮预算。搜索回答"有什么"，正文让用户自己点开。

    返回值带"提示"而不是抛异常：搜索没接上不该影响车次、黄历。
    """
    name = str(server_name or "").strip()
    if not name:
        return tools, []

    from tools.web_search_tool import WebSearchTool

    spec = next((item for item in specs if item.name == name), None)
    if spec is None:
        return tools, [
            f"MCP_WEB_SEARCH_SERVER 配的是 {name}，但 MCP_SERVERS 里没有这台："
            "联网搜索不可用（车次、黄历等其它工具照旧）"
        ]

    dropped_remote = {WebSearchTool.remote_name, "crawl_webpage"}
    kept = [
        tool
        for tool in tools
        if not (
            getattr(tool, "server", "") == name
            and getattr(tool, "remote_name", "") in dropped_remote
        )
    ]
    search = WebSearchTool(
        url=spec.url, token=spec.token, timeout=spec.timeout, max_chars=spec.max_chars
    )
    # `McpProxyTool` 本身没有 `server` 字段，这里补上，免得日后装配逻辑按服务器筛工具时漏掉它
    search.server = name

    notes: list[str] = []
    declared = any(
        getattr(tool, "server", "") == name
        and getattr(tool, "remote_name", "") == WebSearchTool.remote_name
        for tool in tools
    )
    if not declared:
        notes.append(
            f"{name} 的工具清单里没有 {WebSearchTool.remote_name}："
            "联网搜索照样接上，但可能调不通（重跑 mcp_remote_probe.py 看看它声明了什么）"
        )
    return [search, *kept], notes
