"""报错要带当前提供方的名字：切到 GLM 之后，401、网络错误、超时与解析失败都指向 GLM 与它的 key。
"""

from __future__ import annotations

import asyncio

import httpx

from agents import deepseek_llm as module
from agents.deepseek_llm import DeepSeekLLM
from agents.providers import Provider
from core.config import Settings
from core.schemas import Message

GLM = Provider(
    name="glm",
    label="GLM（智谱）",
    base_url="https://open.bigmodel.cn/api/paas/v4",
    model="glm-4-flash",
    api_key="test-glm-key",
)


def _llm(provider=None, *, client=None) -> DeepSeekLLM:
    llm = DeepSeekLLM(settings=Settings(deepseek_api_key="k"), provider=provider)
    if client is not None:
        llm._client = client
    return llm


def _call_and_catch(llm: DeepSeekLLM) -> str:
    original = module.RETRY_BACKOFF_SECONDS
    module.RETRY_BACKOFF_SECONDS = 0.0
    try:
        asyncio.run(llm.complete([Message(role="user", content="你好")]))
    except Exception as exc:      # noqa: BLE001 —— 就是要抓住它看文案
        return str(exc)
    finally:
        module.RETRY_BACKOFF_SECONDS = original
    raise AssertionError("这次调用应该失败")


class _Status:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.attempts = 0
        self._status = status
        self.is_success = status < 400
        self.status_code = status
        self._body = body or {"error": {"message": "boom"}}

    async def post(self, *_args, **_kwargs):
        self.attempts += 1
        return self

    def json(self):
        return self._body

    @property
    def text(self) -> str:
        return str(self._body)

    async def aclose(self) -> None:
        return None


class _Offline:
    async def post(self, *_args, **_kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    async def aclose(self) -> None:
        return None


def test_a_glm_401_says_glm_and_points_at_the_right_env_var():
    message = _call_and_catch(_llm(GLM, client=_Status(401)))

    assert "GLM" in message, message
    assert "GLM_API_KEY" in message, message
    assert "DEEPSEEK_API_KEY" not in message, f"指向了错的变量名：{message}"
    assert "DeepSeek" not in message, f"切到 GLM 却报 DeepSeek：{message}"


def test_a_glm_network_error_says_glm():
    message = _call_and_catch(_llm(GLM, client=_Offline()))

    assert "GLM" in message, message
    assert "DeepSeek" not in message, message


def test_a_glm_rate_limit_says_glm():
    message = _call_and_catch(_llm(GLM, client=_Status(429)))

    assert "GLM" in message, message
    assert "429" in message, message


def test_the_default_provider_still_reports_deepseek():
    """不传 provider 时行为不能漂移：老用户看到的仍是 DeepSeek。"""
    message = _call_and_catch(_llm(None, client=_Status(401)))

    assert "DeepSeek" in message, message
    assert "DEEPSEEK_API_KEY" in message, message


def test_the_env_key_name_follows_the_provider():
    assert _llm(GLM).env_key_name == "GLM_API_KEY"
    assert _llm(None).env_key_name == "DEEPSEEK_API_KEY"
    assert _llm(GLM).label == "GLM（智谱）"


def test_the_probe_route_surfaces_the_switched_provider_name():
    """"测一下"把错误原样交给界面 —— 那条文案也必须是对的。"""
    from fastapi import FastAPI

    from agents.model_router import ModelRouter
    from api.model_routes import router as model_router

    app = FastAPI()
    app.include_router(model_router)
    router = ModelRouter(
        Settings(deepseek_api_key="k", glm_api_key="g"), client=_Offline(), storage=None
    )
    router.switch("glm")
    app.state.model_router = router

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post("/api/model/probe")
            assert response.status_code == 200
            return response.json()

    payload = asyncio.run(run())
    assert payload["ok"] is False
    assert payload["provider"] == "glm"
    assert "GLM" in payload["message"], payload["message"]
    assert "DeepSeek" not in payload["message"], payload["message"]
