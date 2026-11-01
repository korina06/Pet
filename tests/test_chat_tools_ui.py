"""功能栏折叠的行为验证接线：跑 `scripts/verify_chat_tools.js`，验「›」的展开收起、aria 状态、
点胶囊后自动收起，以及「打字时仍然收起」这条既有行为。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_chat_tools.js"
NODE = shutil.which("node")


def test_the_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_chat_tools_collapse_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "功能栏折叠验证未通过：\n" + output
    assert "失败 0 项" in output, output
    for label in (
        "默认收起：胶囊行 hidden",
        "点开后 aria-expanded=true",
        "点胶囊后功能栏自动收起",
        "打字时胶囊行收起",
    ):
        assert label in output, f"缺少断言 {label}：\n{output}"
