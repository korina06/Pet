"""应用装配与静态资源路由：页面引用的 style.css / app.js 必须真的能取到。

这一类问题的表现是「单测全绿但页面是白板」，所以这里也守着 README 与活文档的形状。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx
import pytest
from core.config import get_settings
from httpx import ASGITransport, AsyncClient
from main import STATIC_DIR, app


async def _get(path: str) -> httpx.Response:
    """直接打进 ASGI 应用取一个路径，不启动真实服务器。"""
    # Host 必须用 `testserver`（不是习惯写法 `test`）：第 192 步的 Host 白名单
    # 只放行回环地址 + `testserver`，而后者的存在理由就写在白名单注释里
    # ——「给 TestClient 用的」。对齐现有约定，而不是反向把 `test` 加进白名单。
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(path)


@pytest.mark.parametrize("path", ["/", "/style.css", "/app.js"])
async def test_page_and_relative_assets_are_reachable(path: str) -> None:
    """首页与它用相对路径引用的资源都必须能取到。

    这是"页面能打开、桌宠有样式"的最低保证；
    style.css / app.js 若 404，页面会变成点不动的白板。
    """
    response = await _get(path)
    assert response.status_code == 200, f"{path} 返回 {response.status_code}"
    assert response.content, f"{path} 返回了空内容"


async def test_health_still_handled_by_api_not_static_mount() -> None:
    """静态目录挂在根路径后，/api/* 仍必须由 API 路由处理（返回 200 而不是被静态吃掉）。"""
    response = await _get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_dir_has_frontend_files() -> None:
    assert STATIC_DIR.exists(), f"静态目录不存在：{STATIC_DIR}"
    for name in ("index.html", "style.css", "app.js"):
        assert (STATIC_DIR / name).exists(), f"缺少前端文件：{name}"


def test_index_html_uses_relative_asset_paths() -> None:
    """锁定约定：index.html 用相对路径引用资源。

    若哪天改成绝对路径 /static/xxx，这里会失败，
    提醒同步调整 main.py 的挂载点——两者必须一致，
    否则页面会在浏览器里静默失去样式。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="style.css"' in html
    assert 'src="app.js"' in html


# ------------------------------------------------------------ 桌宠形象（第 6 步）


#: PNG 的固定字节头
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(path) -> tuple[int, int]:
    """读 PNG 的宽高（IHDR 就在固定偏移，不用额外依赖）。"""
    head = path.read_bytes()[:24]
    if head[:8] != PNG_MAGIC:
        raise AssertionError(f"{path.name} 不是 PNG")
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return width, height


def test_pet_image_exists_and_is_real_png() -> None:
    """桌宠形象是 static/pet.png，必须是真实的 PNG（第 141 步换成透明底的竖构图）。"""
    image = STATIC_DIR / "pet.png"
    assert image.exists(), "缺少桌宠图片 static/pet.png"

    # 真实 PNG 的字节头；顺便防止有人塞进一个改了扩展名的其它格式
    head = image.read_bytes()[:8]
    assert head == PNG_MAGIC, f"pet.png 不是真正的 PNG，字节头={head!r}"


async def test_pet_image_reachable_over_http() -> None:
    """图片要能真的取到，否则按钮会变成一片空白。"""
    response = await _get("/pet.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert len(response.content) > 0


def test_pet_uses_image_with_emoji_fallback() -> None:
    """桌宠形象用图片，并保留加载失败时的 emoji 回退。

    只换成 <img> 而不做回退的话，图片一旦丢失按钮就彻底空白，
    用户看不出那里可以点。
    """
    html = _index_html()
    tag = re.search(r"<img[^>]*id=\"pet-image\"[^>]*>", html)
    assert tag, "index.html 里找不到桌宠图片元素"
    body = tag.group(0)
    assert 'src="pet.png"' in body, "图片应引用本目录下的 pet.png"
    assert "alt=" in body, "图片需要有 alt，否则读屏软件读不出来"
    assert "onerror" in body, "图片加载失败时应回退，不能留下空白按钮"

    fallback = re.search(r"<span[^>]*class=\"pet-fallback\"[^>]*>", html)
    assert fallback, "缺少 emoji 回退元素"
    assert "hidden" in fallback.group(0), "回退元素默认应隐藏"


def test_pet_image_css_fills_button_without_distortion() -> None:
    """图片要填满按钮且不变形。

    - overflow: hidden + 圆角负责把图片裁成形状；
    - object-fit: cover 保证图片比例与按钮不一致时也不被拉伸。
    另外按钮不能用 grid + place-items:center 来放图片，
    那样图片会被缩回原始尺寸、填不满按钮（已踩过一次）。
    """
    code = _style_css_without_comments()

    body_rule = _rule(".pet-body", code)
    assert body_rule, "找不到 .pet-body 规则"
    body = body_rule
    assert "overflow: hidden" in body, "按钮需要裁掉图片超出的部分"
    assert "place-items: center" not in body, (
        "不要用 grid 居中图片——会把图片缩回原始尺寸"
    )

    image_rule = _rule(".pet-image", code)
    assert image_rule, "找不到 .pet-image 规则"
    image = image_rule
    assert "object-fit: cover" in image, "图片应裁剪而不是拉伸变形"
    assert "width: 100%" in image and "height: 100%" in image, "图片应填满按钮"


def test_pet_button_is_enlarged_and_matches_image_ratio() -> None:
    """第 12 步：按钮要放大，且比例与图片一致，才能保留更多画面。

    几何关系：按钮为正方形时，横图按 object-fit: cover 必然切掉两侧，
    能保留的比例只由图片比例决定（1024/1536 = 66.7%）。
    把按钮做成与图片同比例的 3:2 后，保留比例变成 100%。
    """
    code = _style_css_without_comments()

    variables = re.search(r":root\s*\{([^}]*)\}", code)
    assert variables, "找不到 :root 变量"
    values = variables.group(1)

    pet_w = _px(values, "--pet-w")
    pet_h = _px(values, "--pet-h")
    assert pet_w is not None and pet_h is not None, "应通过 --pet-w / --pet-h 定义按钮尺寸"

    # 放大：明显大于上一版的 76px
    assert pet_w > 76, f"按钮宽度 {pet_w}px 应大于上一版的 76px"
    assert pet_h > 76, f"按钮高度 {pet_h}px 应大于上一版的 76px"

    # 比例要与**图片本身**一致，否则 cover 又会裁掉内容。
    # 第 141 步起不再写死 1.5（那是旧狐狸图的 1536x1024）：直接从 PNG 头里读尺寸，
    # 换形象时只要改 --pet-w/--pet-h 两个数，这条测试会自动跟着新图校验。
    image_w, image_h = _png_size(STATIC_DIR / "pet.png")
    ratio = image_w / image_h
    assert abs(pet_w / pet_h - ratio) < 0.02, (
        f"按钮宽高比 {pet_w / pet_h:.3f} 应贴近图片的 {ratio:.3f}，否则会裁掉画面"
    )

    # 形状走变量，且不再是正圆（正圆配横图必裁）
    body = _rule(".pet-body", code)
    assert body, "找不到 .pet-body 规则"
    assert "border-radius: var(--pet-radius)" in body, (
        "圆角应走变量，便于窄屏统一调整"
    )
    assert "border-radius: 50%" not in body, (
        "不该再用正圆——正圆配 1536x1024 的横图必然切掉两侧"
    )


def test_wheel_orbits_pet_center_and_bubble_follows_pet_width() -> None:
    """第 39 步：轮盘改为**圆形环绕**桌宠。

    - 原点落在桌宠中心：.wheel 用零尺寸盒子 + top/left 各 50%
    - 4 个小圆均布在半径 --orbit 的圆上，位置由 app.js 的 placeOnOrbit()
      算出后写进 --tx/--ty（CSS 无法对任意数量做三角函数）
    - 所以轮盘**不再依赖 --pet-h**（那是"排在宠物上方"时的做法）
    - 气泡仍贴在桌宠右侧，继续引用 --pet-w
    """
    code = _style_css_without_comments()

    wheel = _rule(".wheel", code)
    assert wheel is not None, "找不到 .wheel 规则"
    assert "top: 50%" in wheel, (
        "轮盘应把原点放在桌宠中心（零尺寸盒子 + 50%/50%）"
    )
    # 第 258 步：水平方向不再写死 50%，而是 50% + --wheel-shift-x。
    # 素材可视中心偏左 22.8px，轮盘要跟着左移才能与「看得见的 karo」同心。
    assert "left: calc(50% + var(--wheel-shift-x))" in wheel, (
        "轮盘的水平基准应引用 --wheel-shift-x（纯平移，不改弧线）"
    )
    shift = re.search(r"--wheel-shift-x:\s*(-?[\d.]+)px", code)
    assert shift, "找不到 --wheel-shift-x"
    assert -30 <= float(shift.group(1)) <= -15, (
        f"平移量应在 -23px 附近（pet.png 可视中心偏左 22.8px），当前 {shift.group(1)}px"
    )
    assert "width: 0" in wheel and "height: 0" in wheel, (
        "轮盘盒子应为零尺寸，否则小圆的相对位移会不对称"
    )
    # 第 260 步：轮盘必须盖在 .pet-body 之上。左移之后「对话」小圆落进桌宠盒子，
    # 而 .pet-body 是 z-index 3 的点击层，会把点击吃掉（表现：对话按钮没反应）。
    body = _rule(".pet-body", code)
    assert body is not None, "找不到 .pet-body 规则"
    wheel_z = re.search(r"z-index:\s*(\d+)", wheel)
    body_z = re.search(r"z-index:\s*(\d+)", body)
    assert wheel_z and body_z, "轮盘与桌宠本体都要有明确的 z-index"
    assert int(wheel_z.group(1)) > int(body_z.group(1)), (
        f"轮盘 z-index（{wheel_z.group(1)}）必须高于桌宠本体（{body_z.group(1)}），"
        "否则落在桌宠盒子内的小圆点不到"
    )

    listing = _rule(".wheel-list", code)
    assert listing is not None, "找不到 .wheel-list 规则"
    assert "display: block" in listing, (
        "小圆改用绝对定位均布在圆上，列表本身不再承担排布"
    )

    item = _rule(".wheel-item", code)
    assert item is not None, "找不到 .wheel-item 规则"
    assert "position: absolute" in item, "小圆应绝对定位在环绕圆周上"
    assert "var(--wheel-btn)" in item, "小圆尺寸应走 --wheel-btn"
    assert "border-radius: 50%" in item, "子功能应是圆形外框"
    # 收起态只做居中；两个方向的偏移在"打开态"规则里生效
    assert "translate(-50%, -50%)" in item, "收起态应以自身中心对齐定位点"

    opened = re.search(r"\.wheel\.is-open\s+\.wheel-item\s*\{([^}]*)\}", code)
    assert opened is not None, "找不到轮盘打开态的规则"
    assert "var(--tx" in opened.group(1) and "var(--ty" in opened.group(1), (
        "打开后小圆位置应由 --tx/--ty 决定（角度由 JS 算）"
    )

    assert re.search(r"\.bubble\s*\{[^}]*var\(--pet-w\)", code), (
        "气泡定位应引用 --pet-w，否则按钮变宽后气泡会盖住它"
    )


def test_orbit_position_is_computed_and_uses_radius_variable() -> None:
    """环绕位置必须在 JS 里按角度算，且半径读的是 --orbit。

    这条替代了早期"轮盘用 CSS grid、JS 不算坐标"的断言——
    环绕本质是极坐标布局，CSS 无法对任意数量做三角函数，
    所以位置计算**必须**回到 JS。但外观（圆形外框、悬停显字）仍归 CSS。

    布局形态（第 39 步起）：小圆绕过桌宠**上方**成对称扇形，
    不是上下左右十字——十字会把小圆放到桌宠正下方，视觉上被压住。
    2026-10-31（第 258 步）整体左移：弧线保持对称，平移交给 CSS 的 --wheel-shift-x。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    code = _style_css_without_comments()

    assert "function placeOnOrbit" in js, "缺少环绕定位函数"
    assert "placeOnOrbit(button, index, actions.length)" in js, (
        "生成小圆时应调用环绕定位"
    )
    # 半径从 CSS 变量读，而不是在 JS 里写死像素
    assert re.search(r"placeOnOrbit[\s\S]{0,700}?--orbit", js), (
        "环绕半径应读 CSS 变量 --orbit，便于窄屏统一缩放"
    )

    # 弧线只取上半圈的一段：两端角度都应落在 0°~180°（即屏幕上方）
    arc = re.search(r"const ARC_START = (\d+);\s*const ARC_END = (\d+);", js)
    assert arc, "找不到弧线范围常量 ARC_START / ARC_END"
    start, end = int(arc.group(1)), int(arc.group(2))
    assert 0 < start < 90 < end < 180, (
        f"弧线应在正上方两侧对称展开（当前 {start}°~{end}°）；"
        "若跨过 0°/180° 说明小圆会跑到桌宠下方"
    )
    # 第 258 步：左移由 CSS 的 --wheel-shift-x 做纯平移，弧线本身保持对称；
    # 不要靠在角度里「转一下」——那会让五个点的间距投影变得不均匀。
    assert abs((start + end) / 2 - 90) < 1, (
        f"弧线中心应为正上方 90°（当前 {(start + end) / 2}°），偏移交给 --wheel-shift-x"
    )

    # 等分依据是"项数 - 1"个间隔，才能保证两端都落到弧线边界
    assert re.search(r"index / \(total - 1\)", js), (
        "弧线上应按「项数-1」个间隔均分，让首尾正好落在弧线两端"
    )

    # 半径变量确实定义在 :root，且窄屏有覆盖
    root = _rule(":root", code)
    assert root is not None and "--orbit" in root, "--orbit 应在 :root 定义"
    media = re.search(r"@media \(max-width: 560px\)\s*\{([\s\S]*)\n\}", code)
    assert media and "--orbit" in media.group(1), "窄屏应覆盖 --orbit 做等比缩小"


def test_disabled_wheel_items_are_hidden_until_the_wheel_opens() -> None:
    """第 261 步：占位项的「灰一档」只能在打开态生效。

    收起态的小圆是 `opacity: 0` 且停在圆心（桌宠身上）。如果 `:disabled` 规则里
    无条件写 `opacity: .55`，它的特异性更高，占位项就会在轮盘没打开时浮在 karo 身上
    （第 260 步把轮盘 z-index 提到桌宠之上后才被看见）。
    """
    code = _style_css_without_comments()

    disabled = _rule(".wheel-item:disabled", code)
    assert disabled is not None, "找不到 .wheel-item:disabled"
    assert "opacity" not in disabled, (
        "收起态的半透明必须由 .wheel.is-open 限定，不能在 :disabled 里无条件写"
    )
    opened = _rule(".wheel.is-open .wheel-item:disabled", code)
    assert opened is not None and "opacity: .55" in opened, (
        "打开态才把占位项调暗一档"
    )


def test_wheel_label_is_hidden_until_hover() -> None:
    """第 39 步：小圆里只放图标，文字要鼠标停上去才出现。

    两个要点：
    1. 文字默认不可见，`:hover` / `:focus-visible` 时才显示；
    2. 文字必须**绝对定位**——否则它一出现就把按钮撑大、
       把相邻的小圆推开（这是布局上的硬约束，不是风格选择）。
    """
    code = _style_css_without_comments()

    label = _rule(".wheel-label", code)
    assert label is not None, "找不到 .wheel-label 规则"
    assert "position: absolute" in label, (
        "文字必须绝对定位，否则浮出时会撑大按钮、推开相邻小圆"
    )
    assert "opacity: 0" in label, "文字应默认隐藏"
    assert "visibility: hidden" in label, "文字应默认隐藏（同时压掉命中）"

    # 悬停/聚焦时才显示
    assert re.search(
        r"\.wheel-item:hover\s+\.wheel-label[\s\S]{0,120}visibility:\s*visible", code
    ), "鼠标停在图标上时应显示文字"
    assert re.search(
        r"\.wheel-item:focus-visible\s+\.wheel-label", code
    ), "键盘聚焦时也应显示文字（不只是鼠标）"

    # 文字不该接收指针事件，免得挡住旁边的小圆
    assert "pointer-events: none" in label, "文字不应拦截指针事件"


def test_frontend_does_not_render_tool_call_chips() -> None:
    """第 15 步：聊天里不显示"调用了什么工具"。

    工具仍会被自动调用，只是不再向用户标注。
    这里断言具体的代码痕迹，而不是搜中文关键词——
    注释里出现同样的字眼会造成误报（已经因此绊过一次）。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # 原来的实现是 addCalls() 建 .call chip、读 tool_name 后插进 chatLog
    assert "addCalls" not in js, "不该再渲染工具调用标记"
    assert "tool_name" not in js, "前端不该再读工具名用于展示"

    code = _style_css_without_comments()
    assert not re.search(r"\.calls\s*\{", code), "工具调用标记的样式应已清理"
    assert not re.search(r"\.call\s*\{", code), "工具调用标记的样式应已清理"

    # 后端接口照旧返回该字段，前端只是不渲染；注释里保留说明便于日后调试
    assert "tool_invocations" in js


def test_pet_is_draggable_and_clamped() -> None:
    """第 17 步：宠物可拖拽，且不会超出屏幕。

    这里的断言偏"结构性"，因为真正的坐标计算发生在浏览器里。
    static/_verify_ui.html 第 65 步已删除 —— 它只是开发用的自检页，却随产品
    一起发布了（StaticFiles 挂在根路径，任何人 GET /_verify_ui.html 都能拿到）。
    行为验证改由 scripts/verify_*.js 在 Node + DOM 桩里跑。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    code = _style_css_without_comments()

    # 1) 位置走 CSS 变量，未设置时回落 50%（即默认居中，保持第 4 步行为）
    pet_rule = _rule(".pet", code)
    assert pet_rule, "找不到 .pet 规则"
    pet_body = pet_rule
    assert "var(--pet-x, 50%)" in pet_body, "水平位置应走 --pet-x，默认 50%"
    assert "var(--pet-y, 50%)" in pet_body, "垂直位置应走 --pet-y，默认 50%"
    assert "translate(-50%, -50%)" in pet_body, "应以自身中心对齐定位点"
    # 老的居中写法要彻底移除，否则两套定位会打架
    assert "place-items: center" not in pet_body
    assert "inset: 0" not in pet_body

    # 2) 拖拽用 Pointer Events（同时兼容鼠标与触摸）
    for name in (
        "onPetPointerDown",
        "onPetPointerMove",
        "onPetPointerUp",
        "clampPosition",
        "applyPosition",
    ):
        assert f"function {name}" in js, f"缺少拖拽函数 {name}"

    # 3) 必须有位移阈值 + 抑制 click，否则"拖一下"会顺带弹出菜单；
    #    且抑制必须靠时间窗，不能用"一次性标志"——标志若没被消费掉，
    #    会把下一次普通点击也吞掉（点图标没反应，已踩过）
    assert "DRAG_THRESHOLD" in js, "缺少拖拽阈值：手抖会被误判成拖拽，菜单点不出来"
    assert "shouldSuppressClick" in js, "拖拽后应抑制紧随其后的 click"
    assert "CLICK_SUPPRESS_MS" in js, "抑制 click 应限定时间窗"
    assert "state.suppressClick" not in js, (
        "不要用一次性标志位抑制点击，会吞掉后续的正常点击"
    )

    # 4) 位置要能持久化，并在读取时重新约束
    assert "localStorage" in js, "位置应持久化"
    assert "window.addEventListener('resize'" in js, "窗口缩放后应重新约束位置"

    # 5) 初始位置不能用 getBoundingClientRect 量——布局未完成时它会返回全 0，
    #    导致第一次拖拽"飞向左上角"。尺寸要从 CSS 变量读，并带兜底。
    assert "readSizeVar" in js, "尺寸应从 CSS 变量读取"
    assert "PET_FALLBACK_W" in js and "PET_FALLBACK_H" in js, "量不到尺寸时要有兜底"
    assert "function clampToBounds" in js, "约束逻辑应做成可单独验证的纯函数"
    for line in js.splitlines():
        stripped = line.strip()
        if "getBoundingClientRect" in line and not stripped.startswith(("*", "//")):
            raise AssertionError(
                f"拖拽逻辑不应依赖 getBoundingClientRect（布局未完成时返回 0）：{stripped}"
            )

    # 6) 触摸拖拽不能被浏览器当成滚动
    body_rule = _rule(".pet-body", code)
    assert body_rule, "找不到 .pet-body 规则"
    assert "touch-action: none" in body_rule, "触摸拖动需要 touch-action: none"
    assert "cursor: grab" in body_rule, "应提示可抓取拖动"


def test_pet_body_does_not_redeclare_size() -> None:
    """按钮尺寸只在 .pet 上声明一处。

    两处都写 width/height 的话，日后改动容易只改一处，
    出现"盒子与按钮不一样大"，进而影响拖拽边界计算。
    """
    code = _style_css_without_comments()
    body = _rule(".pet-body", code)
    assert body is not None, "找不到 .pet-body 规则"
    assert "width: var(--pet-w)" not in body, "尺寸应由父层 .pet 决定，不要重复声明"
    assert "height: var(--pet-h)" not in body, "尺寸应由父层 .pet 决定，不要重复声明"
    assert "inset: 0" in body, "按钮应撑满父层"


def test_narrow_screen_resizes_pet_via_variables() -> None:
    """窄屏要做等比缩小，且通过变量覆盖而不是另写一套写死的数值。"""
    code = _style_css_without_comments()
    media = re.search(r"@media \(max-width: 560px\)\s*\{([\s\S]*)\n\}", code)
    assert media, "找不到窄屏媒体查询"
    body = media.group(1)
    assert "--pet-w" in body and "--pet-h" in body, (
        "窄屏应通过变量覆盖来缩小按钮"
    )


def test_page_contains_no_label_text() -> None:
    """第 3 步的约束：页面上不加任何常驻文字。

    第 209 步删掉侧边栏之后，初始可见文字**一条都没有**了 ——
    屏幕上只剩桌宠本体，任何文字都要用户点一下才会出现。
    """
    html = _index_html()
    visible = _visible_text(html)
    assert visible == "", f"初始可见文字应为空，实际是：{visible!r}"

    # 整份文档的文本节点只允许两种：按钮符号，或"藏起来"的界面文案
    collector = _TextCollector()
    collector.feed(html)

    # 允许新增**图标**（第 95 步加了清空的 🗑），但不允许新增任何文案 ——
    # 这条约束的本意是"页面上别出现常驻文字"，不是"按钮不许加"。
    _SYMBOLS = r"[☰×↑🐾👤🗑\s]"
    symbols = {text for text in collector.parts if re.sub(_SYMBOLS, "", text) == ""}
    assert "×" in symbols, "收起按钮的符号丢了"
    assert "↑" in symbols, "发送按钮的符号丢了"

    # 其余文字必须落在「默认隐藏」的区域里。
    # 用"从开标签到下一个常驻区块"来切分，而不是去数 </div>——
    # 数闭合标签的写法每加一个小窗就会漏（出行窗口就是这么被漏掉的）。
    hidden_regions = _hidden_region_texts(
        html,
        (
            ('<div', 'id="travel-modal"'),
            ('<div', 'id="mail-modal"'),
            ('<div', 'id="recipe-modal"'),
            ('<div', 'id="checkin-modal"'),
            ('<div', 'id="model-modal"'),
        ),
        stop_markers=('<div class="pet"', '<section class="chat"'),
    )

    outside = [
        text
        for text in collector.parts
        if text not in hidden_regions
        and re.sub(_SYMBOLS, "", text) != ""
    ]
    assert outside == [], f"常驻区域出现了多余文字：{outside}"


def _hidden_region_texts(
    html: str,
    regions: tuple[tuple[str, str], ...],
    stop_markers: tuple[str, ...],
) -> set[str]:
    """取出这些"默认隐藏"区块里的文本。

    regions 每项是 (开标签, 定位用属性)，从开标签切到**下一个常驻区块**为止——
    比数 </div> 稳，也不会因为新加一个窗口而漏算。
    """
    texts: set[str] = set()
    for open_tag, anchor in regions:
        start = html.index(anchor)
        start = html.rindex(open_tag, 0, start)
        end = len(html)
        for marker in stop_markers:
            pos = html.find(marker, start)
            if pos != -1:
                end = min(end, pos)
        collector = _TextCollector()
        collector.feed(html[start:end])
        texts |= set(collector.parts)
    return texts


# ----------------------------------- 侧边栏已删除（第 209 步，用户要求只留桌宠本体）

_SIDEBAR_HTML_TOKENS = (
    'id="sidebar"',
    'id="sidebar-toggle"',
    'class="menu-toggle"',
    'id="about-modal"',
    'id="settings-modal"',
    'id="login-modal"',
)
_SIDEBAR_JS_TOKENS = (
    "openSidebar",
    "closeSidebar",
    "sidebarOpen",
    "LOGIN_CREDENTIALS",
    "sidebarUser",
)
_SIDEBAR_CSS_RULES = (
    ".sidebar",
    ".sidebar-tab",
    ".menu-toggle",
    ".login-form",
    ".modal-box--compact",
    ".modal-avatar",
)


def test_sidebar_and_its_dialogs_are_gone() -> None:
    """侧边栏整条删掉，连它那三个弹窗一起（第 209 步）。

    这条测试是**反过来**钉的：删掉的是布局，不是需求 ——
    所以用测试记住"它不该长回来"，比留在注释里更管用。
    真要恢复，得先改这里，改的时候自然会想起当初为什么删。
    """
    html = _index_html()
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    code = _style_css_without_comments()

    for token in _SIDEBAR_HTML_TOKENS:
        assert token not in html, f"{token} 应已随侧边栏删除"
    for token in _SIDEBAR_JS_TOKENS:
        assert token not in js, f"app.js 里不该再有 {token}"
    for rule in _SIDEBAR_CSS_RULES:
        assert _rule(rule, code) is None, f"style.css 里不该再有 {rule} 规则"


def test_pet_keeps_every_sub_feature_after_the_sidebar_removal() -> None:
    """删侧边栏不能顺手删功能：轮盘、聊天、五个窗口、动作菜单都还得在。

    「只要桌宠本体＋它的几个子功能」——这一条守的是后半句。
    """
    html = _index_html()
    for element_id in (
        "pet",
        "menu",
        "chat",
        "travel-modal",
        "mail-modal",
        "recipe-modal",
        "checkin-modal",
        "model-modal",
        "action-menu",
        "pet-note",
    ):
        assert f'id="{element_id}"' in html, f"少了 {element_id}，功能被误删"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in ("openDialog", "closeDialog", "openMenu", "openActionMenu"):
        assert f"function {name}(" in js, f"app.js 里少了 {name}"


def test_dialogs_use_hidden_attribute_convention() -> None:
    """小窗靠 [hidden] 规则隐藏，且不该自己写 display 去对抗它。"""
    code = _style_css_without_comments()
    # 第 5 步加的全局面板隐藏规则必须仍在
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", code)
    assert rule and "!important" in rule.group(1), "[hidden] 强制隐藏规则丢失"

    # 第 209 步删掉了「关于」「设置」两个只读小窗，所以这里改看日常在用的那个
    # 骨架（.modal）。它是遮罩层，display:flex 是它本职，不算"对抗 [hidden]"；
    # 真正要守的是上面那条 !important —— 没有它，任何 display 都会赢过 [hidden]。
    overlay = _rule(".modal", code)
    assert overlay, "找不到小窗遮罩样式 .modal"


# --------------------------------- 快捷胶囊（第 23 步起，第 24 步改语义为"打开功能"）


def test_chips_container_sits_above_the_composer() -> None:
    """胶囊行要在输入框上方：先于 composer 出现在 HTML 里，且默认隐藏。"""
    html = _index_html()
    tag = re.search(r"<div[^>]*id=\"chat-chips\"[^>]*>", html)
    assert tag, "找不到快捷胶囊容器 chat-chips"
    assert "hidden" in tag.group(0), "对话没打开时不该占地方，默认必须 hidden"

    assert html.index('id="chat-chips"') < html.index('id="composer"'), (
        "胶囊应在输入框上方（DOM 顺序在前）"
    )
    # 它属于对话面板内部
    chat = re.search(r"<section[^>]*id=\"chat\"[\s\S]*?</section>", html).group(0)
    assert 'id="chat-chips"' in chat, "胶囊应放在对话面板里"


def test_chips_are_the_three_agreed_entries() -> None:
    """第 24 步：胶囊固定为 出行 / 邮箱 / 食谱，顺序不变。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    entries = re.search(r"const CHIP_ENTRIES = \[([\s\S]*?)\];", js)
    assert entries, "找不到胶囊条目表 CHIP_ENTRIES"
    body = entries.group(1)

    labels = re.findall(r"label:\s*'([^']+)'", body)
    assert labels == ["出行", "邮箱", "食谱"], f"胶囊文案/顺序不对：{labels}"

    # 三个胶囊各自都有自己的窗口：出行（第 24 步）/ 邮箱（第 33 步）/ 食谱（第 37 步）
    assert "panel: 'travel'" in body, "「出行」应关联出行窗口"
    assert "panel: 'mail'" in body, "「邮箱」应在第 33 步接上邮箱窗口"
    assert "panel: 'recipe'" in body, "「食谱」应在第 37 步接上食谱窗口"
    assert "panel: null" not in body, (
        "三个胶囊现在都能打开窗口，不该再有 disabled 占位项"
    )


def test_chips_with_a_panel_open_a_window() -> None:
    """带 panel 的胶囊打开对应窗口；没有 panel 的做成 disabled 占位。

    第 24 步只有「出行」能开；第 33 步接上「邮箱」；第 37 步接上「食谱」，
    于是三个胶囊现在**都能开窗**。

    那个 disabled 分支仍然保留在 renderChips 里：它是这张条目表的通用规则，
    将来再加占位项时不必重新想一遍——**"不能点就别装成能点"**（第 24 步的规矩）。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    chips = re.search(r"function renderChips\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert chips, "找不到 renderChips 函数"
    body = chips.group(1)

    assert "entry.panel" in body, "应按条目是否带 panel 决定行为"
    assert "openDialog(entry.panel)" in body, "带 panel 的胶囊应打开对应窗口"
    assert "disabled = true" in body, "占位胶囊应标成不可点"

    # 第 24 步明确：胶囊不再直接发消息
    assert "sendMessage" not in body, "胶囊不该再直接发送消息（第 24 步改了行为）"


def test_the_recipe_chip_opens_the_recipe_window() -> None:
    """第 37 步：胶囊表里最后那个占位项接上了自己的窗口。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    dialogs = re.search(r"function dialogEls\(kind\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "kind === 'recipe'" in dialogs, "dialogEls 要认识 recipe"
    assert "el.recipeModal" in dialogs, "recipe 应指向食谱窗口"

    open_dialog = re.search(r"function openDialog\(kind\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "kind === 'recipe'" in open_dialog
    assert "prepareRecipe()" in open_dialog, "打开食谱窗口时应先取 schema 与恢复会话"


def test_chips_are_not_driven_by_tools_anymore() -> None:
    """胶囊不再由 /api/tools 驱动：它变成"打开功能"的入口，不再是工具的快捷方式。

    注意：轮盘项仍然由后端工具驱动（intentOf 就是给它用的），这一点不能跟着删——
    第 24 步改的只是胶囊。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "chipTools" not in js, "不该再按工具分流渲染胶囊"
    assert "tool.intent" not in js.split("function renderChips")[1].split("function ")[0], (
        "胶囊不该再读取工具字段"
    )

    # 轮盘侧的后端驱动机制要保留
    wheel = re.search(r"function renderWheel\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert wheel, "找不到 renderWheel 函数"
    assert "intentOf(tool)" in wheel.group(1), "轮盘项应继续使用后端提供的 intent"

    load = re.search(r"async function loadTools\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert load, "找不到 loadTools 函数"
    # loadTools 只管轮盘；胶囊是固定条目，不该等接口回来才出现
    assert "renderChips()" not in load.group(1), "胶囊不该依赖接口返回"
    assert "renderWheel()" in load.group(1), "loadTools 仍应刷新轮盘"


def test_chips_collapse_while_typing() -> None:
    """打字时收起——条件是"用户在输入"，不再依赖"有没有芯片"。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    update = re.search(r"function updateChips\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert update, "找不到 updateChips 函数"
    body = update.group(1)

    assert "value.trim()" in body, "用户打字后应收起胶囊"
    assert "el.chatChips.hidden" in body, "显隐要落到 hidden 属性上"
    # 输入事件要真的接上，否则打字不会收起
    assert re.search(r"on\(el\.input, 'input', updateChips\)", js), (
        "输入框没有绑定收起逻辑"
    )


def test_chips_are_styled_as_pill_buttons() -> None:
    """胶囊是圆角胶囊，且不换行；占位态有明确的禁用样式。"""
    code = _style_css_without_comments()

    chips = _rule(".chips", code)
    assert chips, "找不到 .chips 规则"
    assert "flex" in chips, ".chips 应是 flex 容器"

    chip = _rule(".chip", code)
    assert chip, "找不到 .chip 规则"
    assert "white-space: nowrap" in chip, "胶囊文字不该被折行"
    assert "border-radius" in chip, "胶囊应是圆角胶囊"

    disabled = _rule(".chip:disabled", code)
    assert disabled, "占位胶囊应有禁用样式，否则用户以为能点"
    assert "cursor" in disabled, "禁用态应把光标改回默认"


def test_travel_modal_has_two_tab_pages() -> None:
    """出行窗口的常驻页：聊聊 / 自己填，外加一个按需出现的「计划概览」（第 25、216 步）。

    第 216 步把「填写信息」改名为「自己填」，并把默认页让给「聊聊」——
    所以这里不再假设"第一个标签就是表单页"，而是断言两者都在、且聊天页排在前。
    """
    html = _index_html()
    tag = re.search(r"<div[^>]*id=\"travel-modal\"[^>]*>", html)
    assert tag, "找不到出行窗口"
    assert "hidden" in tag.group(0), "出行窗口默认必须隐藏"
    assert 'id="travel-close"' in html, "出行窗口需要 × 关闭按钮（与其它小窗一致）"

    for marker in (
        'id="travel-tab-chat"', 'id="travel-page-chat"',
        'id="travel-tab-form"', 'id="travel-page-form"',
        'id="travel-tab-plan"', 'id="travel-page-plan"',
    ):
        assert marker in html, f"出行窗口缺少 {marker}"

    assert ">聊聊<" in html, "第一页标签应叫「聊聊」（第 216 步）"
    assert ">自己填（可选）<" in html, "表单页标签应叫「自己填（可选）」（第 220 步）"
    assert ">计划概览<" in html, "第二个标签应叫「计划概览」"
    assert html.index('id="travel-tab-chat"') < html.index('id="travel-tab-form"'), (
        "「聊聊」要排在「自己填」前面（默认入口）"
    )

    plan_tag = re.search(r"<section[^>]*id=\"travel-page-plan\"[^>]*>", html)
    assert plan_tag and "hidden" in plan_tag.group(0), "计划概览页默认必须隐藏"

    # 第 27/30 步：这个标签**默认不出现**，点过「确定」之后才显示出来
    # （用户要求的语义：像浏览器点链接才开新标签页）
    tab_plan = re.search(r"<span[^>]*id=\"travel-tab-plan\"[^>]*>", html)
    assert tab_plan and "hidden" in tab_plan.group(0), (
        "「计划概览」标签默认必须隐藏，点过确定才出现"
    )
    # 它必须带一个独立的关闭按钮（点 × 只关标签，不切页）
    close = re.search(r"<button[^>]*id=\"travel-tab-plan-close\"[^>]*>", html)
    assert close, "「计划概览」标签缺少关闭按钮"

    # 标签与面板要互相关联，读屏和自动化才认得出
    assert 'aria-controls="travel-page-form"' in html
    assert 'aria-controls="travel-page-plan"' in html


def test_travel_has_overview_and_day_pages() -> None:
    """第 28 步：概览页版式 + 「第x天」详情页，标签点过才出现。"""
    html = _index_html()

    for marker in (
        'id="travel-overview"',          # 概览版式容器
        'id="overview-title"',
        'id="overview-dates"',
        'id="overview-advice"',
        'id="day-list"',                 # 每日行程列表
        'id="travel-page-day"',          # 详情页
        'id="day-title"',
        'id="day-body"',
        'id="travel-tabs"',              # 标签栏容器（「第x天」标签由 app.js 动态插入）
    ):
        assert marker in html, f"缺少 {marker}"

    # 第 30 步起：每天一个独立标签、动态插入，所以没有静态的 travel-tab-day
    assert 'id="travel-tab-day"' not in html, (
        "「第x天」标签应动态创建（每天一个），不该是静态元素"
    )

    # 「计划概览」标签默认隐藏（点过某一天/确定才出现）
    tab_plan = re.search(r"<span[^>]*id=\"travel-tab-plan\"[^>]*>", html)
    assert tab_plan and "hidden" in tab_plan.group(0), "「计划概览」标签默认必须隐藏"

    # 用户明确不要的三样东西：返回首页 / 编辑行程 / 导出行程（概览页顶部）
    overview = re.search(r"<div[^>]*id=\"travel-overview\"[\s\S]*?</section>\s*</div>", html)
    assert overview, "找不到概览版式块"
    assert "返回首页" not in overview.group(0), "概览页不该有「返回首页」"
    assert "编辑行程" not in overview.group(0), "概览页不该有「编辑行程」"
    assert "导出行程" not in overview.group(0), "概览页不该有「导出行程」"


def test_spot_photos_prefer_unsplash_and_fall_back_to_amap() -> None:
    """配图：Unsplash 优先，没有再退高德；**署名必须按来源分开**（第 43 步）。

    Unsplash 的署名是条款硬要求（摄影师 + 链回主页 + 链回 Unsplash）；
    高德的图如果挂上 Unsplash 的署名，那就是**假署名**，所以两条路要分开断言。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "${API}/photo?query=" in js, "配图应先走 /api/photo（Unsplash）"
    assert "photoCache" in js, "应有配图缓存，避免重复请求"

    attach = re.search(r"(async function attachSpotPhoto\([\s\S]*?\n  \})", js)
    assert attach, "找不到 attachSpotPhoto"
    body = attach.group(1)

    # 第一条路：Unsplash
    assert "renderSpotFigure(card, decision)" in body, "应把渲染收口到一个函数里"
    assert "source: 'unsplash'" in body, "先试 Unsplash"
    # 第二条路：高德自带照片
    assert "spot.amap_photos" in body, "Unsplash 没有时要退到高德自带照片"
    assert "source: 'amap'" in body
    # 顺序：先 Unsplash、再高德、最后才是占位
    assert body.index("source: 'unsplash'") < body.index("spot.amap_photos"), (
        "Unsplash 必须优先于高德"
    )
    assert body.index("spot.amap_photos") < body.index("spot-photo-placeholder"), (
        "要先试高德兜底，再退回占位"
    )
    # 两边都没有 -> 占位而不是空白（第 32 步的教训），并且必须留痕
    assert "spot-photo-placeholder" in body and "暂无图片" in body
    # 日志在取候选的那一步（第 44 步把取图拆成了 loadPhotoCandidates）
    loader = re.search(r"(async function loadPhotoCandidates\([\s\S]*?\n  \})", js)
    assert loader, "找不到 loadPhotoCandidates"
    assert "console.warn" in loader.group(1), "拿不到图必须留日志，否则排查摸黑"

    figure = re.search(r"(function renderSpotFigure\([\s\S]*?\n  \})", js)
    assert figure, "找不到 renderSpotFigure"
    fb = figure.group(1)
    # Unsplash 分支：条款要求的署名
    assert "photo.author" in fb, "应渲染摄影师名"
    assert "photo.author_url" in fb, "摄影师名应链回其主页"
    assert "photo.source_url" in fb, "应链回 Unsplash"
    # 高德分支：不能挂 Unsplash 的署名
    assert "图片来源：高德地图" in fb, "高德的图要标来源为高德"
    assert "figure.dataset.source" in fb, "要标出来源，验证与统计都靠它"


def test_day_detail_page_has_no_edit_or_export_buttons() -> None:
    """详情页顶部**不该**再有「编辑行程」「导出行程」（第 46 步）。

    第 28 步放了两个 disabled 占位按钮；用户要求删掉——
    两个功能都没做，留着只是噪音（也不符合第 24 步"不能点就别装成能点"的规矩）。
    """
    html = _index_html()
    page = re.search(r"<section[^>]*id=\"travel-page-day\"[\s\S]*?</section>", html)
    assert page, "找不到详情页"
    body = page.group(0)

    assert "编辑行程" not in body, "详情页不该再有「编辑行程」"
    assert "导出行程" not in body, "详情页不该再有「导出行程」"
    for button_id in ("day-edit", "day-export"):
        assert f'id="{button_id}"' not in body, f"{button_id} 应已删除"
    # 页面里也不该再留它们的 el 映射（el 只映射真实存在的元素）
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "dayEdit" not in js and "dayExport" not in js, "app.js 里不该再有这两个元素的映射"


def test_day_entries_are_rendered_from_data_and_clickable() -> None:
    """每日行程由**后端返回的结构化行程**渲染（第 41 步）。

    这条测试以前断言的是"DAY_CONTENT_POOL 存在、且内容标注了是假数据"——
    也就是说，测试在**保护那份假数据**。现在反过来：假数据池必须不存在，
    渲染必须只认 state.trip，且没有数据源的字段一个都不许出现。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # 两套真相源必须彻底消失
    assert "DAY_CONTENT_POOL" not in js, "写死的示例内容池必须删掉"
    assert "DEMO_HOTEL" not in js, "示例酒店必须删掉（项目没有酒店数据源）"
    assert "contentForDay" not in js, "按天循环取假内容的函数必须删掉"

    render = re.search(r"(function renderOverview\(\)[\s\S]*?\n  \})", js)
    assert render, "找不到 renderOverview"
    assert "state.trip" in render.group(1), "概览必须读后端返回的行程"
    assert "day-item" in render.group(1), "每日行程条目应在 renderOverview 里生成"

    detail = re.search(r"(function renderDayDetail\([\s\S]*?\n  \})", js)
    assert detail, "找不到 renderDayDetail"
    text = detail.group(1)
    assert "spot-grid" in text, "详情页缺少景点区"
    assert "meal-table" in text, "详情页缺少餐饮区"

    # 没有数据源的字段不许渲染：门票价、景点描述
    assert "spot.cost" not in text, "门票价没有数据源，不该渲染"
    assert "spot.desc" not in text, "景点描述没有数据源，不该渲染"

    # 住宿现在是真实数据（高德住宿类 POI，第 43 步），但价格与星级高德不提供
    assert "hotel-card" in text, "住宿候选应渲染"
    assert "hotel.price" not in text, "价格没有数据源，不许出现"
    assert "hotel.star" not in text, "星级没有数据源，不许出现"
    assert "hotel.rating" in text, "评分是高德给的真实值，应显示"

    # 估算值与真实值必须分得开
    assert "（估算）" in text, "游览时长是估算，必须标明"
    assert "spot.opentime" in text, "营业时间是高德给的真实值，应显示"

    # 当天的校验问题要摆出来，不能藏
    assert "day.issues" in text, "当天的校验问题必须显示"
    assert "day-issues" in text, "问题要以列表形式渲染出来"
    # 没有餐厅（或没排到景点）的那天要给说明，而不是一张空表格
    assert "day-empty" in text, "没有餐厅/没排到景点的那天要给出说明"

    # 景点卡片要触发配图（第 29 步）；城市来自后端行程而不是示例数据
    assert "attachSpotPhoto(card, spot, trip.city)" in text, "景点卡片没有触发配图"

    # 点击走事件委托（重新渲染后监听器不会失效）
    assert re.search(r"on\(el\.dayList, 'click'", js), "每日行程列表没有接上点击"
    assert "closest('.day-item')" in js, "应按 closest 找到被点的那一天"
    assert "openDayTab(" in js, "点某一天应打开那一天的标签页"


# ------------------------ 日期/天数、后端数据源与标签页（第 30 步起，第 41 步改为后端算）


def test_plan_days_come_from_the_backend_not_the_frontend() -> None:
    """天数与日期由后端算，前端不再自己推（第 41 步）。

    第 30 步修的是"前端算的天数和用户填的对不上"；第 41 步把它整个挪到后端
    （core/itinerary.build_trip_dates）——前端算一份、后端也算一份，
    正是"两套真相源"的老毛病，迟早会不一致。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function collectPlan" not in js, "前端的行程拼装函数应已删除"
    assert "function shiftDate" not in js, "前端不该再自己推日期"

    # 写死的标题/日期/旧数据都不能回来
    assert "北京旅行计划" not in js, "标题不该写死"
    assert "2025-11-03" not in js, "日期不该写死"
    assert "TRAVEL_DEMO" not in js, "旧的写死数据应已移除"

    # 提交给后端的字段名必须与 api/trip_routes.py 的请求模型一致
    collect = re.search(r"(function collectTravelForm\(\)[\s\S]*?\n  \})", js)
    assert collect, "找不到 collectTravelForm"
    body = collect.group(1)
    for field in ("session_id", "city", "start_date", "end_date",
                  "transport", "stay", "preferences", "note"):
        assert f"{field}:" in body, f"提交字段缺少 {field}"

    # 出行有自己的会话 id（与聊天分开，照食谱 pet.recipe_session 的做法）
    assert "pet.trip_session" in js, "出行需要独立的会话 id"


def test_overview_shows_the_backend_dates_and_day_count() -> None:
    """概览页显示的日期与天数取自**后端返回的行程**。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    render = re.search(r"(function renderOverview\(\)[\s\S]*?\n  \})", js)
    assert render, "找不到 renderOverview"
    body = render.group(1)
    assert "state.trip" in body, "概览应使用后端返回的行程"
    assert "trip.start_date" in body and "trip.end_date" in body, "日期应来自后端行程"
    assert "trip.days.length" in body, "天数应来自后端行程"


def test_day_tabs_are_kept_and_closable() -> None:
    """第 30 步 bug 3：打开的「第x天」标签要保留，并且能主动关掉。

    以前每天共用一个标签槽，点第 2 天就把第 1 天顶掉了。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # 每天一个独立标签，存在 state.dayTabs 里（不再共用一个元素）
    assert "dayTabs" in js, "应有按天保存标签的容器"
    open_fn = re.search(r"(function openDayTab\([\s\S]*?\n  \})", js)
    assert open_fn, "找不到 openDayTab"
    assert "state.dayTabs[index]" in open_fn.group(1), "同一天应复用已有标签"
    assert "el.travelTabs.append(tab)" in open_fn.group(1), "新标签应插进标签栏（旧的保留）"

    # 每个标签带一个独立的 ×（点它只关标签，不切页）
    make = re.search(r"(function makeTravelTab\([\s\S]*?\n  \})", js)
    assert make, "找不到 makeTravelTab"
    body = make.group(1)
    assert "tab-close" in body and "onClose" in body, "标签应带关闭按钮"
    assert "'×'" in body, "关闭按钮应是 ×"
    # 「填写信息」不可关闭
    assert "closeFormTab" not in js, "「填写信息」不该有关闭逻辑"

    close_fn = re.search(r"(function closeDayTab\([\s\S]*?\n  \})", js)
    assert close_fn, "找不到 closeDayTab"
    assert "removeChild" in close_fn.group(1) or ".remove()" in close_fn.group(1), (
        "关闭标签要把按钮从标签栏移除"
    )
    # 关掉当前页时要退到别处，不能停在空白页
    assert "showTravelPage(" in close_fn.group(1), "关掉当前标签后应切到别的页"


def test_travel_form_has_the_agreed_fields() -> None:
    """第一页字段按第 25 步的约定：目的地/日期/天数/交通/住宿/偏好/额外要求。"""
    html = _index_html()
    for field_id in (
        "travel-city",
        "travel-start",
        "travel-end",
        "travel-days",
        "travel-transport",
        "travel-stay",
        "travel-note",
        "travel-submit",
    ):
        assert f'id="{field_id}"' in html, f"出行表单缺少字段：{field_id}"

    # 旅行天数是算出来的输出，不是可编辑输入框
    days_tag = re.search(r"<output[^>]*id=\"travel-days\"[^>]*>", html)
    assert days_tag, "旅行天数应是一个 <output>（只读的自动计算结果）"

    # 旅行偏好六个多选项
    values = re.findall(r'<input type="checkbox" id="pref-[^"]+" value="([^"]+)"', html)
    assert values == ["历史文化", "自然风光", "美食", "购物", "艺术", "休闲"], (
        f"旅行偏好的选项不对：{values}"
    )


def test_travel_origin_sits_left_of_the_destination_and_stays_optional() -> None:
    """第 231 步：出发地在目的地**左边**，且是可选字段（不确定就不填）。"""
    html = _index_html()
    origin_at = html.find('id="travel-origin"')
    city_at = html.find('id="travel-city"')
    assert origin_at > 0, "出行表单缺少出发地字段"
    assert city_at > 0, "出行表单缺少目的地字段"
    assert origin_at < city_at, "出发地要排在目的地左边"

    # 可选：不能有 required（否则浏览器会拦住不填出发地的用户）
    origin_tag = re.search(r"<input[^>]*id=\"travel-origin\"[^>]*>", html)
    assert origin_tag, "找不到出发地输入框"
    assert "required" not in origin_tag.group(0), "出发地是可选字段，不能加 required"

    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "travelOrigin: document.getElementById('travel-origin')" in app_js
    assert "origin_city: (el.travelOrigin" in app_js, "提交表单时要带上 origin_city"
    assert "origin_city" in (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_travel_advice_mentions_the_origin_only_when_given() -> None:
    """概览「建议」在有出发地时先说「从X出发」；没填就一个字都不多说。"""
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function adviceText\(trip\)\s*\{([\s\S]*?)\n  \}", app_js)
    assert match, "找不到 adviceText"
    body = match.group(1)
    assert "trip.origin_city" in body, "建议里要读 origin_city"
    assert "if (origin)" in body, "没填出发地时不能凭空写"


def test_each_day_shows_a_forecast_labelled_as_forecast() -> None:
    """第 234 步：逐日预报在概览与详情里都要写明「预报」，没数据就整段不出现。"""
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function forecastText(forecast)" in app_js, "预报文案要单独一个函数"
    assert "`预报 ${bits.join(' ')}`" in app_js, "生成的句子必须带「预报」两个字"
    assert "if (!bits.length) return '';" in app_js, "没数据要返回空串，不显示占位"

    overview = re.search(r"function renderOverview\(\)\s*\{([\s\S]*?)\n  \}", app_js)
    assert overview, "找不到 renderOverview"
    assert "day.forecast" in overview.group(1), "概览的每一天要读自己的 forecast"

    detail = re.search(r"function renderDayDetail\(index\)\s*\{([\s\S]*?)\n  \}", app_js)
    assert detail, "找不到 renderDayDetail"
    assert "不是实况" in detail.group(1), "详情里要明说这不是实况"


def test_leg_text_shows_tolls_only_when_positive() -> None:
    """第 235 步：过路费只在驾车且 >0 时出现；0 元和「没查到」都不显示。"""
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function legText\(leg\)\s*\{([\s\S]*?)\n  \}", app_js)
    assert match, "找不到 legText"
    body = match.group(1)
    assert "leg.tolls" in body, "通行段要读过路费"
    assert "leg.tolls > 0" in body, "0 元不是一条信息，不能显示"
    assert "mode === 'driving'" in body, "过路费只对驾车口径有意义"
    assert "过路费约" in body


def test_travel_form_is_grouped_into_cards() -> None:
    """第一页按参考图分成三块卡片：目的地与日期 / 偏好设置 / 额外要求（第 26 步）。"""
    html = _index_html()
    form = re.search(r"<form[^>]*id=\"travel-form\"[\s\S]*?</form>", html)
    assert form, "找不到出行表单"
    body = form.group(0)

    assert body.count('class="tcard"') == 3, "第一页应有三块卡片"
    for title in ("目的地与日期", "偏好设置", "额外要求"):
        assert title in body, f"缺少卡片标题：{title}"


def test_the_extras_field_does_not_promise_what_the_code_cannot_do() -> None:
    """「额外要求」是自由文本，后端只如实记录、不拿它筛景点（core/itinerary.py:492）。

    所以占位符也不能举「想去看升旗 / 需要无障碍设施」这种**像过滤器**的例子 ——
    那是在承诺代码做不到的事（评价15 T3-12 / B3）。
    第 262 步按用户要求清掉灰色说明行后，「只记进备注、不参与筛选」这两句
    收进占位符里（占位符不算「灰色小字提醒」），保证不退回"写了以为生效"。
    """
    html = _index_html()
    match = re.search(r'id="travel-note"[\s\S]{0,400}?</textarea>', html)
    assert match, "找不到「额外要求」输入框"
    block = match.group(0)

    assert "只记进备注" in block, "占位符要说明这句话的去向"
    assert "不参与筛选" in block, "占位符要明说它不参与筛选"
    for misleading in ("无障碍", "升旗"):
        assert misleading not in block, f"占位符不该给出「{misleading}」这种像筛选的预期"


# --------------------------- 出行窗口跟随系统主题（第 42 步）


def _dark_css() -> str:
    """所有 `prefers-color-scheme: dark` 媒体查询里的内容拼起来。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    blocks = re.findall(r"@media \(prefers-color-scheme: dark\) \{([\s\S]*?)\n\}", css)
    return "\n".join(blocks)


def test_travel_window_colors_come_from_scoped_variables() -> None:
    """出行窗口的配色必须走 `--term-*` 变量，而不是硬编码浅色（第 42 步）。

    原来这一整段写死了浅色值（`.terminal` 的注释甚至写着"不改全局变量"），
    于是系统主题是「暗」时，出行窗口还是一块亮灰色。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    terminal = _rule(".terminal", css)
    assert terminal, "找不到 .terminal 规则"
    assert "background: var(--term-bg)" in terminal, "窗口底色必须走变量"
    assert "color: var(--term-fg)" in terminal, "正文色必须走变量"

    for name in ("--term-bg", "--term-card", "--term-input", "--term-line",
                 "--term-fg", "--term-heading", "--term-muted"):
        assert f"{name}:" in terminal, f".terminal 上缺少变量定义 {name}"

    # 变量自己引用自己会让整条声明失效（改这段时真踩过一次）
    assert not re.search(r"(--term-[\w-]+):\s*var\(\1\)", css), (
        "变量不能自己引用自己"
    )


def test_travel_window_variables_flip_in_dark_theme() -> None:
    """深色主题必须覆盖同一组变量，否则"跟随系统"只是句空话。"""
    light = _rule(".terminal", (STATIC_DIR / "style.css").read_text(encoding="utf-8"))
    dark_css = _dark_css()
    dark_match = re.search(r"\.terminal\s*\{([^}]*)\}", dark_css)
    assert dark_match, "深色主题里没有覆盖 .terminal 的配色变量"
    dark = dark_match.group(1)

    for name in ("--term-bg", "--term-card", "--term-input", "--term-line",
                 "--term-fg", "--term-heading", "--term-muted"):
        found_light = re.search(rf"{name}:\s*([^;]+);", light or "")
        found_dark = re.search(rf"{name}:\s*([^;]+);", dark)
        assert found_light and found_dark, f"{name} 在两种主题里都要有值"
        assert found_light.group(1).strip() != found_dark.group(1).strip(), (
            f"{name} 在深色下必须真的换一个值，否则等于没跟随"
        )


def test_theme_declares_color_scheme_for_native_controls() -> None:
    """出行表单里有 date / select：不声明 color-scheme 的话原生日历在暗色下仍发亮。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "color-scheme: light" in css, "浅色下要声明 color-scheme"
    assert "color-scheme: dark" in _dark_css(), "深色下要声明 color-scheme: dark"


def test_travel_section_has_no_hardcoded_background_colors() -> None:
    """出行区段不许再有硬编码的十六进制背景色。

    这是"跟随系统主题"的**防回归线**：以后新加一条 `background: #eee`
    会在这里立刻暴露，而不是等用户在暗色下看到一块亮斑。
    品牌渐变（linear-gradient）与照片上的半透明遮罩不受此限。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    start = css.index("/* 出行的窗口：尺寸按用户画的框")
    end = css.index("/* ------------------------------------------------------------ 邮箱窗口 */")
    region = css[start:end]

    offenders = re.findall(r"background:\s*#[0-9a-fA-F]{3,8}", region)
    assert offenders == [], f"出行区段仍有硬编码背景色：{offenders}"


def test_travel_form_generates_via_its_own_trip_api() -> None:
    """点「确定」走 POST /api/trip，并按 status 决定渲染还是明说失败（第 41 步）。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert re.search(r"on\(el\.travelForm, 'submit', onTravelSubmit\)", js), (
        "表单没绑定提交"
    )
    assert re.search(r"on\(el\.travelStart, 'change', updateTravelDays\)", js), (
        "开始日期没绑定天数计算"
    )
    assert re.search(r"on\(el\.travelEnd, 'change', updateTravelDays\)", js), (
        "结束日期没绑定天数计算"
    )

    submit = re.search(r"(async function onTravelSubmit\([\s\S]*?\n  \})", js)
    assert submit, "找不到 onTravelSubmit"
    body = submit.group(1)
    assert "${API}/trip" in body, "出行必须走自己的 /api/trip 端点"
    assert "showTravelPage('plan')" in body, "提交后应切到计划页（进度条）"
    assert "setTravelGenerating(true)" in body, "生成期间应进入等待态"
    assert body.count("showTravelPage('form')") >= 2, "失败时应退回第一页"


def test_failed_trip_is_never_rendered_as_an_itinerary() -> None:
    """**本次改造的核心防线**：后端说 failed 时，前端一个景点都不能渲染。

    以前 LLM 失败时 Agent 返回 HTTP 200 + 错误文案，前端只看 response.ok，
    于是后端整条链路挂掉（key 错、配额用完）照样显示一份漂亮的假行程——
    静默、自信、错误。这条测试就是钉死这个行为不许回来。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    submit = re.search(r"(async function onTravelSubmit\([\s\S]*?\n  \})", js)
    assert submit, "找不到 onTravelSubmit"
    body = submit.group(1)

    assert "trip.status === 'failed'" in body, "必须显式判断 failed 状态"
    failed_at = body.index("trip.status === 'failed'")
    render_at = body.index("renderOverview()")
    assert failed_at < render_at, "failed 判断必须发生在 renderOverview 之前"

    # failed 分支必须退回第一页并说清缺了什么，而不是继续渲染
    branch = body[failed_at:render_at]
    assert "showTravelPage('form')" in branch, "failed 时应退回第一页"
    assert "degraded_reasons" in branch, "failed 时必须说明缺了什么"

    # 只有通过检查才允许把行程写进 state
    assert body.index("state.trip = trip") < render_at, (
        "只有通过 status 检查之后才允许把行程写进 state"
    )


def test_degraded_trip_shows_its_reasons_instead_of_hiding_them() -> None:
    """降级原因与校验问题必须显示在概览页上——静默降级是不允许的。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    notices = re.search(r"(function tripNotices\([\s\S]*?\n  \})", js)
    assert notices, "找不到 tripNotices"
    body = notices.group(1)
    assert "degraded_reasons" in body, "应把降级原因收集起来"
    assert "issues" in body, "应把校验问题收集起来"

    render = re.search(r"(function renderOverview\(\)[\s\S]*?\n  \})", js)
    assert "tripNotices(" in render.group(1), "概览页必须渲染这些提示"
    assert "planNotice" in render.group(1), "提示要写进 planNotice 元素"


def test_travel_does_not_use_the_chat_api_anymore() -> None:
    """出行独立于聊天：不再复用 POST /api/chat（第 41 步）。

    以前出行把表单拼成一段自然语言发给 /api/chat，于是模型自己决定调不调
    POI 工具——它跳过时不会有任何错误，只会得到一份没有真实地点的行程；
    而且 geocode+天气+POI 已经用满 agent_max_steps=4，任何重试都会失败。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "${API}/trip" in js, "出行应走自己的端点"
    assert "function buildTravelPrompt" not in js, (
        "不该再把表单拼成给模型的一段自然语言"
    )

    submit = re.search(r"(async function onTravelSubmit\([\s\S]*?\n  \})", js)
    assert "${API}/chat" not in submit.group(1), "出行提交不该打 /api/chat"


def _px(rule_body: str, prop: str) -> float | None:
    """从 CSS 规则体里取某个属性的 px 值。"""
    match = re.search(rf"{prop}\s*:\s*(-?[\d.]+)px", rule_body)
    return float(match.group(1)) if match else None


def test_api_router_registered() -> None:
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/api/chat" in paths
    assert "/api/tools" in paths


def test_settings_are_readable_without_crashing() -> None:
    """缺 key / 缺 host 时应用必须仍能启动（只是调用时才报可读错误）。"""
    settings = get_settings()
    assert settings.deepseek_base_url.startswith("http")
    assert settings.agent_max_steps >= 1


# ---------------------------------------------------------------- 初始界面状态
# 第 4 步的要求：初始状态下屏幕内只有一只居中猫咪，
# 聊天框与轮盘都必须是关闭的，并且用户要能自己关掉聊天框。


def _index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _style_css_without_comments() -> str:
    """剥掉 CSS 注释再断言。

    否则注释里提到的示例写法（例如说明 [hidden]{display:none} 的成因）
    会被正则当成真实规则匹配到，断言就失去意义——这个坑已经踩过一次。
    """
    return re.sub(r"/\*[\s\S]*?\*/", "", _style_css())


def _rule(selector: str, css: str | None = None) -> str | None:
    """取出某个选择器的规则体。

    用「行首（可含缩进）+ 选择器 + {」匹配，解决两个实际踩到的坑：
    1. **后缀误匹配**：`re.search(r"\\.pet-body\\s*\\{")` 会先命中
       `.pet.is-dragging .pet-body {`，拿到拖拽态而不是基础规则。
       负向后顾（`(?<![\\w.#-])`）救不了——`.pet-body` 前紧邻的是空格。
    2. **缩进**：媒体查询里的选择器（如窄屏的 `.menu-toggle`）带前导空格，
       若用 `^` 直接锚定选择器则完全匹配不到。
    故写成 `^[ \\t]*` 再跟选择器：既跳过缩进，又保证选择器从行首
    （忽略空白）开始，从而排除 `.a .b` 这类组合选择器。
    """
    source = _style_css_without_comments() if css is None else css
    pattern = rf"^[ \t]*{re.escape(selector)}\s*\{{([^}}]*)\}}"
    match = re.search(pattern, source, re.MULTILINE)
    return match.group(1) if match else None


class _TextCollector(HTMLParser):
    """收集文档里所有文本节点（不区分是否可见）。

    script / style / noscript / head 里的内容不算"页面文案"，跳过。
    注意：void 标签（img/meta/link/input…）没有结束标签，
    绝不能压进栈，否则 depth 永远清不归零、后续文本全被误判为不可见
    —— 这个坑已经踩过一次。
    """

    SKIP_TAGS = {"script", "style", "noscript", "head", "title"}
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._skip_stack: list[str] = []

    def _push(self, tag: str) -> None:
        if tag in self.VOID_TAGS:
            return          # 无结束标签，不入栈
        self._skip_stack.append(tag)
        self._skip_depth = len(self._skip_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            self._push(tag)
            return
        if tag in self.SKIP_TAGS:
            self._push(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """自闭合写法（如 <br />）同样不入栈。"""
        return

    def handle_endtag(self, tag: str) -> None:
        if not self._skip_depth:
            return
        # 倒着找匹配的开标签，容忍个别未闭合的标签
        for index in range(len(self._skip_stack) - 1, -1, -1):
            if self._skip_stack[index] == tag:
                del self._skip_stack[index:]
                self._skip_depth = len(self._skip_stack)
                return

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


class _VisibleTextExtractor(_TextCollector):
    """在 _TextCollector 基础上，进一步跳过带 hidden 属性的元素整棵子树。

    这正是浏览器里的结果：hidden 元素不可见，其中的文字看不见。
    """

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_depth and any(name == "hidden" for name, _ in attrs):
            self._push(tag)
            return
        super().handle_starttag(tag, attrs)


def _visible_text(html: str) -> str:
    parser = _VisibleTextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(parser.parts)


def test_chat_panel_starts_hidden() -> None:
    """初始不得显示聊天框——屏幕上应该只有猫咪。"""
    html = _index_html()
    chat_tag = re.search(r"<section[^>]*id=\"chat\"[^>]*>", html)
    assert chat_tag, "index.html 里找不到聊天面板"
    assert "hidden" in chat_tag.group(0), "聊天面板初始必须是 hidden"


def test_wheel_menu_starts_hidden() -> None:
    """初始不得显示轮盘菜单。"""
    html = _index_html()
    wheel_tag = re.search(r"<nav[^>]*id=\"menu\"[^>]*>", html)
    assert wheel_tag, "index.html 里找不到轮盘菜单"
    assert "hidden" in wheel_tag.group(0), "轮盘菜单初始必须是 hidden"


def test_chat_panel_has_close_button() -> None:
    """用户必须能主动关掉聊天框（右上角 × 按钮）。"""
    html = _index_html()
    assert 'id="chat-close"' in html, "缺少关闭按钮，用户无法关闭聊天框"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # 关闭按钮必须真的绑定了 closeChat（绑定统一走安全助手 on()）
    assert re.search(r"on\(el\.chatClose, 'click', closeChat\)", js), (
        "关闭按钮没绑定 closeChat，点了不会有反应"
    )
    # Esc 也要能关
    assert "closeChat()" in js


def test_pet_defaults_to_center_of_screen() -> None:
    """默认位置仍是屏幕正中（第 4 步的行为不能因为加拖拽而丢）。

    第 17 步把定位方式从"铺满视口 + grid 居中"改成"CSS 变量 + 自身居中"，
    但默认值仍回落到 50%，所以没拖过时依旧居中。
    """
    body = _rule(".pet")
    assert body is not None, "找不到 .pet 规则"
    assert "position: fixed" in body
    assert "var(--pet-x, 50%)" in body, "未设置位置时应回落到水平 50%"
    assert "var(--pet-y, 50%)" in body, "未设置位置时应回落到垂直 50%"
    assert "translate(-50%, -50%)" in body, "应以自身中心对齐该点"


def test_chat_panel_centering_keeps_transform() -> None:
    """聊天框定位在屏幕右侧，纵向由底部延伸到约垂直中部。

    第 5 步把它从"底部居中"改成"右侧"，
    这条断言锁定新位置，并防止有人再用 left/transform 居中把它推歪。
    """
    code = _style_css_without_comments()
    chat_rule = _rule(".chat", code)
    assert chat_rule, "style.css 里找不到 .chat 规则"
    body = chat_rule
    assert "right:" in body, "聊天框应贴屏幕右侧"
    assert "bottom:" in body, "聊天框应从底部往上排布"
    # 顶部最高到垂直中部，才符合"底部到中间"
    assert "50vh" in body, "聊天框最大高度应为 50vh（顶到约屏幕中部）"
    # 不该再用水平居中，否则会跑到屏幕中间和猫咪重叠
    assert "left: 50%" not in body


def test_hidden_attribute_is_actually_honored() -> None:
    """带 hidden 的元素必须真的不显示。

    这是第 5 步那个 bug 的回归防线：
    `.chat { display: flex }` 会压过浏览器默认的 `[hidden] { display: none }`，
    导致 hidden 属性形同虚设 —— 聊天框一直显示、点 × 也"没反应"。
    只检查 HTML 里有没有 hidden 属性是不够的（上一轮就漏在这里），
    必须同时确认 CSS 里有能兜住它的规则。
    """
    code = _style_css_without_comments()
    html = _index_html()

    # 1) 必须存在强制隐藏规则，且用 !important 保证压得住组件自己的 display
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", code)
    assert rule, "缺少 [hidden] 规则，组件自己的 display 会让 hidden 属性失效"
    assert "display:none" in rule.group(1).replace(" ", "")
    assert "!important" in rule.group(1), (
        "[hidden] 规则需要 !important —— .chat 的 display:flex 优先级更高"
    )

    # 2) 初始应隐藏的三个元素都要带 hidden 属性
    for element_id in ("chat", "menu", "bubble"):
        tag = re.search(rf"<[^>]*id=\"{element_id}\"[^>]*>", html)
        assert tag, f"index.html 里找不到 #{element_id}"
        assert "hidden" in tag.group(0), f"#{element_id} 初始必须是 hidden"

    # 3) 这些元素的组件规则里若声明了 display，也必须靠 [hidden] 兜住（上面已断言）


def test_closing_chat_toggles_hidden_not_just_a_class() -> None:
    """关闭必须走 hidden 属性。

    若改成只切 class，而 CSS 里没有对应的隐藏规则，就会出现
    "点了没反应"的老问题——所以这里锁死实现方式。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "el.chat.hidden = false" in js, "打开聊天框应移除 hidden"
    assert "el.chat.hidden = true" in js, "关闭聊天框应设回 hidden"
    assert "el.menu.hidden = false" in js, "打开轮盘应移除 hidden"
    assert "el.menu.hidden = true" in js, "收起轮盘应设回 hidden"


def test_chat_close_button_is_wired_and_clickable() -> None:
    """× 必须能真的关掉聊天框：既绑定了处理器，也不能被其它层挡住。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    code = _style_css_without_comments()
    assert re.search(r"on\(el\.chatClose, 'click', closeChat\)", js), (
        "× 没绑定 closeChat，点了不会有反应"
    )
    assert "el.chat.hidden = true" in js, "closeChat 必须设置 hidden"

    close_rule = _rule(".chat-close", code)
    assert close_rule, "style.css 里找不到 .chat-close 规则"
    body = close_rule
    # 关闭按钮要浮在面板内容之上，否则日志区会盖住它
    assert "z-index" in body, "关闭按钮需要有层级，避免被内容盖住"
    assert "position: absolute" in body


def test_wheel_uses_css_grid_not_absolute_offsets() -> None:
    """轮盘用 grid 排版（app.js 不再算三角函数位移）。

    这条锁定第 4 步顺带做的简化：若有人改回绝对定位，
    会出现选项跑到屏幕外的老问题。
    """
    css = _style_css()
    assert "grid-template-columns" in css
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "placeItem" not in js, "不该再用 JS 计算轮盘位移"
    assert "--wheel-r" not in js and "--wheel-r" not in css


# ------------------------------------------ 行程内配图去重（第 44 步）


def test_photo_dedup_within_one_itinerary() -> None:
    """同一份行程内不重复用同一张图，并且能丢弃「上一份行程」迟到的回调。

    两个景点的候选列表完全相同时，第二张卡片必须换一张（第 44 步）。
    （第 209 步删掉了只做这件事的 `verify_dialogs.js`，所以这半边也改成读源码。）
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "usedPhotoUrls" in js, "应有「行程内已用图片」的集合"
    assert "photoAssignment" in js, "应按景点记住分配结果"
    assert "function resetPhotoAssignment" in js, "换行程时要能重置"

    attach = re.search(r"(async function attachSpotPhoto\([\s\S]*?\n  \})", js)
    assert attach, "找不到 attachSpotPhoto"
    body = attach.group(1)

    # 挑第一张没用过的，选中后占住它
    assert "usedPhotoUrls.has(" in body, "要跳过已经用过的图"
    assert "usedPhotoUrls.add(" in body, "选中后要占住它"

    # **检查与占用之间不能有 await**：多个景点是并发取图的，
    # 中间一旦让出执行权，两个景点就会挑到同一张。
    pick = body.index("usedPhotoUrls.has(")
    reserve = body.index("usedPhotoUrls.add(")
    assert "await" not in body[pick:reserve], "挑图与占图之间不能有 await"

    # 候选都用完时宁可重复一张，也不要退回占位：去重是偏好，有图比没图强
    assert "chosen = candidates[0]" in body, "候选耗尽时应重复一张"

    # 按景点复用：切页重渲染不许换图
    assert "photoAssignment.get(" in body and "photoAssignment.set(" in body

    # 迟到的回调必须作废（第 36 步邮箱「邮件串台」是同一类问题）：
    # 否则旧行程的分配会写进新行程，两个景点就会拿到同一张图。
    assert "photoGeneration" in js, "要有「代数」令牌"
    assert "generation !== photoGeneration" in body, "要检查代数是否已变"
    assert body.index("await loadPhotoCandidates") < body.index("generation !== photoGeneration"), (
        "代数检查必须在 await 之后，否则拦不住迟到的回调"
    )

    # 重新生成时要重置
    submit = re.search(r"(async function onTravelSubmit\([\s\S]*?\n  \})", js)
    assert submit, "找不到 onTravelSubmit"
    assert "resetPhotoAssignment()" in submit.group(1), "新行程必须重置配图状态"


def test_backend_gives_several_photo_candidates() -> None:
    """去重的前提是后端给多张候选：只给一张就永远只能取第一张。"""
    source = (STATIC_DIR.parent / "api" / "routes.py").read_text(encoding="utf-8")
    assert "PHOTO_CANDIDATES" in source, "要有候选张数常量"
    assert "per_page" in source, "要请求多张"
    assert "candidates" in source, "响应里要给出候选列表"


def test_travel_window_title_is_travel_recommendation() -> None:
    """窗口顶部标题是「出行推荐」（第 46 步按用户要求改）。"""
    html = _index_html()
    tag = re.search(r"<h2[^>]*id=\"travel-title\"[^>]*>([^<]*)</h2>", html)
    assert tag, "找不到出行窗口标题"
    assert tag.group(1).strip() == "出行推荐", f"标题应为「出行推荐」，实际 {tag.group(1)!r}"


def test_leg_text_follows_the_route_mode() -> None:
    """通行段的文案要跟着 mode 变（第 67 步）。

    否则"公交行程里写着驾车 25 分钟"——文案和口径自相矛盾。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    leg = re.search(r"(function legText\(leg\)[\s\S]*?\n  \})", js)
    assert leg, "找不到 legText"
    body = leg.group(1)

    assert "leg.mode" in body, "要读后端给的口径"
    for name in ("驾车", "步行", "公交"):
        assert name in body, f"缺少「{name}」的文案"
    assert "startsWith('amap_')" in body, "来源判断要覆盖三种 amap_*，不能只认 amap_driving"
    assert "amap_driving" not in body, "不该再写死 amap_driving"


def test_readme_is_a_landing_page_not_a_diary() -> None:
    """README 是**一页落地页**，逐步日记在 docs/DECISIONS.md（第 68 步）。

    以前 README 长到 237 KB —— 比 app.js 还大，打开仓库根本看不到"这个项目是什么"，
    只看到第 1 步到第 67 步的开发流水账。

    第 270 步起 `docs/DECISIONS.md` 只是**本机私人笔记**（用户要求仓库只留项目本体），
    所以锚点检查改成「文件在才查」—— 本机没有就跳过这一段，README 的体量约束照旧。
    """
    root = STATIC_DIR.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert len(readme.encode("utf-8")) < 40_000, "README 又涨回大文件了，日记该进 docs/DECISIONS.md"
    assert "docs/DECISIONS.md" in readme, "README 必须指向决策记录（并说明它只在本机）"

    decisions_path = root / "docs" / "DECISIONS.md"
    if not decisions_path.is_file():
        return  # 私人笔记不在本机：没有可查的历史正文
    decisions = decisions_path.read_text(encoding="utf-8")

    # 历史不能丢：整份搬过去，不是重写
    for anchor in ("# 需求记录", "# 已知环境问题", "## 第 1 步"):
        assert anchor in decisions, f"决策记录里缺少 {anchor}"


def test_travel_note_does_not_promise_allergy_filtering() -> None:
    """备注的占位文字不能拿「对海鲜过敏」举例（第 79 步）。

    备注只被记录、不参与任何筛选，所以那个例子会让人以为写了就生效 ——
    这是**安全性质的假承诺**，比"功能没做完"更伤信任。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "对海鲜过敏" not in html, "占位文字不许再暗示备注能过滤过敏"
    assert "不参与筛选" in html, "要在界面上说清备注不参与筛选（第 262 步收进占位符）"


def test_the_trip_view_shows_the_note_and_says_it_does_not_filter() -> None:
    """备注既然存了就要显示出来，并且标明不参与筛选（第 79 步）。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "trip.note" in js, "备注要在行程里显示（以前只存不显）"
    assert "不参与筛选" in js, "显示时要说清它不影响推荐"

def test_the_chat_agent_gets_the_turn_token_budget():
    """聊天那条线的 Agent 必须带轮级 token 预算（第 140 步的自查）。

    用户问"聊天窗口是不是也有 token 预算"。答案是**有**，但这句话得能被测试钉住 ——
    否则哪天有人把 `turn_token_budget=` 从 `agent_factory` 里删掉，没人会发现，
    而症状只是"某次对话突然多花了几毛钱"。
    """
    # 这个文件已经 `from main import STATIC_DIR, app`；STATIC_DIR 的父目录就是仓库根
    source = (STATIC_DIR.parent / "main.py").read_text(encoding="utf-8")

    assert "def agent_factory()" in source
    factory = source[source.index("def agent_factory()"):][:600]
    assert "turn_token_budget=settings.turn_token_budget" in factory, (
        "聊天的 Agent 没有接轮级预算"
    )
    assert "max_steps=" in factory, "也应当限制步数"

def test_the_pet_has_no_capsule_background() -> None:
    """桌宠不带卡片底（第 142 步，用户要求）。

    原来 `.pet-body` 有 `background: var(--surface)` + `box-shadow`，角色被装在一个
    深色圆角胶囊里。图片换成透明底之后，那层底就成了多余的一圈 —— 用户要的是"只保留角色"。

    这条同时挡住"顺手把阴影加回来"：没有底色的框，`box-shadow` 只会变成角色背后
    一团圆角黑雾。
    """
    code = _style_css_without_comments()
    body = _rule(".pet-body", code)
    assert body, "找不到 .pet-body 规则"

    assert "background: transparent" in body, "桌宠按钮应无底色"
    assert "var(--surface)" not in body, "按钮不该再有表面色（那就是胶囊）"
    assert "box-shadow: none" in body, "按钮不该有外阴影"

    hover = code[code.index(".pet-body:hover"):][:260]
    assert "box-shadow" not in hover, "悬停/聚焦不该再画阴影（会变成一团黑雾）"
