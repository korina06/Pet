"""烟测脚本自带的应用装配：用替身模型与会失败的出行工具，断言它不需要真 key、不联网。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def _client():
    import smoke_browser

    app = smoke_browser._build_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smoke"
    )


def test_the_smoke_app_serves_the_page():
    async def run() -> httpx.Response:
        async with _client() as client:
            return await client.get("/")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert 'id="pet-button"' in response.text, "首页里没有桌宠按钮，烟测第一步就会挂"


def test_the_smoke_app_streams_chat_deltas():
    """聊天流式：脚本化的假模型要真的分段吐，否则"增量渲染"那条断言没意义。"""

    async def run() -> str:
        async with _client() as client:
            response = await client.post(
                "/api/chat/stream",
                json={"session_id": "smoke", "message": "今天吃什么"},
            )
            return "".join([chunk async for chunk in response.aiter_text()])

    body = asyncio.run(run())
    assert body.count('"type": "delta"') >= 4, f"分片太少：{body[:200]}"
    assert '"type": "meta"' in body
    assert "我看看" in body


def test_the_smoke_app_returns_recipe_cards():
    """食谱链路：假模型要认出 `search_recipes`，卡片要回库取到真数据。"""

    async def run() -> dict:
        async with _client() as client:
            response = await client.post(
                "/api/recipe/ask",
                json={"session_id": "smoke", "message": "想吃鱼", "wanted": []},
            )
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    assert payload["recipes"], f"没有卡片：{payload}"
    assert any("鱼" in recipe["name"] for recipe in payload["recipes"])


def test_the_smoke_app_fails_the_trip_and_renders_no_days():
    """出行失败：地理编码失败 → status=failed，且**一天都不排**。"""

    async def run() -> dict:
        # 出行路由会拒绝过去的日期（天气只有实况、营业时间也只有当下的数据），
        # 所以这里必须用未来日期 —— 我第一次写死 2026-05-01，当场被拦。
        import smoke_browser

        start, end = smoke_browser._future_dates()
        async with _client() as client:
            response = await client.post(
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
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    # 响应结构是 {"trip_id": ..., "itinerary": {...}}（第 24 步定下的）
    trip = payload["itinerary"]
    assert trip["status"] == "failed", trip
    assert trip.get("degraded_reasons"), "失败要给得出原因，否则界面没东西可显示"
    # `days` 是**空脚手架**（只带日期，没有任何景点/餐/住宿）：
    # 失败发生在流水线第一步，日期结构早就建好了 —— 所以不能断言 days == []，
    # 真正要防的是"排了东西进去"。
    for day in trip["days"]:
        assert not day["spots"], f"失败却排了景点：{day}"
        assert not day["meals"], f"失败却排了餐厅：{day}"
        assert not day["hotels"], f"失败却排了住宿：{day}"


@pytest.mark.skipif(
    not Path(PROJECT_ROOT / "static" / "app.js").exists(), reason="缺前端资源"
)
def test_the_smoke_script_is_syntactically_valid():
    import py_compile

    py_compile.compile(str(PROJECT_ROOT / "scripts" / "smoke_browser.py"), doraise=True)

def test_the_smoke_app_serves_the_static_assets_at_the_root():
    """`/app.js` 与 `/style.css` 必须在**根路径**能取到（第 124 步 / 评价8 P0-1）。

    真实 `main.py` 是把 static 挂在根上的（`index.html` 用的是相对路径）。
    烟测里我挂到了 `/static`，于是浏览器去请求 `/app.js` 得到 404 ——
    脚本根本没加载，点桌宠没有任何反应，三个场景全挂。
    **产品没问题，是烟测的装配写错了。**
    """

    async def run() -> dict[str, int]:
        async with _client() as client:
            result = {}
            for path in ("/", "/app.js", "/style.css", "/api/health"):
                response = await client.get(path)
                result[path] = response.status_code
            return result

    codes = asyncio.run(run())
    assert codes["/app.js"] == 200, f"app.js 拿不到：{codes}"
    assert codes["/style.css"] == 200, f"style.css 拿不到：{codes}"
    assert codes["/"] == 200
    assert codes["/api/health"] == 200
