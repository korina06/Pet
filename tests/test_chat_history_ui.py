"""把 `scripts/verify_chat_history.js` 真跑一遍：历史慢回来时用户的消息不丢、清空后不被画回来。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_chat_history.js"
# 动态查找（第 104 步的教训）：绝不写死本机 node 路径
NODE = shutil.which("node")


def test_verify_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_chat_history_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "聊天历史行为验证未全部通过：\n" + output
    assert "失败 0 项" in output, "验证脚本报告了失败项：\n" + output
    # 三个场景都真的跑过了（名字变了要同步改这里，避免"静默少跑一个场景"）
    for scene in ("场景 1", "场景 2", "场景 3"):
        assert scene in output, f"{scene} 没有出现在输出里：\n{output}"
