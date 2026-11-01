"""`POST /api/recipe/photo` 的接口：与主链路隔离（不读 registry、不读 wanted、不写会话）。

全部用假识别器或空 key，不联网、不花额度。
"""

from __future__ import annotations

import base64

import httpx
from fastapi import FastAPI

from api.recipe_photo_routes import router
from core.recipe_photo import PhotoOutcome


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def build_app(recognizer=None, *, with_settings: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if recognizer is not None:
        app.state.photo_recognizer = recognizer
    if with_settings:
        from core.config import Settings

        app.state.settings = Settings(serpapi_api_key="")
        app.state.http_client = httpx.AsyncClient()
        app.state.llm = None
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class StubRecognizer:
    def __init__(self, outcome: PhotoOutcome):
        self.outcome = outcome
        self.seen: list[tuple[bytes, str]] = []

    async def run(self, data: bytes, fmt: str) -> PhotoOutcome:
        self.seen.append((data, fmt))
        return self.outcome


async def test_bad_base64_is_a_readable_400():
    async with make_client(build_app(StubRecognizer(PhotoOutcome(status="ok")))) as client:
        response = await client.post(
            "/api/recipe/photo", json={"image_base64": "这不是 base64 !!"}
        )
    assert response.status_code == 400
    assert "base64" in response.json()["detail"]


async def test_non_image_bytes_are_refused_before_any_network_call():
    stub = StubRecognizer(PhotoOutcome(status="ok", reply="不该走到这里"))
    payload = base64.b64encode(b"<!DOCTYPE html>").decode()
    async with make_client(build_app(stub)) as client:
        response = await client.post(
            "/api/recipe/photo", json={"image_base64": payload}
        )
    assert response.status_code == 400
    assert "不是图片" in response.json()["detail"]
    assert stub.seen == [], "校验没过就不该调用识别器（不花额度）"


async def test_ok_result_is_returned_with_dish_and_sources():
    stub = StubRecognizer(
        PhotoOutcome(
            status="ok",
            reply="这是麻婆豆腐。做法：豆腐焯水后烧入味。参考：https://example.com/1",
            dish="麻婆豆腐",
            alternatives=["家常麻婆豆腐"],
            sources=["https://example.com/1"],
        )
    )
    payload = base64.b64encode(PNG_BYTES).decode()
    async with make_client(build_app(stub)) as client:
        response = await client.post(
            "/api/recipe/photo",
            json={"image_base64": f"data:image/png;base64,{payload}", "note": "午饭"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["dish"] == "麻婆豆腐"
    assert body["sources"] == ["https://example.com/1"]
    assert body["alternatives"] == ["家常麻婆豆腐"]
    assert stub.seen and stub.seen[0][1] == "png", "前缀要剥掉，格式要认对"


async def test_failures_still_return_200_with_a_message():
    """失败也走 200：前端只需要把 reply 显示成气泡，不用分辨状态码。"""
    stub = StubRecognizer(
        PhotoOutcome(status="timeout", reply="识图服务超时了，这张图没认出来。")
    )
    payload = base64.b64encode(PNG_BYTES).decode()
    async with make_client(build_app(stub)) as client:
        response = await client.post("/api/recipe/photo", json={"image_base64": payload})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "timeout"
    assert "超时" in body["reply"]


async def test_missing_key_is_reported_without_touching_the_network():
    """没配 SERPAPI_API_KEY：直接回"暂时用不了"，不发任何请求。"""
    payload = base64.b64encode(PNG_BYTES).decode()
    app = build_app(None, with_settings=True)
    async with make_client(app) as client:
        response = await client.post("/api/recipe/photo", json={"image_base64": payload})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_key"
    assert "暂时用不了" in body["reply"]


async def test_uninitialized_state_is_503():
    app = FastAPI()
    app.include_router(router)
    payload = base64.b64encode(PNG_BYTES).decode()
    async with make_client(app) as client:
        response = await client.post("/api/recipe/photo", json={"image_base64": payload})
    assert response.status_code == 503
