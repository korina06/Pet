"""模型切换：切换真的换了一家，不可用的那家切不过去。
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from agents.model_router import ModelRouter
from agents.providers import (
    DEFAULT_PROVIDER,
    Provider,
    find_provider,
    providers_from_settings,
)
from api.model_routes import router as model_router
from core import app_settings
from core.config import Settings
from core.recipe_library import ensure_schema
from core.storage import Storage


def _settings(**overrides: str) -> Settings:
    base = {
        "deepseek_api_key": "dk",
        "glm_api_key": "gk",
        "mimo_api_key": "mk",
    }
    base.update(overrides)
    return Settings(**base)


def _db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    return db


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _model_app(router: ModelRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(model_router)
    app.state.model_router = router
    return app


# ------------------------------------------------------------- 提供方目录


def test_profile_lists_three_providers_in_a_fixed_order() -> None:
    providers = providers_from_settings(_settings())

    assert [p.name for p in providers] == ["deepseek", "glm", "mimo"]
    assert DEFAULT_PROVIDER == "deepseek", "默认必须仍是 DeepSeek"


def test_a_provider_without_base_url_is_listed_but_unavailable() -> None:
    """MiMo 只有 key：**列出但不可选**，并说明缺什么。

    这比"能点、点了报错"诚实；也比"干脆不显示"有用 ——
    以后在 `.env` 填上地址与模型名就能用，不用改代码。
    """
    mimo = find_provider(providers_from_settings(_settings()), "mimo")
    assert mimo is not None
    assert mimo.available is False
    assert "接口地址" in mimo.reason and "模型名" in mimo.reason


def test_glm_is_available_and_points_at_the_openai_compatible_path() -> None:
    """GLM 的地址要落在 `/chat/completions`（v4 接口是 OpenAI 兼容的）。"""
    router = ModelRouter(_settings(), storage=_db())
    llm = router.llm_for("glm")

    assert llm.endpoint == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert llm._model == "glm-4-flash"


def test_public_view_never_leaks_the_key() -> None:
    """给界面的视图里**绝不允许**出现 key —— 这是最容易漏的一处。"""
    provider = Provider(
        name="glm", label="GLM", base_url="https://x", model="m", api_key="SECRET-KEY"
    )

    view = provider.public()

    assert "SECRET-KEY" not in str(view)
    assert set(view) == {"name", "label", "model", "note", "available", "reason"}


# --------------------------------------------------------------- 路由器


def test_switch_changes_the_active_provider_and_remembers_it() -> None:
    db = _db()
    router = ModelRouter(_settings(), storage=db)

    assert router.active == "deepseek"
    router.switch("glm")
    assert router.active == "glm"

    # 新实例（= 重启）应当读回同一个选择
    again = ModelRouter(_settings(), storage=db)
    assert again.active == "glm"
    assert app_settings.get_setting(db, app_settings.KEY_ACTIVE_PROVIDER) == "glm"


def test_switching_to_an_unavailable_provider_is_refused_with_a_reason() -> None:
    router = ModelRouter(_settings(), storage=_db())

    try:
        router.switch("mimo")
    except ValueError as exc:
        assert "MiMo" in str(exc)
        assert "配置" in str(exc)
    else:                                        # pragma: no cover
        raise AssertionError("不可用的提供方不该被切换成功")
    assert router.active == "deepseek", "失败的切换不能改动当前选择"


def test_an_unknown_name_is_refused() -> None:
    router = ModelRouter(_settings(), storage=_db())
    try:
        router.switch("gpt-5")
    except ValueError as exc:
        assert "gpt-5" in str(exc)
    else:                                        # pragma: no cover
        raise AssertionError("未知名字应当被拒绝")


def test_a_remembered_provider_that_became_unavailable_falls_back() -> None:
    """上次选了 GLM，但这次 GLM 的 key 被删了 —— 回落默认，而不是起不来。"""
    db = _db()
    app_settings.set_setting(db, app_settings.KEY_ACTIVE_PROVIDER, "glm")

    router = ModelRouter(_settings(glm_api_key=""), storage=db)

    assert router.active == "deepseek"


def test_the_router_forwards_to_the_selected_provider() -> None:
    """路由要真的把请求转给"当前那一家"，而不是永远打 DeepSeek。"""
    router = ModelRouter(_settings(), storage=_db())
    seen: list[str] = []

    class _Fake:
        def __init__(self, name: str) -> None:
            self.name = name

        async def complete(self, messages, tools=None):
            seen.append(self.name)
            return type("R", (), {"content": self.name})()

    router._cache["glm"] = _Fake("glm")        # 替身：只验证转发目标

    router.switch("glm")
    reply = asyncio.run(router.complete([]))

    assert reply.content == "glm"
    assert seen == ["glm"]


# --------------------------------------------------------------- 接口


async def test_get_model_returns_state_without_keys() -> None:
    router = ModelRouter(_settings(), storage=_db())
    async with _client(_model_app(router)) as client:
        response = await client.get("/api/model")

    assert response.status_code == 200
    body = response.json()
    assert body["active"] == "deepseek"
    assert [p["name"] for p in body["providers"]] == ["deepseek", "glm", "mimo"]
    assert "dk" not in response.text and "gk" not in response.text


async def test_post_model_switches_and_returns_the_new_state() -> None:
    router = ModelRouter(_settings(), storage=_db())
    async with _client(_model_app(router)) as client:
        response = await client.post("/api/model", json={"name": "glm"})

    assert response.status_code == 200
    assert response.json()["active"] == "glm"
    assert router.active == "glm"


async def test_post_model_returns_400_with_the_reason_for_mimo() -> None:
    router = ModelRouter(_settings(), storage=_db())
    async with _client(_model_app(router)) as client:
        response = await client.post("/api/model", json={"name": "mimo"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "MiMo" in detail and "配置" in detail


async def test_post_model_rejects_an_empty_name() -> None:
    router = ModelRouter(_settings(), storage=_db())
    async with _client(_model_app(router)) as client:
        response = await client.post("/api/model", json={"name": ""})

    assert response.status_code == 422, "空名字在入参校验就该被挡下"


async def test_probe_reports_ok_when_the_provider_answers() -> None:
    router = ModelRouter(_settings(), storage=_db())

    async def fake_probe(name=None):
        return "可用"

    router.probe = fake_probe                     # type: ignore[assignment]
    async with _client(_model_app(router)) as client:
        response = await client.post("/api/model/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["provider"] == "deepseek"
    assert "可用" in body["message"]


async def test_probe_reports_failure_as_ok_false_not_500() -> None:
    """探测失败是**结果**，不是请求失败：给界面一个 ❌，别走错误分支。"""
    router = ModelRouter(_settings(), storage=_db())

    async def boom(name=None):
        raise RuntimeError("401 Unauthorized")

    router.probe = boom                           # type: ignore[assignment]
    async with _client(_model_app(router)) as client:
        response = await client.post("/api/model/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "401" in body["message"]


async def test_model_endpoints_say_503_when_no_router_is_wired() -> None:
    """只挂了假 LLM 的测试应用也要拿到明确的状态码，而不是 500。"""
    app = FastAPI()
    app.include_router(model_router)
    async with _client(app) as client:
        response = await client.get("/api/model")

    assert response.status_code == 503
