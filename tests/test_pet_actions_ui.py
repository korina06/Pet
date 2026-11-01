"""动作菜单的行为验证接线：轮盘「动作」→ 菜单 → 点选 → 菜单收起、动作执行。

动画本身由真浏览器烟测验，DOM 桩里观察不到持续几百毫秒的类。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_pet_actions.js"
NODE = shutil.which("node")


def test_the_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_pet_action_menu_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "动作菜单行为验证未通过：\n" + output
    assert "失败 0 项" in output, output
    for label in (
        "轮盘里有「动作」一项",
        "点「动作」后菜单出现",
        "动作真的执行了",
        "Esc 也能收起动作菜单",
    ):
        assert label in output, f"缺少断言 {label}：\n{output}"
