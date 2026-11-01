"""鉴权前端：配了 `AUTH_TOKENS` 会自动带 token，本机模式行为不变。

结构断言在 Python 这侧，真实行为交给 `scripts/verify_auth_ui.js`。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from main import STATIC_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_auth_ui.js"


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_app_js_has_one_place_that_adds_the_token() -> None:
    js = _js()
    assert "function apiFetch(" in js, "需要一个统一的 fetch 包装"
    assert "karo.authToken" in js, "token 要存在 localStorage 的这个键里"
    assert "Bearer " in js, "要按 Authorization: Bearer 格式发"
    assert "status === 401" in js or "status !== 401" in js, "401 要走输入框流程"


def test_no_plain_fetch_call_sites_remain() -> None:
    """40 处调用点一旦漏一个，那一个接口在开了鉴权后就会 401。"""
    offenders = []
    for number, line in enumerate(_js().splitlines(), start=1):
        if not __import__("re").search(r"(^|[^.\w])fetch\(", line):
            continue
        if "nativeFetch" in line or "function apiFetch" in line:
            continue
        offenders.append(number)
    assert offenders == [], f"还有直接 fetch 的调用点：{offenders}"


def test_401_is_retried_exactly_once() -> None:
    js = _js()
    assert js.count("askForToken") >= 2, "既要触发浮层，也要在提交后重试"
    assert "只重试一次" in js or "retry once" in js.lower(), "注释要写清为什么只重试一次"


def test_runner_includes_the_auth_ui_tests() -> None:
    runner = (PROJECT_ROOT / "scripts" / "check_frontend.py").read_text(encoding="utf-8")
    assert "test_auth_ui" in runner, "本模块必须登记进全量 runner"
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_auth_ui_behaviour() -> None:
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, "鉴权前端行为验证未通过：\n" + output
    assert "全部通过" in output, "验证脚本报告了失败项：\n" + output
