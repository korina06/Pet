"""lint 进验证：规则集在仓库里、CI 真跑、本机装了 ruff 就必须过（没装则跳过）。
"""

from __future__ import annotations

import sys
from pathlib import Path



PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ruff_available() -> bool:
    import subprocess

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def test_ruff_config_exists():
    """规则集要在仓库里，而不是靠命令行参数（第 121 步）。

    配置里同时写了"为什么只开这么窄"—— 那条注释比配置本身重要：
    没有它，下一个人会以为可以随手把 select 改成 ALL。
    """
    config = PROJECT_ROOT / "ruff.toml"
    assert config.exists(), "缺少 ruff.toml"

    text = config.read_text(encoding="utf-8")
    assert 'select = ["E4", "E7", "E9", "F"]' in text
    assert "为什么只开这么窄" in text


def test_ci_runs_ruff():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "checks.yml").read_text(
        encoding="utf-8"
    )

    assert "ruff check ." in workflow, "CI 里必须真的跑 ruff"


def test_ruff_passes():
    """本地装了 ruff 就真跑一次 —— 让"提交前就发现"成为可能。

    没装时**直接返回**并打印一句：ruff 是开发工具，不在 requirements.txt 里。
    刻意不 import pytest：那会让这个文件在"自研 runner 没有 pytest"时被整组跳过，
    而它恰恰是纯标准库就能跑的一条。
    """
    import subprocess

    if not _ruff_available():
        print("    本机没装 ruff，跳过本组（CI 里会装并强制执行）")
        return

    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "ruff 报错：\n" + output
