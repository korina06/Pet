"""聊天流式的前端行为验证接线：跑 `scripts/verify_chat_stream.js`（delta 会长、reset 会清、meta 会覆盖）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_chat_stream.js"
NODE = shutil.which("node")          # 动态查找（第 104 步的教训）


def test_the_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_chat_stream_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "聊天流式行为验证未通过：\n" + output
    assert "失败 0 项" in output, output
    for label in ("打到 /api/chat/stream", "reset 之后气泡里没有半句废话", "meta 覆盖"):
        assert label in output, f"缺少断言 {label}：\n{output}"
