"""外部 MCP 目录层：配置解析、缓存、工具构建。全部不联网。

真连一次远端由 `scripts/mcp_remote_probe.py` 负责。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_ROOT = ROOT / ".testtmp"


def _tmp_dir() -> Path:
    """不用 `tmp_path`：本机 `%TEMP%` 有 ACL 限制，pytest 建它的夹具会直接报错。"""
    path = TMP_ROOT / f"mcp-catalog-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path

from core.schemas import ToolResult  # noqa: E402
from tools.mcp_catalog import (  # noqa: E402
    McpConfigError,
    McpServerSpec,
    ToolCache,
    build_remote_tools,
    cache_path,
    load_cache,
    parse_servers,
    save_cache,
    servers_from_settings,
)
from tools.mcp_remote import DiscoveredTool, tool_name_for  # noqa: E402


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "mcp_servers": "",
        "mcp_12306_url": "",
        "mcp_12306_token": "",
        "mcp_12306_timeout_seconds": 20.0,
        "mcp_12306_extra_tools": "query-ticket-price,get-current-time",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------- 配置解析
def test_parse_servers_reads_name_url_token_and_tools():
    specs = parse_servers(
        json.dumps(
            [
                {"name": "bazi", "url": "https://x/sse", "token": "sk-1"},
                {
                    "name": "bing",
                    "url": "https://y/mcp",
                    "timeout_seconds": 30,
                    "tools": "search",
                },
            ]
        )
    )

    assert [spec.name for spec in specs] == ["bazi", "bing"]
    assert specs[0].token == "sk-1"
    assert specs[0].timeout == 20.0, "没写超时就用默认值"
    assert specs[1].timeout == 30.0
    assert specs[1].tools == ("search",), "单个字符串也要当成一个工具名"


def test_parse_servers_blank_means_no_server():
    assert parse_servers("") == []
    assert parse_servers("   ") == []


def test_parse_servers_rejects_broken_json_with_a_readable_message():
    try:
        parse_servers('[{"name": "bazi",}]')
    except McpConfigError as error:
        text = str(error)
        assert "不是合法 JSON" in text
        assert "MCP_SERVERS" in text
        assert "正确形状" in text, "报错要给一个能照着改的样例"
    else:  # pragma: no cover - 到这说明校验失效了
        raise AssertionError("坏 JSON 应该直接抛 McpConfigError")


def test_parse_servers_rejects_missing_fields_and_bad_names():
    cases = [
        ([{"name": "bazi"}], "缺 url"),
        ([{"url": "https://x/sse"}], "缺 name"),
        ([{"name": "八字", "url": "https://x/sse"}], "只能由字母/数字/下划线/连字符组成"),
        (
            [{"name": "bazi", "url": "a"}, {"name": "bazi", "url": "b"}],
            "name 重复",
        ),
        (["不是对象"], "第 1 项不是对象"),
        ({"name": "bazi"}, "必须是 JSON 数组"),
    ]
    for payload, expected in cases:
        try:
            parse_servers(json.dumps(payload, ensure_ascii=False))
        except McpConfigError as error:
            assert expected in str(error), f"{payload} 的报错里该有「{expected}」，实际：{error}"
        else:  # pragma: no cover
            raise AssertionError(f"{payload} 应该被拒绝")


# --------------------------------------------------------------- 服务器集合
def test_servers_from_settings_adds_12306_with_whitelist():
    specs = servers_from_settings(
        _settings(
            mcp_servers='[{"name": "bazi", "url": "https://x/sse"}]',
            mcp_12306_url="https://mcp.example/trains/mcp",
        )
    )

    assert [spec.name for spec in specs] == ["bazi", "train"]
    train = specs[1]
    assert train.url == "https://mcp.example/trains/mcp"
    assert train.tools == ("query-ticket-price", "get-current-time")
    assert train.timeout == 20.0


def test_servers_from_settings_can_turn_the_extra_tools_off_or_all_on():
    off = servers_from_settings(
        _settings(mcp_12306_url="https://x/mcp", mcp_12306_extra_tools="none")
    )
    assert [spec.name for spec in off] == []

    every = servers_from_settings(
        _settings(mcp_12306_url="https://x/mcp", mcp_12306_extra_tools="all")
    )
    assert every[0].tools == (), "all 表示不过滤（空元组 = 全部）"


def test_servers_from_settings_keeps_custom_servers_when_12306_is_off():
    specs = servers_from_settings(
        _settings(mcp_servers='[{"name": "bazi", "url": "https://x/sse"}]')
    )
    assert [spec.name for spec in specs] == ["bazi"]


# --------------------------------------------------------------- 缓存
def test_cache_round_trip():
    root = _tmp_dir()
    path = cache_path("data/mcp_tools.json", root=root)
    assert path == root / "data" / "mcp_tools.json"

    save_cache(
        path,
        {
            "bazi": ToolCache(
                url="https://x/sse",
                fetched_at="2026-10-29T20:00:00+08:00",
                tools=[{"name": "query_almanac", "parameters": {"type": "object"}}],
            )
        },
    )

    loaded = load_cache(path)
    assert list(loaded) == ["bazi"]
    assert loaded["bazi"].url == "https://x/sse"
    assert loaded["bazi"].tools[0]["name"] == "query_almanac"
    assert not path.with_suffix(".json.tmp").exists(), "临时文件不该留在磁盘上"
    shutil.rmtree(root, ignore_errors=True)


def test_cache_ignores_unknown_version_and_missing_file():
    root = _tmp_dir()
    path = root / "mcp_tools.json"
    path.write_text(json.dumps({"version": 999, "servers": {"x": {}}}), encoding="utf-8")
    assert load_cache(path) == {}

    assert load_cache(root / "nope.json") == {}
    shutil.rmtree(root, ignore_errors=True)


def test_cache_path_defaults_to_data_folder():
    root = Path("C:/示例")
    assert cache_path("", root=root) == root / "data" / "mcp_tools.json"
    assert cache_path("D:/x/cache.json", root=root) == Path("D:/x/cache.json")


# --------------------------------------------------------------- 造工具
def _cache_with(url: str, *tools: dict) -> dict[str, ToolCache]:
    return {"bazi": ToolCache(url=url, fetched_at="2026-10-29T20:00:00+08:00", tools=list(tools))}


def test_build_remote_tools_without_cache_explains_what_to_run():
    tools, notes = build_remote_tools(
        [McpServerSpec(name="bazi", url="https://x/sse")], {}
    )
    assert tools == []
    assert notes and "还没有工具清单" in notes[0]
    assert "mcp_remote_probe.py" in notes[0], "提示里要给出下一步该跑什么"


def test_build_remote_tools_drops_stale_cache_after_url_change():
    cache = _cache_with("https://old/sse", {"name": "query_almanac"})
    tools, notes = build_remote_tools(
        [McpServerSpec(name="bazi", url="https://new/sse")], cache
    )
    assert tools == [], "地址变了就不能拿旧 schema 去调新服务器"
    assert notes and "地址变过" in notes[0]


def test_build_remote_tools_prefixes_names_and_filters_whitelist():
    cache = _cache_with(
        "https://x/sse",
        {"name": "query_almanac", "parameters": {"type": "object", "properties": {}}},
        {"name": "get-current-time", "description": "现在几点"},
    )
    tools, _ = build_remote_tools(
        [McpServerSpec(name="bazi", url="https://x/sse", tools=("query_almanac",))], cache
    )

    assert [tool.name for tool in tools] == ["bazi_query_almanac"]
    assert all(tool.name.isascii() for tool in tools)


def test_build_remote_tools_reports_missing_whitelisted_tool():
    cache = _cache_with("https://x/sse", {"name": "query_almanac"})
    tools, notes = build_remote_tools(
        [McpServerSpec(name="bazi", url="https://x/sse", tools=("query_almanac", "nope"))],
        cache,
    )
    assert [tool.name for tool in tools] == ["bazi_query_almanac"]
    assert any("找不到这些工具" in note and "nope" in note for note in notes)


def test_build_remote_tools_renames_collisions_instead_of_overwriting():
    cache = {
        "a": ToolCache(url="https://a/mcp", tools=[{"name": "query"}]),
        "b": ToolCache(url="https://b/mcp", tools=[{"name": "query"}]),
    }
    tools, notes = build_remote_tools(
        [
            McpServerSpec(name="a", url="https://a/mcp"),
            McpServerSpec(name="b", url="https://b/mcp"),
        ],
        cache,
    )

    names = [tool.name for tool in tools]
    assert names == ["a_query", "b_query"], "前缀不同，本来就不会撞"
    assert notes == []

    # 真正的撞名：同一个服务器名 + 同一个远端工具名（有两个同名服务器配置时）
    collide = build_remote_tools(
        [
            McpServerSpec(name="srv", url="https://a/mcp"),
            McpServerSpec(name="srv", url="https://b/mcp"),
        ],
        {
            "srv": ToolCache(url="https://a/mcp", tools=[{"name": "query"}]),
        },
    )
    assert collide[0][0].name == "srv_query"


# --------------------------------------------------------------- 工具本身
def test_tool_name_for_is_model_safe():
    assert tool_name_for("bazi", "get_current_almanac") == "bazi_get_current_almanac"
    assert tool_name_for("bazi", "get-current-time") == "bazi_get_current_time"
    assert tool_name_for("八字", "查询 宜忌") == "mcp_tool", "中文全被换成下划线后要兜底"
    assert tool_name_for("x" * 80, "y") .startswith("x" * 64)


def test_discovered_tool_defaults_schema_and_stays_out_of_the_menu():
    tool = DiscoveredTool(server="bazi", url="https://x/sse", remote_name="query")
    assert tool.parameters == {"type": "object", "properties": {}}
    assert "bazi" in tool.description, "远端没给描述时也要有一句能读的兜底"
    assert tool.show_in_menu is False, "远端工具不该出现在轮盘菜单里"


def test_discovered_tool_says_so_when_no_url_configured():
    tool = DiscoveredTool(server="bazi", url="", remote_name="query")
    result = asyncio.run(tool.safe_run({}))
    assert result.ok is False
    assert "还没配置服务器地址" in result.error


def test_discovered_tool_calls_remote_and_passes_token(monkeypatch):
    import tools.mcp_remote as mcp_remote

    seen: dict = {}

    async def fake_call(url, name, arguments, *, timeout, token=""):
        seen.update(url=url, name=name, arguments=arguments, token=token, timeout=timeout)
        return "农历九月初九，宜出行"

    monkeypatch.setattr(mcp_remote, "call_remote_tool", fake_call)

    tool = DiscoveredTool(
        server="bazi",
        url="https://x/sse",
        remote_name="query_almanac",
        token="sk-1",
        timeout=7.0,
    )
    result = asyncio.run(tool.safe_run({"city": "南京"}))

    assert isinstance(result, ToolResult) and result.ok is True
    assert "宜出行" in result.content
    assert seen == {
        "url": "https://x/sse",
        "name": "query_almanac",
        "arguments": {"city": "南京"},
        "token": "sk-1",
        "timeout": 7.0,
    }, "令牌/超时/参数一个都不能丢，否则远端会 401 或参数不认"


def test_discovered_tool_reports_remote_failure_as_tool_error(monkeypatch):
    import tools.mcp_remote as mcp_remote

    async def broken(url, name, arguments, *, timeout, token=""):
        raise mcp_remote.McpUnavailable("MCP 服务器超时（https://x/sse，sse）")

    monkeypatch.setattr(mcp_remote, "call_remote_tool", broken)
    tool = DiscoveredTool(server="bazi", url="https://x/sse", remote_name="query")
    result = asyncio.run(tool.safe_run({}))

    assert result.ok is False
    assert "超时" in result.error, "失败原因要原样带给模型，它才能跟用户解释"
