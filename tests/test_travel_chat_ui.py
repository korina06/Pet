"""出行「聊聊」页：结构读 HTML 与 app.js，行为交给 `scripts/verify_travel_chat.js`。

钉住「打开窗口停在哪一页」和「表单页还在不在」这类一改就悄悄回退的地方；服务端语义在
`tests/test_trip_chat.py`。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from main import STATIC_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_travel_chat.js"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _travel_window() -> str:
    """出行窗口那一段（到桌宠为止），并剥掉 HTML 注释。

    剥注释是必须的：注释里写着「为什么把表单挤到第二页」，不剥掉会撞上自己的说明。
    """
    html = _html()
    start = html.index('id="travel-modal"')
    end = html.index('id="pet"', start)
    return re.sub(r"<!--[\s\S]*?-->", "", html[start:end])


def _opening_tag(block: str, node_id: str) -> str:
    """取含有 `id="..."` 的那个开标签的**完整**文本（含 class/hidden 等属性）。"""
    at = block.index(f'id="{node_id}"')
    start = block.rindex("<", 0, at)
    return block[start:block.index(">", at) + 1]


# ------------------------------------------------------------------ 结构


def test_chat_tab_is_first_and_default_active():
    block = _travel_window()
    chat = block.index('id="travel-tab-chat"')
    form = block.index('id="travel-tab-form"')
    assert chat < form, "「聊聊」要排在「自己填」前面（默认入口）"

    tag = _opening_tag(block, "travel-tab-chat")
    assert 'aria-selected="true"' in tag, "「聊聊」默认选中"
    assert "is-active" in tag, "「聊聊」默认高亮"

    form_tag = _opening_tag(block, "travel-tab-form")
    assert 'aria-selected="false"' in form_tag, "「自己填」默认不选中"
    assert "is-active" not in form_tag, "「自己填」默认不高亮"


def test_form_page_starts_hidden():
    block = _travel_window()
    tag = _opening_tag(block, "travel-page-form")
    assert "hidden" in tag, "表单页一开始是收起来的，等用户点「自己填」才出现"
    assert "hidden" not in _opening_tag(block, "travel-page-chat"), "聊天页一开始是打开的"


def test_chat_page_has_log_hint_and_form():
    block = _travel_window()
    for node_id in (
        "travel-page-chat",
        "travel-chat-log",
        "travel-chat-form",
        "travel-chat-input",
        "travel-chat-send",
    ):
        assert f'id="{node_id}"' in block, f"聊天页缺少 {node_id}"

    # 日志要能被读屏念出来（role=log + aria-live），和主聊天一致
    start = block.index('id="travel-chat-log"')
    tag = block[block.rindex("<div", 0, start):block.index(">", start)]
    assert 'role="log"' in tag and "aria-live" in tag


def test_app_js_wires_the_chat_page():
    js = _js()
    assert "travel-chat-log" in js, "app.js 要引用聊天日志容器"
    assert "/trip/chat" in js, "必须请求第 216 步新增的端点"
    assert "onTravelChatSubmit" in js
    # 打开窗口的默认页：必须是 chat
    open_block = js[js.index("if (kind === 'travel')"):]
    open_block = open_block[:open_block.index("showTravelPage(")]
    assert "showTravelPage('chat')" in js[js.index("if (kind === 'travel')"):][:1200]
    assert "showTravelPage('form')" not in open_block


def test_send_button_disabled_while_waiting():
    """等待期间不能发第二条：桩里测了，这里钉住源码里那个开关。"""
    js = _js()
    assert "travelChatBusy" in js
    assert "el.travelChatSend.disabled = true" in js
    assert "el.travelChatSend.disabled = false" in js


# ------------------------------------------------------------------ 行为


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_travel_chat_behaviour():
    """真跑一遍：开窗 → 说一句 → 看气泡 → 可疑名字提醒 → 失败路径 → 切标签。"""
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"

    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "出行对话行为验证未全部通过：\n" + output
    assert "全部通过" in output, "验证脚本报告了失败项：\n" + output
