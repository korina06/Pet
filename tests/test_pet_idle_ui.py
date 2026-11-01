"""待机动画的行为验证接线：定时器排上、URL 换了（才会重播）、演完摘类、减少动态效果时不动。

真实节奏由烟测验，桩里定时器是立即执行的。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_pet_idle.js"
NODE = shutil.which("node")


def test_the_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_pet_idle_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "待机动画行为验证未通过：\n" + output
    assert "失败 0 项" in output, output
    for label in (
        "页面上有待机动画层",
        "视频动作把播放位置归零",
        "演完把类摘掉",
        "减少动态效果时不演",
    ):
        assert label in output, f"缺少断言 {label}：\n{output}"
