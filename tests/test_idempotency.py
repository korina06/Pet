"""幂等键：无头不变、同 key 回放、并发 409、失败不缓存、过期重跑，以及真实 `/api/trip` 只产一份行程。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Response

from api.idempotency import IdempotencyMiddleware


def _probe_app(*, delay: float = 0.0, status: int = 200) -> tuple[FastAPI, list[int]]:
    """一个带计数器的假接口：数**真正执行了几次**。"""
    calls: list[int] = []
    app = FastAPI()

    @app.post("/api/trip")
    async def trip() -> dict:
        calls.append(1)
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            from fastapi import HTTPException

            raise HTTPException(status_code=status, detail="boom")
        return {"trip_id": f"t{len(calls)}", "n": len(calls)}

    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})
    return app, calls


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _post(client: httpx.AsyncClient, key: str | None = None) -> "asyncio.Future":
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/trip", json={}, headers=headers)


def test_without_a_key_the_endpoint_behaves_exactly_as_before():
    app, calls = _probe_app()

    async def run() -> list[dict]:
        async with _client(app) as client:
            return [(await _post(client)).json() for _ in range(2)]

    payloads = asyncio.run(run())
    assert len(calls) == 2, "没有 key 时必须每次都真执行"
    assert payloads[0]["n"] == 1 and payloads[1]["n"] == 2


def test_the_same_key_runs_the_work_only_once():
    app, calls = _probe_app()

    async def run() -> tuple[httpx.Response, httpx.Response]:
        async with _client(app) as client:
            return await _post(client, "k1"), await _post(client, "k1")

    first, second = asyncio.run(run())

    assert len(calls) == 1, f"同 key 执行了 {len(calls)} 次"
    assert first.text == second.text, "回放的响应体必须一致"
    assert first.headers.get("idempotent-replay") is None
    assert second.headers.get("idempotent-replay") == "true"


def test_different_keys_both_run():
    app, calls = _probe_app()

    async def run() -> None:
        async with _client(app) as client:
            await _post(client, "k1")
            await _post(client, "k2")

    asyncio.run(run())
    assert len(calls) == 2


def test_a_concurrent_duplicate_is_told_to_wait_instead_of_being_replayed():
    """同一个 key 的并发请求：一个干活、另一个 409 —— 不能假装成功，也不能重复干活。"""
    app, calls = _probe_app(delay=0.3)

    async def run() -> tuple[httpx.Response, httpx.Response]:
        async with _client(app) as client:
            first = asyncio.create_task(_post(client, "same"))
            await asyncio.sleep(0.05)          # 让第一个先进入处理中
            second = asyncio.create_task(_post(client, "same"))
            return await first, await second

    first, second = asyncio.run(run())

    assert first.status_code == 200
    assert second.status_code == 409, second.text
    assert second.headers.get("retry-after") == "5"
    assert len(calls) == 1, f"并发同 key 干了 {len(calls)} 次"


def test_a_failure_is_not_cached_so_the_caller_can_retry():
    app, _calls = _probe_app(status=500)

    async def run() -> tuple[httpx.Response, httpx.Response]:
        async with _client(app) as client:
            return await _post(client, "k1"), await _post(client, "k1")

    first, second = asyncio.run(run())
    assert first.status_code == 500 and second.status_code == 500
    assert second.headers.get("idempotent-replay") is None, "失败不该被缓存后回放"


def test_an_expired_entry_runs_again():
    calls: list[int] = []
    app = FastAPI()

    @app.post("/api/trip")
    async def trip() -> dict:
        calls.append(1)
        return {"n": len(calls)}

    # ttl=0：上一份立刻过期
    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"}, ttl=0.0)

    async def run() -> None:
        async with _client(app) as client:
            await _post(client, "k1")
            await asyncio.sleep(0.01)
            await _post(client, "k1")

    asyncio.run(run())
    assert len(calls) == 2, "过期之后应当重新执行"


def test_a_streaming_path_is_not_touched():
    """流式接口不在名单里：同 key 两次都照常执行（重放流是另一回事）。"""
    calls: list[int] = []
    app = FastAPI()

    @app.post("/api/chat/stream")
    async def stream() -> dict:
        calls.append(1)
        return {"ok": True}

    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})

    async def run() -> None:
        async with _client(app) as client:
            for _ in range(2):
                await client.post(
                    "/api/chat/stream", json={}, headers={"Idempotency-Key": "k1"}
                )

    asyncio.run(run())
    assert len(calls) == 2, "名单外的接口不该被拦"


def test_a_successful_trip_is_replayed_for_the_same_key():
    """接上真实路由：**成功**时同 key 两次拿到同一份行程（第 135 步）。

    注：失败态故意**不缓存**（第 145 步 / 评价9 P2），那条路径在
    `test_a_response_can_ask_not_to_be_cached` 里验。烟测里的出行是"地理编码必失败"，
    所以不能再拿它验重放 —— 这里用一个会成功的最小 app。
    """
    app = FastAPI()
    calls: list[int] = []

    @app.post("/api/trip")
    async def trip() -> dict:
        calls.append(1)
        return {"trip_id": f"t{len(calls)}", "itinerary": {"days": [1, 2]}}

    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})

    async def run() -> tuple[dict, dict]:
        async with _client(app) as client:
            first = await _post(client, "trip-1")
            second = await _post(client, "trip-1")
            return first.json(), second.json()

    first, second = asyncio.run(run())
    assert len(calls) == 1, "成功的结果应当只执行一次"
    assert first["trip_id"] == second["trip_id"], "同 key 应当拿到同一份行程"
    assert first["itinerary"] == second["itinerary"]


@pytest.mark.parametrize("key", ["", "   "])
def test_a_blank_key_is_treated_as_absent(key):
    app, calls = _probe_app()

    async def run() -> None:
        async with _client(app) as client:
            for _ in range(2):
                await client.post("/api/trip", json={}, headers={"Idempotency-Key": key})

    asyncio.run(run())
    assert len(calls) == 2, "空白 key 视为没给"


def test_a_response_can_ask_not_to_be_cached():
    """路由可以用 `Idempotency-Store: no` 说"这次别缓存"（第 145 步 / 评价9 P2）。

    真实场景：`/api/trip` 即使没生成出行程也返回 HTTP 200（status 字段才是 failed）。
    中间件只认状态码，不特别说明的话，同一个 key 重试 10 分钟都只会拿到那份失败结果。
    """
    app = FastAPI()
    calls: list[int] = []

    @app.post("/api/trip")
    async def trip(response: Response) -> dict:

        calls.append(1)
        response.headers["Idempotency-Store"] = "no"
        return {"trip_id": "none", "status": "failed", "n": len(calls)}

    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})

    async def run() -> tuple[dict, dict]:
        async with _client(app) as client:
            first = await _post(client, "bad")
            second = await _post(client, "bad")
            return first.json(), second.json()

    first, second = asyncio.run(run())
    assert len(calls) == 2, "标了 no-store 的响应不该被缓存"
    assert second["n"] == 2, "第二次应当真的重新执行"


def test_a_cancelled_request_does_not_leave_an_inflight_entry():
    """被取消的请求必须把 inflight 登记撤掉（评价9 P2 指出 `except Exception` 抓不到）。

    `asyncio.CancelledError` 继承 BaseException，所以原来那段 `except Exception`
    是抓不到它的 —— 结果同一个 key 会一直 409，直到进程重启。
    """
    app = FastAPI()
    calls: list[int] = []

    @app.post("/api/trip")
    async def trip() -> dict:
        calls.append(1)
        await asyncio.sleep(5)          # 永远等不到：留给取消
        return {"ok": True}

    app.add_middleware(IdempotencyMiddleware, paths={"/api/trip"})

    async def run() -> None:
        async with _client(app) as client:
            task = asyncio.create_task(_post(client, "c1"))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # 取消之后重试同一个 key：不该再是 409
            await asyncio.sleep(0.05)
            second = await _post(client, "c1")
            assert second.status_code != 409, "取消后留下了永不消失的 inflight"

    asyncio.run(run())


def test_an_abandoned_inflight_entry_expires():
    """inflight 也有 TTL：万一没走到清理分支，也不该永远 409。"""
    import time as real_time

    app = FastAPI()

    @app.post("/api/trip")
    async def trip() -> dict:
        return {"ok": True}

    middleware = IdempotencyMiddleware(app, paths={"/api/trip"})
    # 手动塞一条很老的 inflight 记录，模拟"清理没走到"的极端情况
    entry = type("E", (), {})()
    entry.state = "inflight"
    from api.idempotency import INFLIGHT_TTL_SECONDS

    entry.at = real_time.monotonic() - INFLIGHT_TTL_SECONDS - 5
    middleware._entries[("/api/trip", "old")] = entry

    middleware._evict_expired()
    assert ("/api/trip", "old") not in middleware._entries
