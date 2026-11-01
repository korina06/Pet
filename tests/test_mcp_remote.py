"""外部 MCP 接入：没配、连不上、远端报错时我们的行为。不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings  # noqa: E402
from tools import mcp_remote  # noqa: E402
from tools.mcp_remote import (  # noqa: E402
    McpProxyTool,
    McpUnavailable,
    RemoteTool,
    _text_of,
    transport_for,
)
from tools.registry import (  # noqa: E402
    RECIPE_TOOLS,
    TRAVEL_OPTIONAL_TOOLS,
    TRAVEL_TOOLS,
    build_default_registry,
    build_feature_registry,
)
from tools.train_tool import TrainTicketTool  # noqa: E402


# ------------------------------------------------------------------ 传输判定
def test_transport_follows_the_url_suffix():
    assert transport_for("https://mcp.api-inference.modelscope.net/abc/sse") == "sse"
    assert transport_for("https://mcp.api-inference.modelscope.net/abc/sse/") == "sse"
    assert transport_for("http://localhost:8080/mcp") == "streamable_http"
    assert transport_for("http://localhost:8080") == "streamable_http"


def test_transport_rejects_empty_and_non_http():
    for bad in ("", "   ", None):
        try:
            transport_for(bad)  # type: ignore[arg-type]
        except McpUnavailable as error:
            assert "地址" in str(error), error
        else:  # pragma: no cover - 不该走到这里
            raise AssertionError(f"空地址应当被拒绝：{bad!r}")

    try:
        transport_for("mcp.api-inference.modelscope.net/abc/sse")
    except McpUnavailable as error:
        assert "http" in str(error), error
    else:  # pragma: no cover
        raise AssertionError("缺少协议头的地址应当被拒绝")


# ------------------------------------------------- 远端结果 → 文本 / 失败语义
class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _CallResult:
    def __init__(self, blocks: list[object], *, is_error: bool = False) -> None:
        self.content = blocks
        self.isError = is_error


def test_text_of_joins_every_text_block():
    result = _CallResult([_Block("G1 次"), _Block("07:00 开")])
    assert _text_of(result) == "G1 次\n07:00 开"
    assert _text_of(_CallResult([])) == ""


def test_remote_error_turns_into_unavailable_not_exception():
    """远端 `isError` 必须变成一句人话，而不是把异常扔进 Agent 循环。"""
    original = mcp_remote.open_session

    class _Session:
        async def call_tool(self, name, arguments=None):  # noqa: ANN001, ARG002
            return _CallResult([_Block("车次查询超时")], is_error=True)

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *exc):  # noqa: ANN002
            return False

    mcp_remote.open_session = lambda *a, **k: _Ctx()  # type: ignore[assignment]
    try:
        import asyncio

        try:
            asyncio.run(
                mcp_remote.call_remote_tool(
                    "http://localhost:9/mcp", "query-tickets", {"from_station": "南京"}
                )
            )
        except McpUnavailable as error:
            assert "车次查询超时" in str(error), error
        else:  # pragma: no cover
            raise AssertionError("远端报错应当抛 McpUnavailable")
    finally:
        mcp_remote.open_session = original  # type: ignore[assignment]


# ----------------------------------------------------- 工具包装的三个关键行为
def test_proxy_tool_without_url_fails_with_actionable_message():
    tool = McpProxyTool()
    tool.name = "demo"
    tool.label = "示例"
    tool.url = ""

    import asyncio

    result = asyncio.run(tool.run(city="苏州"))
    assert result.ok is False
    assert "配置" in (result.error or ""), result.error


def test_proxy_tool_passes_remote_text_through():
    tool = McpProxyTool()
    tool.name = "demo"
    tool.url = "http://localhost:9/mcp"
    tool.remote_name = "ping"
    original = mcp_remote.call_remote_tool
    seen: dict[str, object] = {}

    async def fake(url, name, arguments, *, timeout=20.0, token=""):  # noqa: ANN001
        seen.update(url=url, name=name, arguments=arguments)
        return "pong"

    mcp_remote.call_remote_tool = fake  # type: ignore[assignment]
    try:
        import asyncio

        result = asyncio.run(tool.run(city="苏州"))
    finally:
        mcp_remote.call_remote_tool = original  # type: ignore[assignment]

    assert result.ok is True
    assert result.content == "pong"
    assert seen == {
        "url": "http://localhost:9/mcp",
        "name": "ping",
        "arguments": {"city": "苏州"},
    }, seen


def test_proxy_tool_reports_unavailable_as_failure_result():
    tool = McpProxyTool()
    tool.name = "demo"
    tool.url = "http://localhost:9/mcp"
    original = mcp_remote.call_remote_tool

    async def boom(*args, **kwargs):  # noqa: ANN002, ARG001
        raise McpUnavailable("连不上 MCP 服务器")

    mcp_remote.call_remote_tool = boom  # type: ignore[assignment]
    try:
        import asyncio

        result = asyncio.run(tool.run(city="苏州"))
    finally:
        mcp_remote.call_remote_tool = original  # type: ignore[assignment]

    assert result.ok is False
    assert "连不上" in (result.error or ""), result.error


# ------------------------------------------------------------- 12306 工具本身
def test_train_tool_schema_matches_the_remote_query_tickets():
    """名字与参数是照**真实探测结果**抄的：抄错一个字段，远端会空手而归。

    2026-10-29 用 `scripts/mcp_remote_probe.py` 探到那台服务器声明的是
    `query-tickets(from_station, to_station, train_date)`，不是 `get-tickets(origin, ...)`。
    我第一次按别处看来的名字写，真调用回的是 `{"success": false, "error": "未知工具: get-tickets"}`。
    """
    assert TrainTicketTool.remote_name == "query-tickets"
    assert TrainTicketTool.name == "search_trains"
    params = TrainTicketTool.parameters
    assert sorted(params["properties"]) == ["from_station", "to_station", "train_date"]
    assert sorted(params["required"]) == ["from_station", "to_station", "train_date"]
    # 车次只在出行窗口里当模型能力用，不进桌宠轮盘
    assert TrainTicketTool.show_in_menu is False


def test_train_tool_compacts_306_trains_into_a_readable_list():
    """真实事故的形状：306 趟 × 全字段 = 82,372 字符。压缩后必须够小、够用。"""
    import json

    trains = []
    # 半夜的 30 趟（按发车时间会排在前面，但它们不是用户要的）
    for index in range(30):
        trains.append(
            {
                "train_no": f"K{1000 + index}",
                "from_station": "南京",
                "from_station_code": "NJH",
                "to_station": "苏州",
                "to_station_code": "SZH",
                "start_time": f"0{index % 6}:{index % 60:02d}",
                "arrive_time": "11:11",
                "duration": "03:00",
                "seats": {"soft_sleeper": "无", "hard_sleeper": "有", "hard_seat": "无"},
            }
        )
    # 白天的 30 趟
    for index in range(30):
        trains.append(
            {
                "train_no": f"G{7000 + index}",
                "from_station": "南京南",
                "from_station_code": "NKH",
                "to_station": "苏州北",
                "to_station_code": "OHH",
                "start_time": f"{7 + index % 8:02d}:{index % 60:02d}",
                "arrive_time": "12:00",
                "duration": "01:10",
                "seats": {"business": "5", "first_class": "有", "second_class": "有"},
            }
        )
    payload = {
        "success": True,
        "from_station": "南京",
        "to_station": "苏州",
        "train_date": "2026-11-05",
        "count": 306,
        "trains": trains,
    }

    tool = TrainTicketTool(url="http://localhost:9/mcp")
    text = tool._shape(json.dumps(payload, ensure_ascii=False))

    assert "共 306 趟" in text, "总数必须保留，否则模型以为这就是全部"
    assert len(text) < 1600, f"压缩后还是太长：{len(text)}"
    listed = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(listed) == 15, len(listed)
    # 白天优先：第一辆应当是 G 车，而不是凌晨的 K 车
    assert listed[0].startswith("- G"), listed[0]


def test_train_tool_does_not_pass_availability_through() -> None:
    """第 220 步：用户要"先只告诉车次信息"，余票不出现在工具结果里。

    为什么放在工具层做、而不是只靠提示词：提示词是"请求"，这里是"事实"。
    余票是半分钟前的快照，一旦进了上下文，模型就有底气说「还有票」。
    """
    import json

    payload = {
        "count": 1,
        "from_station": "南京",
        "to_station": "苏州",
        "train_date": "2026-11-05",
        "trains": [
            {
                "train_no": "G7001",
                "from_station": "南京",
                "to_station": "苏州",
                "start_time": "07:00",
                "arrive_time": "08:10",
                "duration": "01:10",
                "seats": {"second_class": "有", "first_class": "无"},
            }
        ],
    }

    tool = TrainTicketTool(url="http://localhost:9/mcp")
    text = tool._shape(json.dumps(payload, ensure_ascii=False))

    assert "G7001" in text and "07:00" in text, "车次与时刻必须留着"
    assert "二等座" not in text and "一等座" not in text, "席别/余票不该出现"
    assert "有票" not in text
    assert "12306" in text, "要告诉对方余票去哪儿看"


def test_train_tool_dedupes_the_same_train_listed_for_several_stations():
    """同一车次会因途经站重复出现（实测 C3854 出现在 3 条记录里）。"""
    import json

    payload = {
        "count": 3,
        "from_station": "南京",
        "to_station": "苏州",
        "train_date": "2026-11-05",
        "trains": [
            {
                "train_no": "C3854",
                "from_station": "南京",
                "to_station": "张家港",
                "start_time": "06:00",
                "arrive_time": "08:45",
                "duration": "02:45",
                "seats": {},
            },
            {
                "train_no": "C3854",
                "from_station": "南京",
                "to_station": "常熟",
                "start_time": "06:00",
                "arrive_time": "08:57",
                "duration": "02:57",
                "seats": {},
            },
            {
                "train_no": "C3854",
                "from_station": "南京",
                "to_station": "苏州",
                "start_time": "06:00",
                "arrive_time": "09:17",
                "duration": "03:17",
                "seats": {},
            },
        ],
    }

    tool = TrainTicketTool(url="http://localhost:9/mcp")
    tool._destination = "苏州"
    text = tool._shape(json.dumps(payload, ensure_ascii=False))

    assert text.count("C3854") == 1, text
    assert "去重后 1 趟" in text, text


def test_train_tool_falls_back_to_raw_text_when_payload_is_not_json():
    """远端换了格式也不能让工具崩：退回原文（有长度上限）。"""
    tool = TrainTicketTool(url="http://localhost:9/mcp")
    tool.max_chars = 100
    text = tool._shape("这不是 JSON，" * 200)
    assert len(text) <= tool.max_chars


def test_proxy_tool_caps_runaway_remote_output():
    """通用上限：任何远端工具都不许把 4 万 token 灌进上下文。"""
    from tools.mcp_remote import _cap

    text = _cap("x" * 9000, 8000, "示例")
    assert len(text) < 8100
    assert "被截断" in text and "9000" in text


def test_train_tool_is_registered_only_when_a_url_is_configured():
    without = build_default_registry()
    assert "search_trains" not in without.names()

    with_url = build_default_registry(train_url="http://localhost:8080/mcp")
    assert "search_trains" in with_url.names()


def test_travel_registry_tolerates_the_absent_optional_tool():
    """没配 MCP 时出行窗口必须照旧成立（三件套在，车次缺席）。"""
    registry = build_default_registry()
    travel = build_feature_registry(
        registry,
        TRAVEL_TOOLS + TRAVEL_OPTIONAL_TOOLS,
        feature="出行",
        optional=TRAVEL_OPTIONAL_TOOLS,
    )
    assert set(TRAVEL_TOOLS) <= set(travel.names())
    assert "search_trains" not in travel.names()

    # 配了地址之后它要真的进来
    with_url = build_default_registry(train_url="https://example.com/sse")
    travel2 = build_feature_registry(
        with_url,
        TRAVEL_TOOLS + TRAVEL_OPTIONAL_TOOLS,
        feature="出行",
        optional=TRAVEL_OPTIONAL_TOOLS,
    )
    assert "search_trains" in travel2.names()


def test_missing_required_tool_still_raises():
    """`optional` 只放过可选项，别把"少必需工具"的守卫一起放掉了。"""
    registry = build_default_registry()
    try:
        build_feature_registry(
            registry,
            TRAVEL_TOOLS + ("not_a_tool",),
            feature="出行",
            optional=TRAVEL_OPTIONAL_TOOLS,
        )
    except ValueError as error:
        assert "not_a_tool" in str(error), error
    else:  # pragma: no cover
        raise AssertionError("缺必需工具必须报错")


# ----------------------------------------------------------------- 配置默认值
def test_mcp_settings_default_to_off():
    """默认必须是"不接"：装了别人的服务器才该有行为变化。"""
    fields = Settings.model_fields
    assert fields["mcp_12306_url"].default == ""
    assert fields["mcp_12306_timeout_seconds"].default == 20.0


def test_remote_tool_dataclass_is_frozen():
    tool = RemoteTool(name="get-tickets", description="查票", parameters={"type": "object"})
    try:
        tool.name = "changed"  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("RemoteTool 应当不可变，避免下游改坏 schema")


def test_recipe_registry_is_untouched():
    """车次是出行能力，不该顺手进到食谱窗口。"""
    registry = build_default_registry(train_url="http://localhost:8080/mcp")
    recipe = build_feature_registry(registry, RECIPE_TOOLS, feature="食谱")
    assert "search_trains" not in recipe.names()
