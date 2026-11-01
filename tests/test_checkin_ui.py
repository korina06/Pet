"""签到窗口：结构断言读 HTML/CSS/JS，点轮盘、画日历这类行为交给 `scripts/verify_checkin.js`。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import shutil

import pytest

from main import STATIC_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# GitHub 的 runner 上没有本机那个 node 路径 —— **动态查找**（第 104 步）。
# 找不到时由 pytest.mark.skipif 跳过，绝不写死绝对路径。
NODE = shutil.which("node")
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_checkin.js"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _modal_block() -> str:
    """index.html 里签到窗口那一段（到桌宠为止），并剥掉 HTML 注释。

    剥注释是必须的：注释里解释了"还没到的日期不算"这类设计，
    不剥掉就会撞上自己的说明文字（test_recipe_ui.py 踩过同一个坑）。
    """
    html = _html()
    start = html.index('id="checkin-modal"')
    end = html.index('id="pet"')
    return re.sub(r"<!--[\s\S]*?-->", "", html[start:end])


def _rule(selector: str, code: str | None = None) -> str | None:
    """取第一条以 selector 开头的规则体（大括号里的内容）。"""
    source = code if code is not None else _css()
    match = re.search(
        rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", source
    )
    return match.group(1) if match else None


# ------------------------------------------------------------------ 窗口


def test_checkin_window_is_a_dialog_with_a_title():
    block = _modal_block()
    assert 'role="dialog"' in block, "签到窗口应是对话框"
    assert 'aria-modal="true"' in block, "签到窗口应是模态"
    assert 'aria-labelledby="checkin-title"' in block, "要能被读屏软件念出名字"
    assert 'id="checkin-title"' in block and "签到" in block, "窗口要有标题「签到」"


def test_checkin_window_exists_and_is_hidden_by_default():
    """窗口用 hidden 属性表示关闭（项目统一约定，见第 5 步的 [hidden] 规则）。"""
    html = _html()
    tag = re.search(r"<div[^>]*id=\"checkin-modal\"[^>]*>", html)
    assert tag, "index.html 里找不到签到窗口"
    assert "hidden" in tag.group(0), "签到窗口初始必须是 hidden"


def test_checkin_window_is_noticeably_larger():
    """用户要求"窗口做得稍微大一些"——这里把"大"落成可检查的数字。

    做法：签到窗口必须比登入窗口宽一倍以上，且宽高都写死
    （不靠内容撑高：日历 5~6 行，翻月时高度会跳）。
    """
    checkin = _rule(".modal-box--checkin")
    assert checkin, "找不到 .modal-box--checkin 规则"

    width = re.search(r"width:\s*min\((\d+)px", checkin)
    assert width, f"签到窗口应写死宽度：{checkin}"
    assert int(width.group(1)) >= 560, (
        f"窗口宽度只有 {width.group(1)}px，日历 7 列放不下「已签/未签」这行小字"
    )

    assert re.search(r"height:\s*min\(\d+px", checkin), (
        "高度要写死（日历有 5~6 行，靠内容撑会随月份跳）"
    )
    assert "max-height" in checkin, "矮屏要有上限，否则窗口会顶出视口"

    login = _rule(".modal-box") or ""
    login_width = re.search(r"width:\s*min\((\d+)px", login)
    assert login_width, "找不到登入窗口的宽度（用来对照）"
    assert int(width.group(1)) >= int(login_width.group(1)) + 240, (
        "签到窗口相对其它窗口要明显更大（用户要求「稍微大一些」）"
    )


def test_checkin_window_scrolls_inside_not_the_whole_box():
    """窗口自身不滚，滚动交给日历区——否则表头会跟着滚走。"""
    checkin = _rule(".modal-box--checkin")
    assert "overflow: hidden" in checkin, "窗口骨架不该自己滚"
    body = _rule(".cal-body")
    assert body and "overflow-y: auto" in body, "日历区自己滚"


# ------------------------------------------------------------------ 日历


def test_calendar_is_a_seven_column_table():
    """日历用真表格：读屏软件能念出"第几行第几列"，也自带列对齐。"""
    block = _modal_block()
    assert "cal-table" in block and "<table" in block, "日历应是 <table>"
    assert 'id="checkin-days"' in block, "缺少日期格容器"

    table = _rule(".cal-table")
    assert table and "table-layout: fixed" in table, (
        "固定版式：不然某一列会被内容撑宽，7 列就不等宽了"
    )
    week = _rule(".cal-week")
    assert week and "14.28%" in week, "每列 1/7 ≈ 14.28%"


def test_day_cell_has_room_for_a_status_line():
    """每个日期格下面要有写"已签 / 未签"的位置。

    这是用户这次的核心要求（"在日历表的每个日期下加上'已签'和'未签'"），
    所以格子高度必须定死（走变量），且是一列多行（号数 / 状态 [+ 今天的标签]）。
    """
    day = _rule(".cal-day")
    assert day, "找不到 .cal-day 规则"
    assert "var(--cal-cell" in day, "格子高度应走 --cal-cell 变量（窄屏要能改小）"
    assert "min-height" in day, (
        "用 min-height 而不是 height：今天那格多一行「今天」标签，"
        "定死高度会把三行挤在一起"
    )
    assert "flex-direction: column" in day, "格子里是上下几行（号数 / 状态）"

    note = _rule(".cal-note")
    assert note, "找不到状态文字 .cal-note 的规则"
    assert "font-size" in note, "状态文字要定字号，否则会把格子撑变形"

    box = _rule(".modal-box--checkin")
    assert re.search(r"--cal-cell:\s*(\d+)px", box), "--cal-cell 应有默认值"


def test_done_days_have_no_color_mark():
    """用户第 3 条：**已签日期不用加颜色标记**。

    所以 .is-done 不该再有自己的绿底/绿字，只保留默认底与默认边框；
    区分靠"已签"两个字本身。
    """
    done = _rule(".cal-day.is-done")
    assert done, "找不到 .cal-day.is-done 规则"
    assert "background: var(--surface)" in done, "已签不应有彩色底"
    assert "var(--cal-done)" not in done, "已签不该再用绿色边框"
    assert ".cal-day.is-done .cal-num { color" not in _css(), "已签的数字不该变色"
    assert ".cal-day.is-done .cal-note { color" not in _css(), "已签的状态字不该变色"

    todo = _rule(".cal-day.is-todo")
    assert todo and "background" in todo, "未签保留淡灰底（它没有别的标记）"


def test_past_days_get_a_grey_blue_highlight_on_hover():
    """用户第 4 条：鼠标指向或点击过去日期时用**灰蓝色**标出，区分"之前"和"现在"。"""
    css = _css()
    assert ".cal-day.is-past:hover" in css, "过去日期悬停要有单独样式"
    assert ".cal-day.is-past:active" in css, "按下（点击）时也要标出来"
    past_rule = re.search(
        r"\.cal-day\.is-past:hover[\s\S]{0,160}?\{([^}]*)\}", css
    )
    assert past_rule, "找不到过去日期的悬停规则"
    assert "var(--cal-past)" in past_rule.group(1), "灰蓝色应走 --cal-past 变量"

    js = _js()
    assert "is-past" in js, "app.js 要给过去的日子加 is-past 类"


def test_today_uses_a_blue_accent_and_no_label():
    """用户第 2 条：今天用**蓝色特效**标出，**不写"今天"二字**。"""
    js = _js()
    assert "is-today" in js, "今天要有自己的类"

    today = _rule(".cal-day.is-today")
    assert today, "找不到今天的样式"
    assert "border-color: var(--accent)" in today, "今天要有一圈蓝色边框"
    assert "box-shadow" in today, "再加一层光晕，蓝色特效更明显"
    assert ".cal-day.is-today .cal-num { color: var(--accent)" in _css(), "今天的数字用蓝色"

    assert "cal-today-tag" not in js, "不该再往格子里写「今天」标签"
    assert "cal-today-tag" not in _css(), "对应的样式也该删掉"


def test_future_days_have_no_frame():
    """用户第 5 条：没到的日期**不加框**。"""
    future = _rule(".cal-day.is-future")
    assert future, "找不到 future 的样式"
    assert "border-color: transparent" in future, "未来的格子不画边框"
    assert "background: transparent" in future, "未来的格子不画底色"


def test_cell_has_festival_and_lunar_lines():
    """用户第 1 条：日期数字下面要有**节日名 + 农历日期**，字号都比数字小。

    第三行才是"已签 / 未签"（用户第 2 条），所以状态字最小。
    """
    js = _js()
    num = _rule(".cal-num")
    fest = _rule(".cal-fest")
    lunar = _rule(".cal-lunar")
    note = _rule(".cal-note")
    for name, rule in ((".cal-num", num), (".cal-fest", fest),
                       (".cal-lunar", lunar), (".cal-note", note)):
        assert rule, f"找不到 {name} 的样式"

    def size(rule: str) -> float:
        return float(re.search(r"font-size:\s*([\d.]+)px", rule).group(1))

    assert size(fest) < size(num), "节日名要比日期数字小"
    assert size(lunar) < size(num), "农历要比日期数字小"
    assert size(note) <= size(lunar), "已签/未签要比农历还小（用户第 2 条）"

    cell = js[js.index("function checkinDayCell"):]
    cell = cell[: cell.index("\n  }")]
    for cls in ("cal-num", "cal-fest", "cal-lunar", "cal-note"):
        assert cls in cell, f"格子里应渲染 .{cls}"
    # 顺序：数字 → 节日 → 农历 → 状态
    order = [cell.index(f"'{cls}'") for cls in
             ("cal-num", "cal-fest", "cal-lunar", "cal-note")]
    assert order == sorted(order), "渲染顺序应是 数字 → 节日 → 农历 → 已签/未签"


def test_rest_badge_uses_backend_labels():
    """用户第 1 条：要有"休"（放假）与"班"（调休上班）的标志。"""
    js, css = _js(), _css()
    assert "cal-badge" in js, "app.js 要渲染休/班小标"
    assert "rest_label" in js, "小标文字用后端给的 rest_label"
    assert "rest_labels" in js, "没有逐日标签时回落到月视图的 rest_labels"

    badge = _rule(".cal-badge")
    assert badge, "找不到 .cal-badge 样式"
    assert "position: absolute" in badge, "小标定位在角落，不占行高"
    assert ".cal-day.is-rest-work .cal-badge" in css, "补班日要用另一种颜色"


def test_future_days_are_greyed_out_and_not_clickable():
    """用户原话："还没到的日期不算"——不写状态、变灰、不可点。"""
    future = _rule(".cal-day.is-future")
    assert future, "找不到还没到的日期的样式"
    assert "cursor: default" in future, "不可点的格子不该是手形光标"
    assert "opacity" in future, "要变灰一档，看起来就是不可点"

    js = _js()
    # 真正让"点不动"成立的是 disabled（浏览器不派发 disabled 元素的 click）
    assert "cell.disabled = true" in js, "还没到的日期应设 disabled"
    # 并且**不给**它写状态文字
    assert "if (status && day.note)" in js, "没有状态就不该写状态文字"


def test_today_is_marked_and_clickable():
    js = _js()
    assert "is-today" in js, "今天要有自己的记号"
    today = _rule(".cal-day.is-today")
    assert today and "border-color: var(--accent)" in today, (
        "今天要有一圈高亮边框，用户一眼能找到该点哪个"
    )


# ------------------------------------------------------------ 元素与接线


def test_all_checkin_elements_exist_and_are_referenced():
    """HTML 与 app.js 必须同步：漏一个 id 就会被安全助手静默跳过。

    第 19 步的事故就是这么来的（一个元素缺失，连带三个功能失效），
    test_frontend_wiring 里有总的防线，这里再针对签到这一组钉一遍。
    """
    html = _html()
    js = _js()
    for element_id in (
        "checkin-modal",
        "checkin-close",
        "checkin-prev",
        "checkin-next",
        "checkin-month-label",
        "checkin-state",
        "checkin-grid",
        "checkin-weekdays",
        "checkin-days",
        "checkin-confirm",
        "checkin-confirm-text",
        "checkin-confirm-ok",
        "checkin-confirm-cancel",
    ):
        assert f'id="{element_id}"' in html, f"HTML 缺少 {element_id}"
        assert f"getElementById('{element_id}')" in js, f"app.js 没有引用 {element_id}"


def test_removed_ui_pieces_stay_removed():
    """用户第 1、2、3 条要去掉的三处，不该再被加回来。

    写这条是因为"删掉的东西"最容易被下一次改动顺手带回来：
    图例、表格 caption、本月统计行，现在都不该出现在 HTML / CSS / app.js 里。
    """
    html, css, js = _html(), _css(), _js()
    for gone in ("checkin-legend", "checkin-caption", "checkin-summary"):
        assert gone not in html, f"{gone} 已被用户要求去掉，不该再出现在 HTML 里"
        assert f"getElementById('{gone}')" not in js, f"app.js 不该再引用 {gone}"
    for selector in (".cal-legend", ".cal-key", ".cal-caption", ".cal-summary"):
        assert selector not in css, f"{selector} 的样式该一起删掉"


def test_makeup_confirm_is_an_in_window_box_not_native_confirm():
    """用户第 1 条：补签的确认要弹在**这个窗口里面**，不是浏览器原生 confirm。

    原生框的问题很具体：长得跟页面无关、把整个窗口遮住、还阻塞脚本。
    所以这里同时钉两件事：窗口里有那个小框，且代码里不再用 window.confirm。
    """
    html, _, js = _html(), _css(), _js()
    assert 'id="checkin-confirm"' in html, "缺少窗口内的补签确认框"
    assert 'role="dialog"' in html[html.index('id="checkin-confirm"'):][:500], (
        "小框也该是一个对话框，读屏软件才念得出来"
    )
    assert "window.confirm" not in js, "不该再用原生 confirm"
    assert "askMakeup" in js, "补签前的确认应走 askMakeup"

    overlay = _rule(".cal-confirm")
    assert overlay, "找不到 .cal-confirm 规则"
    assert "position: absolute" in overlay, (
        "遮罩要相对**窗口**定位（absolute），而不是整个视口（fixed）"
    )
    assert "inset: 0" in overlay, "遮罩要盖住整个窗口"

    # 关窗的 × 必须压在遮罩之上，否则"弹了小框就退不出去"
    close_rule = _rule(".modal-box--checkin .modal-close")
    assert close_rule and "z-index" in close_rule, "× 要提到遮罩之上"


def test_card_error_is_rendered_inside_the_day_cell():
    """用户第 2 条：红字提醒放在想补签的那一天的"未签"下面。

    实现要点：提示是**一格一格的**（按日期匹配），不是窗口底部一行通稿。
    """
    js = _js()
    note = _rule(".cal-card-note")
    assert note, "缺少格子内提示的样式"
    assert "var(--danger)" in note, "提示要用与其它窗口一致的红色"
    assert "overflow-wrap" in note, "窄格里长文案也不许溢出格子"

    assert "state.checkinCardNote" in js, "提示应存成「某一天 + 文案」"
    cell = js[js.index("function checkinDayCell"):]
    cell = cell[: cell.index("\n  }")]
    assert "cal-note" in cell and "cal-card-note" in cell, (
        "格子里要同时有状态行与失败提示（提示是追加，不是替掉「未签」）"
    )
    assert "day.date === note.date" in cell, "提示必须只挂在出问题的那一天"

    # 窗口底部那行只负责"取数中 / 取数失败"，不再当通用错误区
    assert "checkin-error" not in _html(), "窗口底部那个错误区已并入 .cal-state"


def test_checkin_is_opened_from_the_wheel_and_closable():
    js = _js()
    assert "run: () => openDialog('checkin')" in js, "轮盘上的「签到」应能开窗"
    assert "if (kind === 'checkin')" in js, "dialogEls / openDialog 要认得 checkin"
    assert "on(el.checkinClose, 'click', closeDialog)" in js, "× 要能关窗"
    assert "if (event.target === el.checkinModal) closeDialog()" in js, (
        "点遮罩要能关窗"
    )
    # Esc 走的是统一的 openDialogKind 分支，这里确认它还在
    assert "else if (state.openDialogKind) closeDialog()" in js, "Esc 要能关窗"


def test_calendar_is_built_in_seven_wide_rows():
    """分行的规则必须写死在 JS 里（7 列），且空格也要占一格。"""
    js = _js()
    assert "const WEEKDAY_COLUMNS = 7" in js, "列数应是一个常量，别散落魔法数字"
    render = re.search(r"function renderCheckinDays\(view\)\s*\{([\s\S]*?)\n  \}", js)
    assert render, "找不到 renderCheckinDays 函数"
    body = render.group(1)
    assert "WEEKDAY_COLUMNS" in body, "分行要按常量来"
    assert "cal-blank" in body, "月初的空格也要占一格，否则 1 号会挪到别的列"


def test_status_text_comes_from_the_backend():
    """状态文案（已签/未签）由后端给，前端只照着画。

    这是项目既有约定（食谱窗口连开场白都不硬编码）：加一种状态
    或改一个词只动 core/checkin.py，前端自动跟上。
    """
    js = _js()
    assert "status_labels" in js, "前端应读后端给的 status_labels"
    assert "day.note" in js, "格子里的状态文字应直接用后端给的 note"
    # 前端不该自己写这两个词去做判断（注释里提到不算）
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*[\s\S]*?\*/", "", js))
    assert "'已签'" not in code and "'未签'" not in code, (
        "前端代码里不该硬编码「已签」「未签」，它们来自接口"
    )


def test_clicking_a_day_goes_through_event_delegation():
    """日期格是每次重画现生成的，逐个绑定会在重画后失效。"""
    js = _js()
    assert "on(el.checkinDays, 'click', onCheckinDayClick)" in js, (
        "点日期要走事件委托（绑在容器上）"
    )
    assert "event.target.closest('.cal-day')" in js, "要能从点击目标找回日期格"


def test_checkin_errors_keep_the_backend_wording():
    """失败要说人话：后端写好的 detail 不能被换成含糊的"出了点问题"。"""
    js = _js()
    assert "readDetail(response)" in js, "错误文案应走既有的 readDetail"
    # describeError 会把 detail 统一换掉，签到这条链上不能再用它
    checkin = js[js.index("签到窗口 */"):]
    assert "showCheckinFailure(describeError(" not in checkin, (
        "签到失败别用 describeError——那会丢掉后端那句可照做的中文"
    )


# ------------------------------------------------------------------ 行为


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_checkin_flow_behaviour():
    """真跑一遍：开窗 → 画日历 → 点今天签到 → 补签 → 翻月 → 关窗。"""
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"

    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "签到窗口行为验证未全部通过：\n" + output
    assert "失败 0 项" in output, "验证脚本报告了失败项：\n" + output
