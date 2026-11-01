"""出行对话：提示词里「名字只能来自工具返回」这条约束、事后核对、两个端点与失败路径。

模型答得好不好要靠真跑和人工看，这里只测能确定的部分。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from agents.agent import AgentReply, build_travel_prompt  # noqa: E402
from api.trip_chat_routes import router  # noqa: E402
from core.schemas import Message, ToolResult  # noqa: E402
from core.trip_verify import allowed_names, suspect_places  # noqa: E402
from tests.fakes import (  # noqa: E402
    BoomTool,
    FailingLLM,
    FakeLLM,
    assistant_text,
    assistant_tool_call,
)
from tools.base import BaseTool  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402

PLACES = [
    {"name": "苏州博物馆（本馆）", "address": "东北街 204 号", "distance_m": 380},
    {"name": "拙政园", "address": "东北街 178 号", "distance_m": 520},
]


class FakePoiTool(BaseTool):
    """返回两个真实景点的假 POI 工具。"""

    name = "find_nearby_places"
    description = "找周边地点，仅用于测试"
    parameters = {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(
            ok=True,
            content="找到 2 个地点",
            tool_name=self.name,
            arguments=kwargs,
            data={"places": PLACES},
        )


class FakeStore:
    """内存版 SessionStore：这层只需要 get_history / append 两个方法。"""

    def __init__(self) -> None:
        self.history: dict[str, list[Message]] = {}

    async def get_history(self, session_id: str) -> list[Message]:
        return list(self.history.get(session_id, []))

    async def append(self, session_id: str, message: Message) -> None:
        self.history.setdefault(session_id, []).append(message)


def build_app(*, llm: Any = None, tools: dict[str, Any] | None = None) -> FastAPI:
    registry = ToolRegistry()
    for tool in (tools or {"find_nearby_places": FakePoiTool()}).values():
        registry.register(tool)

    app = FastAPI()
    app.include_router(router)
    app.state.llm = llm
    app.state.travel_registry = registry
    app.state.session_store = FakeStore()
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


# ------------------------------------------------------------ 预算与配置


def test_trip_chat_has_its_own_token_budget(monkeypatch: Any) -> None:
    """第 256 步：出行一轮真实要 4~6 次调用，用聊天的 20K 预算会半路停下。

    实测（2026-10-31，上海→北京）：4 次调用合计 **25,952 token**，
    而 `TURN_TOKEN_BUDGET=20000` 在第 5 步之前就把这一轮掐了。
    所以出行要有自己的预算，默认值必须高到够走完 6 步。
    """
    from core.config import Settings

    assert Settings(_env_file=None).trip_chat_turn_token_budget == 60000


async def test_trip_chat_passes_its_budget_to_the_agent(monkeypatch: Any) -> None:
    from api import trip_chat_routes
    from core.config import Settings

    seen: dict[str, Any] = {}

    class CaptureAgent:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def run(self, messages: list[Any]) -> AgentReply:
            return AgentReply(content="好")

    monkeypatch.setattr(trip_chat_routes, "Agent", CaptureAgent)
    monkeypatch.setattr(
        trip_chat_routes,
        "get_settings",
        lambda: Settings(_env_file=None, trip_chat_turn_token_budget=54321),
    )

    app = build_app(llm=object())
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "上海去北京", "session_id": "s"}
        )

    assert response.status_code == 200, response.text
    assert seen["turn_token_budget"] == 54321
    assert seen["max_steps"] == 6


# ------------------------------------------------------------ 提示词与核对


def test_prompt_keeps_the_no_invention_rule() -> None:
    prompt = build_travel_prompt(["find_nearby_places", "search_trains"])
    assert "名字只能来自工具返回" in prompt
    assert "不写票价" in prompt
    assert "一次问完" in prompt, "缺信息要一次问全，不要来回挤牙膏"
    assert "find_nearby_places、search_trains" in prompt, "工具清单要按真实工具面拼"


def test_prompt_takes_the_origin_only_from_what_was_said() -> None:
    """第 220 步：出发地只从对方的话里取，没说就客气问一句，但别停下其余工作。"""
    prompt = build_travel_prompt(["search_trains"])
    assert "出发地只从对方的话里取" in prompt
    assert "不要替他假定一个城市" in prompt, "不能拿目的地反推出发地"
    assert "从哪个城市出发" in prompt, "缺出发地时要有一句现成的问法"
    assert "继续把能做的先做" in prompt, "不能因为少一个信息就整轮停住"


def test_prompt_never_reports_availability() -> None:
    """第 220 步：用户明确说余票先不查，只报车次信息 —— 提示词里要写死。"""
    prompt = build_travel_prompt(["search_trains"])
    assert "不报余票、不报票价" in prompt
    assert "余票随时会变" in prompt, "要说明为什么不让它报余票"


def test_prompt_drops_the_train_rule_when_the_tool_is_not_registered() -> None:
    """第 233 步：12306 没配时 search_trains 根本不注册，提示词不能再教它调。"""
    prompt = build_travel_prompt(["geocode_city", "find_nearby_places"])

    assert "search_trains" not in prompt, "不能教模型调一个不存在的工具"
    assert "不报余票" not in prompt, "余票那段要跟着车次工具一起走"
    assert "12306" not in prompt
    # 编号按存在性重排，不留 1、2、4、5 的断号
    expected = [
        "有城市名",
        "景点、餐馆",
        "名字只能来自工具返回",
        "get_weather 不传窗口参数是实况",
    ]
    for i, head in enumerate(expected, start=1):
        assert f"{i}. {head}" in prompt, f"第 {i} 条应当是「{head}」"
    assert "5. " not in prompt, "只有四条规则，不该出现第 5 条"


def test_prompt_uses_end_date_not_trip_length_for_forecasts() -> None:
    """第 236 步：和风的 days 从今天起算，提示词必须教模型传 end_date。"""
    prompt = build_travel_prompt(["geocode_city", "get_weather"])

    assert "end_date" in prompt
    assert "不要用 days 传行程天数" in prompt, "这条错位必须写死在提示词里"
    assert "从今天起算" in prompt, "要说清 days 的口径，否则模型还会误用"
    assert "预报不是观测" in prompt


def test_prompt_only_explains_the_almanac_when_it_exists() -> None:
    """第 221 步：黄历是可选工具，没接就不该在提示词里出现这一段。"""
    with_almanac = build_travel_prompt(["geocode_city", "search_almanac"])
    without = build_travel_prompt(["geocode_city"])

    assert "【黄历怎么用】" in with_almanac
    assert "start_date 传第一天" in with_almanac, "行程要一次查完区间"
    assert "只传 start_date" in with_almanac, "只问一天就只传一天"
    assert "不打吉凶分" in with_almanac, "远端只给宜忌，不许自己评吉凶"
    assert "【黄历怎么用】" not in without


def test_route_comparison_section_appears_only_with_the_route_tool() -> None:
    """第 223 步：四种方式都要能比，且不许拿一种的数替另一种。"""
    with_route = build_travel_prompt(["geocode_city", "driving_route"])
    without = build_travel_prompt(["geocode_city"])

    assert "【怎么比交通" in with_route
    assert "驾车、公交、骑行、步行都能查" in with_route
    assert "city_adcode" in with_route, "公交必须说明要给目的城市编码"
    assert "不要用出发地的编码" in with_route
    assert "过路费 0 元" in with_route, "要明确禁止把 0 元过路费写出来"
    assert "【怎么比交通" not in without


def test_allowed_names_skips_failed_calls() -> None:
    ok = ToolResult(
        ok=True, content="找到", tool_name="find_nearby_places", data={"places": PLACES}
    )
    failed = ToolResult(
        ok=False,
        error="超时",
        tool_name="find_nearby_places",
        data={"places": [{"name": "编造山庄"}]},
    )
    names = allowed_names([ok, failed])
    assert "苏州博物馆（本馆）" in names
    assert "编造山庄" not in names, "失败调用里的内容不能当白名单，否则会放过编造"


def test_suspect_places_flags_invented_name() -> None:
    text = "第一天：苏州博物馆 → 拙政园 → 寒山寺看夜景。"
    suspects = suspect_places(text, {"苏州博物馆（本馆）", "拙政园"})
    assert suspects == ["寒山寺"], suspects


def test_suspect_places_accepts_short_form_of_allowed_name() -> None:
    text = "先去苏州博物馆，再去拙政园。"
    # 工具返回的是「苏州博物馆（本馆）」，回答写简称是合理的，不该被判为编造
    assert suspect_places(text, {"苏州博物馆（本馆）", "拙政园"}) == []


def test_suspect_places_without_any_tool_result_says_nothing() -> None:
    """一条成功结果都没有时，列名字没有意义 —— 该说的是「没查到」。"""
    assert suspect_places("虎丘塔、寒山寺", set()) == []


# ---------------------------------------------------------------- 端点


def _tool_then_answer() -> FakeLLM:
    return FakeLLM(
        [
            assistant_tool_call("c1", "find_nearby_places", '{"keyword": "博物馆"}'),
            assistant_text("第一天：苏州博物馆 → 拙政园 → 虎丘塔。"),
        ]
    )


async def test_trip_chat_returns_answer_and_suspects() -> None:
    app = build_app(llm=_tool_then_answer())
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "苏州两天怎么玩", "session_id": "s1"}
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "苏州博物馆" in payload["content"]
    assert payload["tools_used"] == ["find_nearby_places"]
    assert payload["suspects"] == ["虎丘塔"], "没被工具返回过的地名要单独列出来"


async def test_trip_chat_persists_both_turns() -> None:
    app = build_app(llm=FakeLLM([assistant_text("好的。")]))
    async with make_client(app) as client:
        await client.post(
            "/api/trip/chat", json={"message": "我想去苏州", "session_id": "s2"}
        )
    stored = app.state.session_store.history["trip:s2"]
    assert [message.role for message in stored] == ["user", "assistant"]
    assert stored[0].content == "我想去苏州", "落库的是用户原话，不带【今天】前缀"
    assert "【今天】" not in stored[0].content


async def test_trip_chat_uses_todays_date_in_the_model_copy() -> None:
    llm = FakeLLM([assistant_text("好。")])
    app = build_app(llm=llm)
    async with make_client(app) as client:
        await client.post("/api/trip/chat", json={"message": "下周去哪", "session_id": "s3"})
    sent = llm.calls[0][-1].content
    assert sent.startswith("【今天】"), sent
    assert sent.endswith("下周去哪")


async def test_trip_chat_reports_tool_failure_without_crashing() -> None:
    llm = FakeLLM(
        [
            assistant_tool_call("c1", "boom", '{"reason": "网络"}'),
            assistant_text("这次没查到，换个说法再试一次。"),
        ]
    )
    app = build_app(llm=llm, tools={"boom": BoomTool()})
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "查一下", "session_id": "s4"}
        )
    assert response.status_code == 200
    assert "没查到" in response.json()["content"]
    # 失败的调用不进 tools_used 里的"成功"统计，但仍会出现在真实调用序列里
    assert response.json()["tools_used"] == ["boom"]


async def test_trip_chat_model_outage_still_answers_with_words() -> None:
    app = build_app(llm=FailingLLM("模拟的模型故障"))
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "帮我规划", "session_id": "s5"}
        )
    assert response.status_code == 200, "模型挂了不该让桌宠弹错误框"
    assert response.json()["content"], "至少要说一句话"
    assert response.json()["suspects"] == []


async def test_trip_chat_uninitialised_returns_503() -> None:
    app = FastAPI()
    app.include_router(router)
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "你好", "session_id": "s6"}
        )
    assert response.status_code == 503


# ------------------------------------------------- 路线图（第 227 步）


class FakeRouteTool(BaseTool):
    """假的车次/路线工具：查成功就回一句人话 + data，查失败就 ok=False。"""

    name = "driving_route"
    description = "算两个地方之间的路线，仅用于测试"
    parameters = {
        "type": "object",
        "properties": {
            "origin_lat": {"type": "number"},
            "origin_lon": {"type": "number"},
            "dest_lat": {"type": "number"},
            "dest_lon": {"type": "number"},
            "mode": {"type": "string"},
        },
        "required": ["origin_lat", "origin_lon"],
    }

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        if not self.ok:
            return ToolResult(ok=False, error="高德没响应", tool_name=self.name, arguments=kwargs)
        return ToolResult(
            ok=True,
            content="驾车约 144.0 公里，约 102 分钟，过路费约 52 元",
            tool_name=self.name,
            arguments=kwargs,
            data={"mode": kwargs.get("mode", "driving"), "distance_m": 144000},
        )


def _route_call(mode: str = "driving") -> FakeLLM:
    return FakeLLM(
        [
            assistant_tool_call(
                "c1",
                "driving_route",
                '{"origin_lat": 32.06, "origin_lon": 118.79, "dest_lat": 31.23, '
                '"dest_lon": 121.47, "mode": "%s"}' % mode,
            ),
            assistant_text("从南京到上海大概 140 公里。"),
        ]
    )


async def test_trip_chat_returns_a_map_url_for_a_checked_route() -> None:
    app = build_app(llm=_route_call(), tools={"driving_route": FakeRouteTool()})
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "南京到上海开车多远", "session_id": "m1"}
        )
    assert response.status_code == 200, response.text
    routes = response.json()["routes"]
    assert len(routes) == 1, routes
    route = routes[0]
    assert route["mode"] == "driving"
    assert route["url"].startswith("/api/map/route.png?")
    # 坐标必须是**我们传下去的**那两个点（别把经纬度顺序搞反）
    assert "118.790000%2C32.060000" in route["url"], route["url"]
    assert "121.470000%2C31.230000" in route["url"], route["url"]
    assert "驾车约 144.0 公里" in route["text"]


async def test_trip_chat_gives_no_map_when_the_route_call_failed() -> None:
    app = build_app(llm=_route_call(), tools={"driving_route": FakeRouteTool(ok=False)})
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "南京到上海开车多远", "session_id": "m2"}
        )
    assert response.status_code == 200
    assert response.json()["routes"] == [], "查失败还画图等于编一条路出来"


async def test_trip_chat_skips_transit_maps() -> None:
    app = build_app(llm=_route_call("transit"), tools={"driving_route": FakeRouteTool()})
    async with make_client(app) as client:
        response = await client.post(
            "/api/trip/chat", json={"message": "坐高铁去上海", "session_id": "m3"}
        )
    assert response.status_code == 200
    assert response.json()["routes"] == [], "公交线是另一套数据，画出来会和实际换乘不一致"


def test_route_images_need_both_coordinates() -> None:
    from api.trip_chat_routes import _route_images

    only_one = ToolResult(
        ok=True,
        content="驾车约 10 公里",
        tool_name="driving_route",
        arguments={"origin_lat": 32.06, "origin_lon": 118.79},
    )
    assert _route_images([only_one]) == [], "只有一个点就画不出路线，宁可不画"

    out_of_range = ToolResult(
        ok=True,
        content="驾车约 10 公里",
        tool_name="driving_route",
        arguments={
            "origin_lat": 200,
            "origin_lon": 118.0,
            "dest_lat": 31.0,
            "dest_lon": 121.0,
        },
    )
    assert _route_images([out_of_range]) == [], "越界坐标不是坐标"


def test_route_images_dedupe_and_cap() -> None:
    from api.trip_chat_routes import MAX_ROUTE_IMAGES, _route_images

    def result(lat: float) -> ToolResult:
        return ToolResult(
            ok=True,
            content="驾车",
            tool_name="driving_route",
            arguments={
                "origin_lat": lat,
                "origin_lon": 118.0,
                "dest_lat": 31.0,
                "dest_lon": 121.0,
                "mode": "driving",
            },
        )

    same = [result(32.0), result(32.0)]
    assert len(_route_images(same)) == 1, "同一条路线问两遍只画一张"

    many = [result(30.0 + index) for index in range(6)]
    assert len(_route_images(many)) == MAX_ROUTE_IMAGES, "一轮的图要有上限，否则刷屏"
