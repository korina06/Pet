"""黄历工具：用两份真实返回（含 16 个字段）核对压缩逻辑该丢哪些、该留哪些。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.almanac_tool import AlmanacTool, day_line  # noqa: E402
from tools.mcp_catalog import McpServerSpec, swap_almanac_tools  # noqa: E402
from tools.mcp_remote import DiscoveredTool, McpUnavailable  # noqa: E402

DAY_30 = {
    "公历": "2026年10月30日 星期五",
    "农历": "农历丙午年九月廿一",
    "干支": "丙午 戊戌 丁丑",
    "生肖": "马",
    "纳音": "涧下水",
    "节气": "霜降",
    "二十八宿": "娄金狗吉",
    "彭祖百忌": "丁不剃头头必生疮 丑不冠带主不还乡",
    "喜神方位": "南",
    "阳贵神方位": "西北",
    "阴贵神方位": "西",
    "福神方位": "东",
    "财神方位": "西南",
    "冲煞": "冲羊(未)煞东",
    "宜": "祭祀,平治道涂,馀事勿取",
    "忌": "嫁娶,开市",
}

DAY_31 = {
    "公历": "2026年10月31日 星期六",
    "农历": "农历丙午年九月廿二",
    "节气": "霜降",
    "二十八宿": "胃土彘吉",
    "冲煞": "冲猴(申)煞北",
    "宜": "捕捉,畋猎,馀事勿取",
    "忌": "开市,交易,祭祀,入宅,安葬",
}


def test_day_line_keeps_what_a_trip_needs() -> None:
    text = day_line(DAY_30)
    assert "2026年10月30日 星期五" in text, "公历与星期要留着"
    assert "农历丙午年九月廿一" in text
    assert "霜降" in text, "节气对「这个季节去合适吗」有用"
    assert "宜：祭祀、平治道涂、馀事勿取" in text, "逗号要换成顿号，读起来才像话"
    assert "忌：嫁娶、开市" in text
    assert "冲羊(未)煞东" in text
    # 择日/算命用的字段必须丢掉，否则「宜」「忌」会被挤到注意力之外
    for dropped in ("涧下水", "彭祖百忌", "财神方位", "西北", "丙午 戊戌 丁丑", "马"):
        assert dropped not in text, f"不该留的字段出现了：{dropped}"
    assert len(text.splitlines()) <= 4, f"一天最多 4 行，现在 {len(text.splitlines())} 行"


def test_day_line_handles_missing_fields() -> None:
    text = day_line({"公历": "2026年11月1日 星期日"})
    assert "宜：（无）" in text and "忌：（无）" in text, "缺字段也要说清是「无」，不是空白"


def test_shape_falls_back_to_raw_text() -> None:
    tool = AlmanacTool(url="http://localhost:9/mcp")
    assert tool._shape("这不是 JSON") == "这不是 JSON", "解析不了就退回原文，不能吞掉"


def _fake_remote(records: list[dict], *, fail_days: set[str] | None = None):
    """伪造 `call_remote_tool`：记下每次调用的参数，按日期回一份真实形状的 JSON。"""
    fail_days = fail_days or set()

    async def fake(url, name, arguments, *, timeout=20.0, token=""):  # noqa: ANN001
        records.append({"url": url, "name": name, "arguments": dict(arguments)})
        day = str(arguments.get("solarDatetime", ""))[:10]
        if day in fail_days:
            raise McpUnavailable(f"{day} 那台服务器超时了")
        payload = DAY_30 if day == "2026-10-30" else DAY_31
        payload = dict(payload, 公历=payload["公历"].replace("30日", f"{day[-2:]}日"))
        return json.dumps(payload, ensure_ascii=False)

    return fake


def _run(tool: AlmanacTool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


def test_run_queries_the_whole_range_in_one_call(monkeypatch=None) -> None:
    """区间要一次查完：两天 = 两次远端调用，且日期正确。"""
    import tools.almanac_tool as module

    records: list[dict] = []
    original = module.call_remote_tool
    module.call_remote_tool = _fake_remote(records)
    try:
        result = _run(
            AlmanacTool(url="http://localhost:9/mcp"),
            start_date="2026-10-30",
            end_date="2026-10-31",
        )
    finally:
        module.call_remote_tool = original

    assert result.ok, result.error
    assert [row["arguments"]["solarDatetime"] for row in records] == [
        "2026-10-30T12:00:00+08:00",
        "2026-10-31T12:00:00+08:00",
    ], records
    assert all(row["name"] == "getChineseCalendar" for row in records)
    assert "2026年10月30日" in result.content and "2026年10月31日" in result.content
    assert "宜：祭祀" in result.content and "忌：开市" in result.content


def test_run_without_end_date_is_a_single_day() -> None:
    import tools.almanac_tool as module

    records: list[dict] = []
    original = module.call_remote_tool
    module.call_remote_tool = _fake_remote(records)
    try:
        result = _run(AlmanacTool(url="http://localhost:9/mcp"), start_date="2026-10-30")
    finally:
        module.call_remote_tool = original

    assert result.ok
    assert len(records) == 1, "只问一天就只查一天"


def test_run_rejects_dates_that_cannot_be_right() -> None:
    tool = AlmanacTool(url="http://localhost:9/mcp")

    reversed_result = _run(tool, start_date="2026-10-31", end_date="2026-10-30")
    assert not reversed_result.ok
    assert "写反" in reversed_result.error

    bad = _run(tool, start_date="10月30日")
    assert not bad.ok
    assert "YYYY-MM-DD" in bad.error

    too_long = _run(tool, start_date="2026-01-01", end_date="2026-12-31")
    assert not too_long.ok
    assert "最多查 60 天" in too_long.error, "别让写错的日期变成几百次远端调用"


def test_run_truncates_at_fourteen_days() -> None:
    import tools.almanac_tool as module

    records: list[dict] = []
    original = module.call_remote_tool
    module.call_remote_tool = _fake_remote(records)
    try:
        result = _run(
            AlmanacTool(url="http://localhost:9/mcp"),
            start_date="2026-11-01",
            end_date="2026-11-20",
        )
    finally:
        module.call_remote_tool = original

    assert result.ok
    assert len(records) == 14, len(records)
    assert "其余 6 天没列" in result.content, "截断了必须说清楚"


def test_run_survives_one_failing_day() -> None:
    import tools.almanac_tool as module

    records: list[dict] = []
    original = module.call_remote_tool
    module.call_remote_tool = _fake_remote(records, fail_days={"2026-10-31"})
    try:
        result = _run(
            AlmanacTool(url="http://localhost:9/mcp"),
            start_date="2026-10-30",
            end_date="2026-10-31",
        )
    finally:
        module.call_remote_tool = original

    assert result.ok, "一天失败不该让整次失败"
    assert "没查到：" in result.content and "2026-10-31" in result.content
    assert "2026年10月30日" in result.content


def test_run_fails_when_no_day_can_be_read() -> None:
    import tools.almanac_tool as module

    records: list[dict] = []
    original = module.call_remote_tool
    module.call_remote_tool = _fake_remote(
        records, fail_days={"2026-10-30", "2026-10-31"}
    )
    try:
        result = _run(
            AlmanacTool(url="http://localhost:9/mcp"),
            start_date="2026-10-30",
            end_date="2026-10-31",
        )
    finally:
        module.call_remote_tool = original

    assert not result.ok
    assert "都没查到" in result.error


def test_run_without_url_says_so() -> None:
    result = _run(AlmanacTool(url=""), start_date="2026-10-30")
    assert not result.ok
    assert "还没配置服务器地址" in result.error


# ------------------------------------------------- 装进工具面（替换远端的按天工具）
def _discovered(server: str, remote: str) -> DiscoveredTool:
    return DiscoveredTool(
        server=server,
        url=f"https://example.invalid/{server}/mcp",
        remote_name=remote,
        description="远端工具",
        parameters={"type": "object", "properties": {}},
    )


def test_swap_replaces_the_daily_calendar_tool() -> None:
    tools = [
        _discovered("bazi", "getChineseCalendar"),
        _discovered("bazi", "getBaziDetail"),
        _discovered("flight", "search_flights"),
    ]
    specs = [
        McpServerSpec(name="bazi", url="https://example.invalid/bazi/mcp"),
        McpServerSpec(name="flight", url="https://example.invalid/flight/mcp"),
    ]

    swapped, notes = swap_almanac_tools(tools, specs, "bazi")

    names = [tool.name for tool in swapped]
    assert names[0] == "search_almanac", "黄历工具要排在第一位，模型才容易挑到"
    assert "bazi_getChineseCalendar" not in names, "按天查的那个必须摘掉"
    assert "bazi_getBaziDetail" in names, "同服务器的其它工具照旧"
    assert "flight_search_flights" in names, "别的服务器一点都不该受影响"
    assert notes == []


def test_swap_is_off_when_not_configured() -> None:
    tools = [_discovered("bazi", "getChineseCalendar")]
    specs = [McpServerSpec(name="bazi", url="https://example.invalid/bazi/mcp")]

    swapped, notes = swap_almanac_tools(tools, specs, "")

    assert [tool.name for tool in swapped] == ["bazi_getChineseCalendar"]
    assert notes == []


def test_swap_warns_when_the_named_server_is_missing() -> None:
    swapped, notes = swap_almanac_tools([], [], "bazi")
    assert swapped == []
    assert notes and "MCP_ALMANAC_SERVER 配的是 bazi" in notes[0]
