"""配置层的测试。"""

from __future__ import annotations

import pytest

from core.config import Settings


def test_hefeng_base_url_adds_scheme():
    settings = Settings(hefeng_api_host="abc123.qweatherapi.com")
    assert settings.hefeng_base_url == "https://abc123.qweatherapi.com"


def test_hefeng_base_url_preserves_existing_scheme_and_strips_slash():
    assert (
        Settings(hefeng_api_host="https://abc.qweatherapi.com/").hefeng_base_url
        == "https://abc.qweatherapi.com"
    )
    assert (
        Settings(hefeng_api_host="http://abc.qweatherapi.com").hefeng_base_url
        == "http://abc.qweatherapi.com"
    )


def test_hefeng_base_url_raises_actionable_error_when_missing():
    """缺 host 时报错必须告诉用户去哪拿，而不是只说"未配置"。"""
    settings = Settings(hefeng_api_host="")
    with pytest.raises(ValueError) as excinfo:
        _ = settings.hefeng_base_url
    assert "console.qweather.com" in str(excinfo.value)


def test_require_deepseek_key_raises_with_hint():
    with pytest.raises(ValueError, match="platform.deepseek.com"):
        Settings(deepseek_api_key="").require_deepseek_key()


def test_require_deepseek_key_returns_stripped_key():
    assert Settings(deepseek_api_key="  sk-abc  ").require_deepseek_key() == "sk-abc"


def test_require_hefeng_key_raises_when_missing():
    with pytest.raises(ValueError, match="HEFENG_API_KEY"):
        Settings(hefeng_api_key="").require_hefeng_key()


def test_default_agent_limits_are_sane():
    settings = Settings()
    assert settings.agent_max_steps >= 1
    assert settings.session_max_messages >= 2
    # 默认城市应该是北京
    assert 39 < settings.default_city_lat < 41
    assert 115 < settings.default_city_lon < 118


def test_env_example_covers_every_setting():
    """`.env.example` 必须覆盖 `core/config.py` 里的**每一个**字段（第 70 步）。

    这条是拿真事立的：`SERPAPI_API_KEY` 与 `PET_DB_PATH` 曾经在 config.py 里有、
    但 .env.example 里没有 —— 别人 clone 下来会**静默漏配**，跑到那个功能才炸。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8")

    missing = [
        name.upper() for name in Settings.model_fields if name.upper() not in example
    ]
    assert not missing, f".env.example 缺少这些键：{missing}"


def test_trip_deadline_is_a_sane_positive_number():
    """行程总预算必须是个正数（第 70 步）。它是 asyncio.timeout 的参数，
    设成 0 或负数会让每次生成立刻超时。"""
    assert Settings().trip_deadline_seconds > 0


def test_readme_documents_every_setting():
    """README 的环境变量表必须提到 `core/config.py` 里的**每一个**字段（第 107 步）。

    为什么加这条：`.env.example` 早有守卫，README 却没有 —— 于是模型切换那轮
    加了 6 个配置项，README 的表格还停在 6 行。文档漂移不是靠"记得更新"能防住的，
    得让它变成一种**会失败的测试**。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    missing = [
        name.upper() for name in Settings.model_fields if name.upper() not in readme
    ]
    assert not missing, f"README 没提到这些配置项：{missing}"

def test_the_pet_address_defaults_to_older_brother(monkeypatch):
    """称呼默认「哥哥」，可由 PET_ADDRESS 覆盖（第 143 步）。"""
    from core.config import Settings

    monkeypatch.delenv("PET_ADDRESS", raising=False)
    assert Settings(_env_file=None).pet_address == "哥哥"

    monkeypatch.setenv("PET_ADDRESS", "姐姐")
    assert Settings(_env_file=None).pet_address == "姐姐"
