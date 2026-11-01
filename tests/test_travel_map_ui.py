"""行程地图的接线：卡片在不在、脚本有没有被引入、是不是点了才加载。

行为交给 `scripts/verify_travel_map.js`；另有用例确认这个脚本真的登记进了测试 runner。
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
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_travel_map.js"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _map_js() -> str:
    return (STATIC_DIR / "map.js").read_text(encoding="utf-8")


def _travel_window() -> str:
    html = _html()
    start = html.index('id="travel-modal"')
    end = html.index('id="pet"', start)
    return re.sub(r"<!--[\s\S]*?-->", "", html[start:end])


# ------------------------------------------------------------------ 结构


def test_map_card_lives_in_the_overview_and_starts_hidden():
    block = _travel_window()
    assert 'id="travel-map-card"' in block, "概览页要有地图卡片"
    # 卡片默认收起来：没配置 key 的用户不该看到一个点不动的空壳
    at = block.index('id="travel-map-card"')
    tag = block[block.rindex("<", 0, at) : block.index(">", at) + 1]
    assert "hidden" in tag, "卡片默认必须是 hidden，由 app.js 问过配置再决定显不显示"


def test_map_controls_exist():
    block = _travel_window()
    for node_id in (
        "travel-map",
        "travel-map-days",
        "travel-map-toggle",
        "travel-map-note",
    ):
        assert f'id="{node_id}"' in block, f"地图卡片缺少 {node_id}"


def test_map_js_is_loaded_by_the_page_but_not_by_amap():
    """map.js 自己要在页面里（很轻），高德的 JS API 由它在被点开时才去拉。"""
    html = _html()
    assert "map.js" in html, "页面要引入 static/map.js"
    assert "webapi.amap.com" not in html, "高德脚本必须点到才加载，不许写死在页面里"
    assert "webapi.amap.com" in _map_js(), "拉高德脚本的地址应该只在 map.js 里出现一次"


def test_map_js_exposes_the_documented_api():
    source = _map_js()
    for name in ("render", "setFocus", "destroy", "pointList", "polylineFor", "colorFor"):
        assert name in source, f"map.js 应暴露 {name}"
    assert "TravelMap" in source, "对外挂的名字是 window.TravelMap"


def test_app_js_asks_the_backend_before_showing_the_card():
    js = _js()
    assert "/map/config" in js, "配置必须问后端（key 不许写进前端）"
    assert "prepareTravelMap" in js and "expandTravelMap" in js
    # 没配 key 时整张卡片不出现
    assert "el.travelMapCard.hidden = !config.enabled" in js


def test_prepare_is_idempotent_so_an_open_map_never_self_collapses():
    """第 224 步实测：概览会渲染两次，第二次把用户正看着的地图收起来了。"""
    js = _js()
    assert "mapSignature" in js, "要能分辨'换了一份行程'和'同一份又渲染了一遍'"
    assert "mapState.signature === signature" in js
    assert "epoch" in js, "慢的那次 prepare 不能盖掉新的一次"


def test_runner_includes_the_map_verification():
    """全量 runner 是按模块登记测试的；行为脚本由本文件自己起 node 去跑。

    所以这里要钉两件事：本模块登记进了 runner，且它真的会去跑那个脚本
    ——第 216 步漏登记过一次，本地全绿而 runner 压根没碰到新代码。
    """
    runner = (PROJECT_ROOT / "scripts" / "check_frontend.py").read_text(encoding="utf-8")
    assert "test_travel_map_ui" in runner, "本文件必须登记进全量 runner"
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"
    assert "verify_travel_map.js" in Path(__file__).read_text(encoding="utf-8")


def test_backend_map_config_route_is_registered():
    main_source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "map_routes" in main_source, "地图配置路由要真的挂到应用上"


# ------------------------------------------------------------------ 行为
@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_travel_map_behaviour():
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"

    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "行程地图行为验证未全部通过：\n" + output
    assert "全部通过" in output, "验证脚本报告了失败项：\n" + output

# ---------------------------------------------------- 聊天里的路线图（第 227 步）

CHAT_VERIFY = PROJECT_ROOT / "scripts" / "verify_travel_chat.js"


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def test_chat_shows_the_images_the_server_returns():
    # 断言必须对着**代码**：注释里也写着这个地址（解释用的），按原文断言会误红
    js = re.sub(r"/\*[\s\S]*?\*/", "", _js())
    submit = js[js.index("async function onTravelChatSubmit") : js.index("function makeTravelTab")]
    assert "data.routes" in submit, "后端给了 routes，前端得读它"
    assert "addTravelRouteImage" in submit, "读到了要真的去画"
    assert "route.url" in js, "图片地址来自服务端，前端不拼"
    assert "/api/map/route.png" not in js, "前端不许自己拼地图地址（服务端才知道接口形状）"


def test_route_image_figure_is_styled():
    css = _css()
    assert ".msg-travel-map img" in css
    assert "travel-map--broken" in css, "图挂了要有兜底样式"
    js = _js()
    assert "img.loading = 'lazy'" in js, "聊天里可能有好几张图，不该一次全拉"


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_travel_chat_behaviour():
    assert CHAT_VERIFY.exists(), f"缺少验证脚本：{CHAT_VERIFY}"
    completed = subprocess.run(
        [NODE, str(CHAT_VERIFY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, "出行对话行为验证未全部通过：\n" + output
    assert "全部通过" in output, "验证脚本报告了失败项：\n" + output
