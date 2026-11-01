"""用真实的 `main.py` 起一次 TestClient：确认干净检出下应用能启动、路由注册齐、不依赖外部配置。
"""

from __future__ import annotations

import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = PROJECT_ROOT / ".testtmp"


def _load_real_app():
    """按**路径**加载真的 `main.py`，拿到 `app`。

    为什么不 `from main import app`：自研 runner（`scripts/check_frontend.py`）在启动时
    用 `sys.modules.setdefault("main", stub)` 装了一个**只有 STATIC_DIR 的假模块**
    （它自己注释里写着"不 import 真 main（那会拉起 fastapi）"）。
    于是任何 `from main import app` 在那里都会 `ImportError: cannot import name 'app'`。

    这正好解释了评价9 的 P0 为什么能溜过去：**主验证路径里没有任何东西真的加载 main.py**。
    这条测试用 importlib 绕开那个替身，两个 runner 下都能验。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pet_desktop_main_for_startup", PROJECT_ROOT / "main.py"
    )
    assert spec is not None and spec.loader is not None, "找不到 main.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def test_the_real_app_starts_and_answers_health(monkeypatch):
    """`with TestClient(app)` 会真的跑 lifespan；起不来就抛，正是我们要抓的。"""
    from fastapi.testclient import TestClient

    TEMP_DIR.mkdir(exist_ok=True)
    db_path = TEMP_DIR / f"startup-{uuid.uuid4().hex[:8]}.db"
    monkeypatch.setenv("PET_DB_PATH", str(db_path))

    # 设置是 lru_cache 的：不清理就会沿用别的测试读到的旧 .env / 旧路径
    import core.config as config_module

    config_module.get_settings.cache_clear()

    app = _load_real_app()

    with TestClient(app) as client:          # ← 这一行跑 lifespan
        response = client.get("/api/health")
        assert response.status_code == 200, response.text

    # lifespan 走完等于"库已连上、四套 state 已挂"：逐个抽查（用 getattr，Starlette 的 State 不支持 dir）
    for name in (
        "storage",
        "session_store",
        "recipe_store",
        "checkin_store",
        "trip_store",
        "registry",
        "travel_registry",
        "recipe_registry",
    ):
        assert getattr(app.state, name, None) is not None, f"lifespan 没挂上 app.state.{name}"

    config_module.get_settings.cache_clear()


def test_only_local_hosts_are_accepted(monkeypatch):
    """Host 白名单（第 192 步 / 评价15 T0-1）。

    这是三道墙里唯一能挡 DNS rebinding 的那道：恶意网页把自己的域名解析到 127.0.0.1 后，
    浏览器认为「我在跟自己的域名说话」，于是不发预检、不看 CORS，
    请求带着 `Host: evil.example` 打进本地服务。只有 Host 头能分辨它是谁。
    """
    from fastapi.testclient import TestClient

    TEMP_DIR.mkdir(exist_ok=True)
    monkeypatch.setenv("PET_DB_PATH", str(TEMP_DIR / f"host-{uuid.uuid4().hex[:8]}.db"))
    import core.config as config_module

    config_module.get_settings.cache_clear()
    app = _load_real_app()

    accepted = ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "testserver")
    rejected = (
        "evil.example.com",
        "evil.example.com:8000",
        "127.0.0.1.evil.example.com:8000",      # 前缀像本机、后缀是攻击者域名
    )

    with TestClient(app) as client:
        for host in accepted:
            response = client.get("/api/health", headers={"host": host})
            assert response.status_code == 200, f"Host={host} 不该被挡：{response.status_code}"

        for host in rejected:
            response = client.get("/api/health", headers={"host": host})
            assert response.status_code == 403, f"Host={host} 应该被挡，实际 {response.status_code}"
            assert "只接受本机访问" in response.json()["detail"], response.text

        # 静态首页也要挡：否则 rebinding 至少能读到聊天记录页
        assert client.get("/", headers={"host": "evil.example.com"}).status_code == 403
        # 诊断接口同理，别漏
        assert client.get("/api/diagnostics", headers={"host": "evil.example.com"}).status_code == 403

    config_module.get_settings.cache_clear()
