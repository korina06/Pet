"""本地负载测试：并发打 `/api/chat/stream`，输出吞吐、延迟分布与事件循环 lag。

外部调用全被替身挡掉，所以测的是本机这套服务能扛多少，不是模型能扛多少。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402

from core.console import force_utf8  # noqa: E402

NOTE = "客户端与服务都在本机；模型与外部 API 都是替身 —— 这些数字量的是我们的代码，不是网络"


def _percentile(samples: list[float], ratio: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


class _LoopLagMonitor:
    """后台量"事件循环被占住多久"：每次期望睡 10ms，实际睡了多久。"""

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while not self._stop:
            started = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.samples.append((time.perf_counter() - started - self.interval) * 1000)

    async def __aenter__(self) -> "_LoopLagMonitor":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc) -> bool:
        self._stop = True
        if self._task is not None:
            await self._task
        return False

    def summary(self) -> dict[str, float]:
        return {
            "p50": _percentile(self.samples, 0.5),
            "p95": _percentile(self.samples, 0.95),
            "max": max(self.samples) if self.samples else 0.0,
        }


async def _one_round(
    client: httpx.AsyncClient, scenario: dict[str, Any], count: int
) -> tuple[list[float], int]:
    """发 `count` 个请求（并发度为 scenario['concurrency']），返回延迟与失败数。"""

    latencies: list[float] = []
    failures = 0
    semaphore = asyncio.Semaphore(int(scenario["concurrency"]))

    async def one(index: int) -> None:
        nonlocal failures
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await scenario["call"](client, index)
                if response.status_code >= 400:
                    failures += 1
            except Exception:          # noqa: BLE001 —— 压测里任何异常都算一次失败
                failures += 1
            finally:
                latencies.append((time.perf_counter() - started) * 1000)

    await asyncio.gather(*(one(index) for index in range(count)))
    return latencies, failures


def _scenarios() -> list[dict[str, Any]]:
    async def read_tools(client: httpx.AsyncClient, _index: int):
        return await client.get("/api/tools")

    async def read_session(client: httpx.AsyncClient, index: int):
        return await client.get("/api/session", params={"session_id": f"load-{index}"})

    async def chat_stream(client: httpx.AsyncClient, index: int):
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"session_id": f"load-chat-{index}", "message": f"负载测试 {index}"},
        ) as response:
            async for _chunk in response.aiter_text():
                pass
            return response

    async def trip(client: httpx.AsyncClient, _index: int):
        from smoke_browser import _future_dates

        start, end = _future_dates()
        return await client.post(
            "/api/trip",
            json={
                "city": "北京",
                "start_date": start,
                "end_date": end,
                "transport": "公共交通",
                "stay": "不住宿",
                "preferences": [],
            },
        )

    return [
        {"id": "tools", "name": "读接口（/api/tools）", "call": read_tools},
        {"id": "chat", "name": "聊天 SSE（流式全读完）", "call": chat_stream},
        {"id": "trip", "name": "出行流水线（替身工具）", "call": trip},
    ]


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="本地负载测试")
    parser.add_argument("--concurrency", default="1,4,8,16")
    parser.add_argument("--requests", type=int, default=40, help="每个并发档发多少个请求")
    parser.add_argument("--quick", action="store_true", help="CI 用：2 档、每档 4 个请求")
    parser.add_argument(
        "--heavy",
        action="store_true",
        help="重压档：8/50/200 并发，且每档请求数至少铺满并发（第 206 步，评价16 第③件）",
    )
    parser.add_argument(
        "--scenarios",
        default="",
        help="只跑指定的情景，逗号分隔，按 id 或名字里的词匹配（tools/chat/trip）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.quick:
        levels = [2, 4]
        per_level = 4
    elif args.heavy:
        levels = [8, 50, 200]
        per_level = max(1, args.requests)
    else:
        levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
        per_level = max(1, args.requests)

    scenarios = _scenarios()
    wanted = [word.strip() for word in args.scenarios.split(",") if word.strip()]
    if wanted:
        scenarios = [
            scenario
            for scenario in scenarios
            if any(word in scenario["id"] or word in scenario["name"] for word in wanted)
        ]
        if not scenarios:
            parser.error(f"--scenarios 一个都没匹配上：{args.scenarios}")

    import uvicorn

    from smoke_browser import _build_app, _free_port, _wait_http

    port = _free_port()
    app = _build_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    rows: list[dict[str, Any]] = []
    try:
        _wait_http(f"{base}/api/health")

        async def run_all() -> None:
            async with httpx.AsyncClient(base_url=base, timeout=60) as client:
                for scenario in scenarios:
                    for level in levels:
                        case = {**scenario, "concurrency": level}
                        # 重压档：请求数不够铺满并发就是在量"队列"而不是"并发"，
                        # 所以每档至少发 level 个请求（第 206 步）。
                        count = max(per_level, level) if args.heavy else per_level
                        async with _LoopLagMonitor() as monitor:
                            started = time.perf_counter()
                            latencies, failures = await _one_round(client, case, count)
                            elapsed = time.perf_counter() - started
                        rows.append(
                            {
                                "scenario": scenario["name"],
                                "concurrency": level,
                                "requests": count,
                                "failures": failures,
                                "rps": count / elapsed if elapsed else 0.0,
                                "p50": _percentile(latencies, 0.5),
                                "p95": _percentile(latencies, 0.95),
                                "max": max(latencies) if latencies else 0.0,
                                "loop_lag": monitor.summary(),
                            }
                        )

        asyncio.run(run_all())
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("=" * 92)
    print("本地负载测试")
    print(f"⚠️  {NOTE}")
    print("=" * 92)
    print(
        f"  {'场景':<30}{'并发':>5}{'请求':>5}{'失败':>5}"
        f"{'RPS':>7}{'p50':>9}{'p95':>9}{'loop lag p95':>13}"
    )
    print("-" * 92)
    for row in rows:
        print(
            f"  {row['scenario']:<30}{row['concurrency']:>5}{row['requests']:>5}"
            f"{row['failures']:>5}{row['rps']:>7.1f}"
            f"{row['p50']:>8.1f}ms{row['p95']:>8.1f}ms"
            f"{row['loop_lag']['p95']:>12.1f}ms"
        )
    print("-" * 92)

    worst = max(rows, key=lambda row: row["loop_lag"]["p95"], default=None)
    if worst:
        print(
            f"  最大事件循环延迟：{worst['loop_lag']['p95']:.1f}ms（{worst['scenario']}，"
            f"并发 {worst['concurrency']}）"
        )
        if worst["loop_lag"]["p95"] < 50:
            print("  → 没有证据表明同步 SQLite 拖住了事件循环；不必改成 asyncio.to_thread。")
        else:
            print("  → 值得考虑把阻塞调用挪出事件循环（asyncio.to_thread）。")
    failures = sum(row["failures"] for row in rows)
    print(f"  失败请求总计：{failures}")
    print("=" * 92)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
