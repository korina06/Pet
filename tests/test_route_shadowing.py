"""路由遮挡守卫：任何字面路径只要先被前面某条参数路由完整命中，就说明它根本走不到。
"""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.routing import Match

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _real_app():
    """按**路径**加载真的 `main.py`。

    为什么不 `from main import app`：自研 runner（`scripts/check_frontend.py`）启动时
    用 `sys.modules.setdefault("main", stub)` 装了一个**只有 STATIC_DIR 的假模块**，
    那样 `from main import app` 在那里必 ImportError。和 `tests/test_startup.py` 同样
    用 importlib 绕开替身，两个 runner 下都能验。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pet_desktop_main_for_routes", PROJECT_ROOT / "main.py"
    )
    assert spec is not None and spec.loader is not None, "找不到 main.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _scope(path: str, method: str = "GET") -> dict[str, object]:
    return {
        "type": "http",
        "path": path,
        "method": method,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


def _api_routes(app):
    from fastapi.routing import APIRoute

    return [route for route in app.routes if isinstance(route, APIRoute)]


def test_no_literal_route_is_swallowed_by_an_earlier_parameter_route() -> None:
    app = _real_app()
    routes = _api_routes(app)
    problems: list[str] = []

    for index, route in enumerate(routes):
        path = route.path
        if "{" in path:
            # 参数路由互相之间是否歧义是另一件事（相对路径更靠前的更具体），
            # 这里只盯「字面路径被参数路由吃掉」这一种，因为它的后果最隐蔽：
            # 接口存在、测试能调通、真请求却永远落到别人身上。
            continue
        for earlier in routes[:index]:
            if earlier.path == path:
                continue
            for method in sorted(route.methods or {"GET"}):
                match, _ = earlier.matches(_scope(path, method))
                if match == Match.FULL:
                    problems.append(
                        f"{method} {path}（{route.name}）被它前面的 "
                        f"{earlier.path}（{earlier.name}）完整命中，永远走不到"
                    )
                    break

    assert problems == [], "有接口被前面的路由吃掉了：\n" + "\n".join(problems)


def _auth_headers() -> dict[str, str]:
    """本机 .env 可能配了 AUTH_TOKENS（第 251 步）：这条测试只关心路由，不关心鉴权。

    不这么做的话，用户一旦启用鉴权，本地全量就会红在这里 —— 而 CI 没有 .env，
    反而绿，属于「环境依赖的假红/假绿」。
    """
    from core.config import get_settings

    raw = getattr(get_settings(), "auth_tokens", "") or ""
    if not raw.strip():
        return {}
    first = raw.split(",")[0]
    _, _, token = first.partition(":")
    return {"Authorization": f"Bearer {token.strip()}"} if token.strip() else {}


def test_map_config_answers_when_asked_by_url() -> None:
    """光看路由表还不够，必须真发一次请求 —— 这正是当初漏掉的那一步。"""
    from fastapi.testclient import TestClient

    app = _real_app()
    with TestClient(app) as client:
        response = client.get("/api/map/config", headers=_auth_headers())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "enabled" in payload
    # 安全密钥绝不出现在响应里（第 224 步的红线，这里再钉一次）
    assert "jscode" not in payload
    assert "security_code" not in payload
