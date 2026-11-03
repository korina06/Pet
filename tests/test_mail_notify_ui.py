"""邮件提醒的行为验证接线：冒泡一次、同一封只说一次、静默时段不吵、没登入是哑的、退出后停表。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_mail_notify.js"
NODE = shutil.which("node")


def test_the_script_exists():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"


def test_the_page_still_has_the_pet_note_nodes():
    """结构前提：气泡那两个节点在页面里。

    行为验证挂的时候，第一个要排除的就是"节点根本没建出来" —— 那和 app.js 逻辑无关，
    但两者的现象完全一样（气泡不出现）。分开断，失败信息才指得向真正的方向。
    """
    html = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for node_id in ('id="pet-note"', 'id="pet-note-text"'):
        assert node_id in html, f"static/index.html 里找不到 {node_id}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_mail_notice_behaviour():
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "邮件提醒行为验证未通过：\n" + output
    assert "全部通过" in output, output
    for label in (
        "桌宠气泡显示出来了",
        "点气泡之后邮箱窗口打开",
        "定位不到时明确说出来（不假装成功）",
        "已经说过的 UID 不再冒泡",
        "静默时段不冒泡",
        "天亮后把夜里攒的说出来",
        "没登入不冒泡",
        "退出之后后台不再查新",
    ):
        assert label in output, f"缺少断言 {label}：\n{output}"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_mail_notice_behaviour_does_not_depend_on_the_machine_timezone():
    """第 276 步：CI（runner 是 UTC）连红 17 次、本机（UTC+8）一直绿，根因是时区。

    `isQuietHour()` 读的是 `getHours()`，也就是**本地钟点**；而验证脚本以前给
    `__petMailNow` 的是一个带 `+08:00` 的绝对时刻。在 UTC 的机器上 14:00 被换算成
    06:00，落进静默时段（22:00–08:00），"白天该冒泡"于是变成"静默只记账"，
    7 条断言一起挂，而本机永远看不到。

    所以这条守卫把 CI 的时区条件搬到本地：同一个脚本在 `TZ=UTC` 下也必须全绿。
    没有它，下次谁再往钩子里塞绝对时刻，本地依然是绿的。
    """
    env = dict(os.environ, TZ="UTC")
    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=env,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, "TZ=UTC 下邮件提醒行为验证未通过：\n" + output
    assert "全部通过" in output, output
