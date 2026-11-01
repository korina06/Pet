"""`search_web` 的压缩与装配：10 条 3192 字的真实返回要压到 5 条约 700 字。

断言钉住「压掉了什么」，防止有人把 uuid、displayUrl 又加回来。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mcp_catalog import McpServerSpec, swap_web_search_tools  # noqa: E402
from tools.mcp_remote import DiscoveredTool  # noqa: E402
from tools.web_search_tool import MAX_RESULTS, SNIPPET_CHARS, WebSearchTool, format_results  # noqa: E402


def _payload(count: int = 10) -> dict:
    return {
        "query": "成都 11 月 旅游 攻略",
        "results": [
            {
                "uuid": f"c5ac9b8e-284e-4eff-aff6-{index:012d}",
                "title": f"成都市_第{index}条",
                "url": f"https://baike.baidu.com/item/%E6%88%90%E9%83%BD%E5%B8%82/50208{index:02d}",
                "snippet": "成都市（Chengdu City），简称“蓉”，别称蓉城、锦城，四川省辖地级市、省会、副省级市、超大城市。",
                "displayUrl": "https://baike.baidu.com",
            }
            for index in range(1, count + 1)
        ],
        "totalResults": 100000,
    }


def test_format_keeps_title_snippet_url_and_drops_the_rest() -> None:
    text = format_results(_payload(10))

    assert "成都市_第1条" in text and f"成都市_第{MAX_RESULTS}条" in text, "上限内的条目要按顺序全在"
    assert f"成都市_第{MAX_RESULTS + 1}条" not in text, "超出上限的条目不许出现"
    assert "https://baike.baidu.com/item/" in text
    assert "c5ac9b8e" not in text, "uuid 只喂 crawl_webpage，抓正文这一步已经摘掉了"
    assert "displayUrl" not in text and "https://baike.baidu.com\n" not in text
    assert "共约 100000 条" in text


def test_format_caps_count_and_snippet_length() -> None:
    text = format_results(_payload(10))

    assert text.count("成都市_第") == MAX_RESULTS, f"最多 {MAX_RESULTS} 条"
    # 远端**不看我们传的 count**（实测传 5 仍回 8 条），上限必须在我们这边切
    five = format_results(_payload(8), limit=5)
    assert five.count("成都市_第") == 5 and "搜到 5 条" in five
    long_snippet = "宜" * (SNIPPET_CHARS + 20)
    payload = {"results": [{"title": "t", "url": "u", "snippet": long_snippet}]}
    once = format_results(payload)
    assert "…" in once, "超长摘要要截断并加省略号"
    assert "宜" * (SNIPPET_CHARS + 1) not in once


def test_format_survives_odd_shapes() -> None:
    assert "没搜到结果" in format_results({"results": []})
    assert "没搜到结果" in format_results({"results": None})
    assert "（搜索没有返回内容）" in format_results("")
    # 远端偶尔直接回纯文本：原样给模型，不要伪装成结构化结果
    assert "服务暂时不可用" in format_results("服务暂时不可用")


def test_parameters_are_valid_and_bounded() -> None:
    schema = WebSearchTool.parameters
    assert schema["required"] == ["query"]
    assert schema["properties"]["count"]["maximum"] == MAX_RESULTS
    assert WebSearchTool.remote_name == "bing_search"
    assert WebSearchTool.name == "search_web"


def _run(coro):
    return asyncio.run(coro)


def test_run_passes_count_through_and_returns_formatted(monkeypatch) -> None:
    seen: dict = {}

    async def fake_call(url, remote, args, *, token="", timeout=30.0):
        seen.update({"url": url, "remote": remote, "args": args})
        return json.dumps(_payload(6))

    monkeypatch.setattr("tools.web_search_tool.call_remote_tool", fake_call)

    tool = WebSearchTool(url="https://mcp.test/sse", token="sk-1")
    result = _run(tool.run(query="  成都  旅游  ", count=99))

    assert result.ok
    assert seen["args"] == {"query": "成都 旅游", "count": MAX_RESULTS}, "空白要收敛、条数要夹到上限"
    assert "成都市_第1条" in (result.content or "")


def test_run_says_what_is_missing() -> None:
    tool = WebSearchTool(url="")
    assert not _run(tool.run(query="成都")).ok

    plain = WebSearchTool(url="https://mcp.test/sse")
    result = _run(plain.run(query="   "))
    assert not result.ok and "不能是空的" in (result.error or "")


def test_swap_replaces_search_and_drops_crawl() -> None:
    tools = [
        DiscoveredTool(server="bing", url="https://mcp.test/sse", remote_name="bing_search"),
        DiscoveredTool(server="bing", url="https://mcp.test/sse", remote_name="crawl_webpage"),
        DiscoveredTool(server="bazi", url="https://mcp.test/sse", remote_name="getChineseCalendar"),
    ]
    specs = [McpServerSpec(name="bing", url="https://mcp.test/sse")]

    swapped, notes = swap_web_search_tools(tools, specs, "bing")

    names = [tool.name for tool in swapped]
    assert names[0] == "search_web"
    assert "bing_crawl_webpage" not in names, "抓正文要摘掉：实测 403，且整页太贵"
    assert "bazi_getChineseCalendar" in names, "别的服务器的工具不许动"
    assert notes == []
    assert swapped[0].server == "bing", "装配后要能按服务器筛回来"


def test_swap_is_quiet_when_not_configured() -> None:
    tools = [DiscoveredTool(server="bing", url="https://mcp.test/sse", remote_name="bing_search")]
    same, notes = swap_web_search_tools(tools, [], "")
    assert same is tools and notes == []

    _, notes = swap_web_search_tools(tools, [], "nope")
    assert notes and "没有这台" in notes[0]
