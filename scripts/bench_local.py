"""本地基准：出行装排、菜谱检索、Agent 循环与缓存命中的耗时。

外部调用全是替身，所以数字只说明「我们自己的代码多快」，不是用户等待时间。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.console import force_utf8  # noqa: E402

MOCK_NOTE = "数字来自替身（mock），**不含真实网络延迟**，只代表本地代码的耗时"


def _display_width(text: str) -> int:
    """按终端显示宽度算（中日韩字符占 2 列），否则表格会错位。"""
    import unicodedata

    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(text))


def _percentile(samples: list[float], ratio: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "n": len(samples),
        "p50": statistics.median(samples) if samples else 0.0,
        "p95": _percentile(samples, 0.95),
        "max": max(samples) if samples else 0.0,
    }


# --------------------------------------------------------------- 1) 出行流水线


class _CannedTool:
    """立即返回固定结果的工具替身（顺便数调用次数）。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls = 0

    async def run(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return self._result


def _bench_trip(rounds: int) -> dict[str, Any]:
    from core.schemas import ToolResult
    from core.trip_planner import TripRequest, build_itinerary

    def place(index: int, kind: str = "110200") -> dict[str, Any]:
        return {
            "name": f"{kind}-{index}",
            "location": "116.41,39.92",
            "typecode": kind,
            "distance_m": 400 + index * 100,
            "opentime": "09:00-17:00",
            "rating": "4.5",
        }

    spots = ToolResult(
        ok=True, content="景点", tool_name="find_nearby_places",
        data={"places": [place(i) for i in range(12)]},
    )
    restaurants = ToolResult(
        ok=True, content="餐厅", tool_name="find_nearby_places",
        data={"places": [place(i, "050000") for i in range(8)]},
    )
    hotels = ToolResult(
        ok=True, content="酒店", tool_name="find_nearby_places",
        data={"places": [place(i, "100000") for i in range(6)]},
    )
    geocode = ToolResult(
        ok=True, content="坐标", tool_name="geocode_city",
        data={"lat": 39.92, "lon": 116.41, "adcode": "110000", "formatted_address": "北京市"},
    )
    weather = ToolResult(
        ok=True, content="晴", tool_name="get_weather",
        data={"location": "北京", "temp": 20, "text": "晴"},
    )
    route = ToolResult(
        ok=True, content="路线", tool_name="get_route",
        data={"duration_min": 12, "distance_m": 3000},
    )

    samples: list[float] = []
    statuses: dict[str, int] = {}

    for _ in range(rounds):
        tools = [_CannedTool(spots), _CannedTool(restaurants), _CannedTool(hotels)]
        # 同一轮里"景点/餐厅/住宿"是三次不同的调用：用列表轮转分配
        queue = iter(tools)

        class _Multi:
            async def run(self, **kwargs: Any) -> Any:
                try:
                    return await next(queue).run(**kwargs)
                except StopIteration:
                    return spots

        request = TripRequest(
            city="北京", start_date="2026-05-01", end_date="2026-05-03",
            transport="自驾", stay="不住宿", preferences=["文化"],
        )
        started = time.perf_counter()
        itinerary = asyncio.run(
            build_itinerary(
                request,
                geocoder=_CannedTool(geocode),
                weather_tool=_CannedTool(weather),
                poi_tool=_Multi(),
                route_tool=_CannedTool(route),
            )
        )
        samples.append((time.perf_counter() - started) * 1000)
        statuses[itinerary.status] = statuses.get(itinerary.status, 0) + 1

    return {"scenario": "出行流水线（装排 + 校验，工具为替身）", **_summary(samples),
            "extra": f"状态分布 {statuses}"}


# --------------------------------------------------------------- 2) 菜谱检索


def _bench_recipe(rounds: int) -> dict[str, Any]:
    from core.recipe_library import ensure_schema
    from core.recipe_seed import import_seed
    from core.storage import Storage
    from tools.recipe_search_tool import RecipeSearchTool, with_enforced_constraints

    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    # 用真实的"约束代理"包一层：这正是线上走的那条路（第 55 步）
    tool = with_enforced_constraints(
        RecipeSearchTool(storage=db), ["清淡"], aversions=["香菜"]
    )
    queries = [
        {"tags": ["清淡"]},
        {"ingredients": ["鸡"]},
        {"keyword": "鱼"},
        {"tags": ["辣"], "max_minutes": 30},
        {"tags": ["川菜"], "exclude_tags": ["辣"]},
    ]
    samples: list[float] = []
    for index in range(rounds):
        arguments = queries[index % len(queries)]
        started = time.perf_counter()
        asyncio.run(tool.run(**arguments))
        samples.append((time.perf_counter() - started) * 1000)
    db.close()
    return {"scenario": "菜谱检索（96 道，真 SQLite，无网络）", **_summary(samples)}


# --------------------------------------------------------------- 3) 缓存命中


def _bench_cache(rounds: int) -> dict[str, Any]:
    """同一句话问两遍：第二次应当一次外部请求都不发。"""
    import httpx

    from core.config import Settings
    from tools.geocode_tool import GeocodeTool

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "status": "1",
                "geocodes": [{
                    "formatted_address": "北京市",
                    "location": "116.41,39.92",
                    "adcode": "110000",
                }],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = GeocodeTool(
        client=client,
        settings=Settings(gaode_api_key="test-key"),
    )

    async def one() -> float:
        started = time.perf_counter()
        await tool.run(city="北京")
        return (time.perf_counter() - started) * 1000

    async def run_all() -> list[float]:
        # 逐次计时，才能算**真的**分位数（评价8 指出：原来用"总耗时/次数"冒充 p50/p95）
        samples = []
        for _ in range(rounds):
            samples.append(await one())
        samples.append(await one())      # 同城再问一次：应当命中缓存
        return samples

    samples = asyncio.run(run_all())
    asyncio.run(client.aclose())
    requests_made = calls["count"]
    total = len(samples)
    hit_rate = (total - requests_made) / total if total else 0.0
    return {
        "scenario": f"地理编码缓存（{total} 次同城查询）",
        **_summary(samples),
        "extra": f"首次 {samples[0]:.1f}ms；外部请求 {requests_made} 次，命中率 {hit_rate:.0%}",
    }


# --------------------------------------------------------------- 4) Agent 循环


def _bench_agent(rounds: int) -> dict[str, Any]:
    from agents.agent import Agent
    from core.schemas import Message
    from tools.base import BaseTool
    from tools.registry import ToolRegistry
    from core.schemas import ToolResult

    class _Echo(BaseTool):
        name = "echo"
        description = "回声"
        show_in_menu = False
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

        async def run(self, **arguments: Any) -> ToolResult:
            return ToolResult(tool_name=self.name, ok=True, content="ok", arguments=arguments)

    class _LLM:
        """一次工具调用 + 一次回答：测"两步循环"的本地开销。"""

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools=None):
            from core.schemas import ToolCall

            self.calls += 1
            if self.calls % 2 == 1:
                return Message(
                    role="assistant", content="",
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"},
                                         raw_arguments='{"text": "x"}')],
                )
            return Message(role="assistant", content="好了")

    registry = ToolRegistry()
    registry.register(_Echo())
    samples: list[float] = []
    for _ in range(rounds):
        agent = Agent(llm=_LLM(), registry=registry, max_steps=3)
        started = time.perf_counter()
        asyncio.run(agent.run([Message(role="user", content="hi")]))
        samples.append((time.perf_counter() - started) * 1000)
    return {"scenario": "Agent 循环（1 次工具 + 1 次回答，模型为替身）", **_summary(samples)}


# --------------------------------------------------------------- 报告


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="本地基准（不含网络）")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--quick", action="store_true", help="5 次，给 CI 用")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    rounds = 5 if args.quick else max(1, args.rounds)
    rows = [
        _bench_trip(rounds),
        _bench_recipe(rounds),
        _bench_cache(rounds),
        _bench_agent(rounds),
    ]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("=" * 86)
    print(f"本地基准（每项 {rounds} 次）")
    print(f"⚠️  {MOCK_NOTE}")
    print("=" * 86)
    print(f"  {_pad('场景', 40)}{'p50':>9}{'p95':>9}{'max':>9}  备注")
    print("-" * 86)
    for row in rows:
        print(
            f"  {_pad(row['scenario'], 40)}{row['p50']:>7.1f}ms{row['p95']:>7.1f}ms"
            f"{row['max']:>7.1f}ms  {row.get('extra', '')}"
        )
    print("-" * 86)
    print(f"  样本数 {rounds}；用 python scripts/bench_local.py --rounds 100 可加长")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
