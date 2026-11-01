"""前端接线：app.js 引用的每个 id 在 HTML 里都存在，且单个元素缺失不会连带弄坏整套绑定。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from main import STATIC_DIR

APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT   # 同义（新测试用它更直观）
MAIN_PY = ROOT / "main.py"
SCRIPTS_DIR = ROOT / "scripts"


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_every_referenced_element_id_exists_in_html():
    """app.js 引用的每个 id 都必须能在 index.html 里找到。

    这条能直接拦住"少写一个按钮导致整页瘫痪"的事故。
    """
    referenced = sorted(set(re.findall(r"getElementById\('([^']+)'\)", _js())))
    assert referenced, "没有解析到任何 getElementById 调用，检查解析正则"

    html = _html()
    missing = [name for name in referenced if f'id="{name}"' not in html]
    assert not missing, (
        f"app.js 引用了 HTML 里不存在的元素：{missing}。"
        "这会让 bindEvents() 抛错，从而中断后续所有绑定。"
    )


def test_element_lookups_are_declared_in_one_place():
    """所有 getElementById 都应集中在 el 对象里，便于一眼核对。

    若散落在各处，漏改或改错名字就很难发现。
    """
    matches = re.findall(r"document\.getElementById\('([^']+)'\)", _js())
    # 全部出现次数应与 el 对象里的条目数一致（允许重复取用同一个 id）
    el_block = re.search(r"const el = \{([\s\S]*?)\n  \};", _js())
    assert el_block, "找不到 el 对象定义"
    declared = re.findall(r"(\w+):\s*document\.getElementById", el_block.group(1))
    assert len(declared) >= len(set(matches)), (
        "存在未在 el 对象里声明的元素查询，建议集中声明"
    )


def test_binding_goes_through_safe_helper():
    """元素绑定必须走安全助手，单个元素缺失不能中断其它绑定。

    直接写 `el.xxx.addEventListener(...)` 时，只要有一个元素为 null，
    这一行就会抛错并中断后面所有绑定——已经因此出过一次线上事故。
    """
    js = _js()

    # 安全助手与启动自检都要在
    assert "function on(target, type, handler)" in js, "缺少安全绑定助手 on()"
    assert "function warnMissingElements()" in js, "缺少启动自检"
    assert "warnMissingElements()" in js, "启动自检没有被调用"

    # bindEvents 里不允许再出现直接对 el.xxx 调 addEventListener
    bind_block = re.search(r"function bindEvents\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert bind_block, "找不到 bindEvents 函数"
    body = bind_block.group(1)
    direct = re.findall(r"el\.\w+\.addEventListener", body)
    assert not direct, (
        f"bindEvents 里仍有直接绑定（应为 on(el.xxx, ...)）：{direct}。"
        "元素一旦缺失就会中断后续所有绑定。"
    )


def test_init_steps_are_isolated():
    """init() 的各步要互相隔离，任一步失败不影响其它步。"""
    js = _js()
    init_block = re.search(r"function init\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert init_block, "找不到 init 函数"
    body = init_block.group(1)

    for step in ("bindEvents()", "renderWheel()", "restorePosition()", "loadTools()"):
        assert step in body, f"init 里缺少 {step}"
    # 关键步骤都包在 try/catch 里：数一下 catch 数量
    assert body.count("catch") >= 3, (
        "init 的关键步骤应各自 try/catch，避免一步失败导致整页瘫痪"
    )


def test_sidebar_close_button_is_gone():
    """侧边栏内的 × 已按用户要求移除（第 20 步）；第 209 步连侧边栏本身也删了。

    历史上这里踩过的坑值得留着：HTML 少一个按钮 → app.js 去取 null →
    `bindEvents()` 抛 TypeError → 它之后的绑定全都没执行（功能静默失效）。
    所以这条测试除了"删掉的东西别回来"，还守着"入口只有桌宠自己"这件事。
    """
    html = _html()
    js = _js()

    assert 'id="sidebar-close"' not in html, "× 按钮已被移除，不该再出现"
    # 第 209 步：整条侧边栏都没了，连它的 ☰ 开关与切换函数一起。
    assert 'id="sidebar"' not in html, "侧边栏已按用户要求删除"
    assert 'id="sidebar-toggle"' not in html, "左上角 ☰ 开关已随侧边栏删除"
    assert "toggleSidebar" not in js, "侧边栏的开关函数应已删除"
    assert "closeSidebar" not in js, "侧边栏的关闭函数应已删除"


def test_only_pet_entry_points_remain():
    """第 209 步：入口只剩桌宠本身 —— 五个真在用的小窗都在，且都能关。

    删掉「关于 / 设置 / 登入」后最容易漏的是**关窗通路**：以前那两个小窗是
    `closeDialog()` 的第一批用户，删它们时若顺手删了通用关闭函数，
    剩下五个窗口就会打不开也关不掉。所以这里一次钉住"在"和"能关"。
    """
    html = _html()
    js = _js()

    for modal in ("model-modal", "travel-modal", "mail-modal", "recipe-modal", "checkin-modal"):
        assert f'id="{modal}"' in html, f"缺了还在用的小窗 {modal}"

    for key in ("modelClose", "travelClose", "mailClose", "recipeClose", "checkinClose"):
        assert f"on(el.{key}, 'click', closeDialog)" in js, f"{key} 没接到 closeDialog"

    assert "function closeDialog()" in js, "通用关闭函数不能丢"
    # Esc 也要能关（第 209 步之前由 verify_dialogs.js 守着，那个脚本已删）
    assert "if (event.key !== 'Escape') return;" in js, "Esc 关闭通路丢了"


def test_frontend_files_are_served_with_no_store():
    """前端文件必须带 no-store，避免"新 JS + 旧 HTML"的静默失效（第 26 步）。

    这类事故很难自查：旧 index.html 里没有新元素 → 绑定被安全助手跳过 →
    **没有报错**，只是功能失效（例如表单 submit 没被接管，点确定就刷新整页）。
    所以从服务端堵掉，并在这里钉住，防止哪天中间件被误删。
    """
    source = MAIN_PY.read_text(encoding="utf-8")

    assert '@app.middleware("http")' in source, "main.py 缺少中间件装饰器"
    assert "no-store" in source, "缺少 no-store 响应头"
    # /api/* 不在此列，方便将来给接口做缓存
    assert 'startswith("/api/")' in source, "应把 /api/* 排除在外"


def test_static_assets_are_versioned_to_bust_cache():
    """首页里的 css/js 链接必须带版本号（第 27 步）。

    只加 no-store 还不够：用户浏览器里可能早就存着一份旧的 app.js，
    no-store 管不到"已经存下的副本"。给链接加上 `?v=<修改时间>` 后地址变了，
    浏览器必然重新下载——用户不需要记得按 Ctrl+F5。
    """
    source = MAIN_PY.read_text(encoding="utf-8")

    assert "STATIC_ASSETS" in source, "缺少需要版本化的资源清单"
    assert "st_mtime" in source, "版本号应来自文件修改时间（改了就变）"
    assert "?v=" in source, "首页 HTML 里的资源链接应插入版本参数"
    assert "html=True" in source, (
        "StaticFiles 要开 html=True，首页才会走中间件拿到带版本号的 HTML"
    )


def test_app_js_references_all_travel_elements():
    """出行窗口那批 id 必须同时存在于 HTML 与 app.js（第 26 步补的防线）。

    上一轮的现象正是"HTML 与 JS 不同步"：HTML 里有表单，但浏览器用了旧的
    HTML，于是 app.js 里的 el.travelForm 是 null，绑定被静默跳过。
    """
    html = _html()
    js = _js()

    for element_id in (
        "travel-modal",
        "travel-form",
        "travel-city",
        "travel-start",
        "travel-end",
        "travel-days",
        "travel-transport",
        "travel-stay",
        "travel-note",
        "travel-submit",
        "travel-tab-form",
        "travel-tab-plan",
        "travel-tab-plan-label",
        "travel-tab-plan-close",
        "travel-tabs",
        "travel-page-form",
        "travel-page-plan",
        "travel-page-day",
        "travel-overview",
        "overview-title",
        "overview-dates",
        "overview-advice",
        "day-list",
        "day-title",
        "day-body",
        "plan-progress",
        # 第 41 步：模型原文折叠区（overview-model / plan-output）已删除，
        # 换成显示降级原因与校验问题的提示条。
        "plan-notice",
    ):
        assert f'id="{element_id}"' in html, f"HTML 缺少 {element_id}"
        assert f"getElementById('{element_id}')" in js, f"app.js 没有引用 {element_id}"


def test_app_js_references_all_mail_elements():
    """邮箱窗口那批 id 必须同时存在于 HTML 与 app.js（第 33 步）。

    道理和上面出行窗口那条一样：HTML 与 JS 一旦不同步，
    取到的就是 null，绑定被安全助手静默跳过——**没有报错，只是功能没了**。
    登入页与收件箱页的元素都在这张清单里，因为它们同在一个窗口内。
    """
    html = _html()
    js = _js()

    for element_id in (
        "mail-modal",
        "mail-close",
        "mail-page-login",
        "mail-page-inbox",
        "mail-form",
        "mail-address",
        "mail-secret",
        "mail-provider-hint",
        "mail-error",
        "mail-submit",
        "mail-service-toggle",
        "mail-services",
        "mail-folders",
        "mail-account",
        "mail-logout",
        "mail-search",
        "mail-list",
        "mail-state",
    ):
        assert f'id="{element_id}"' in html, f"HTML 缺少 {element_id}"
        assert f"getElementById('{element_id}')" in js, f"app.js 没有引用 {element_id}"


# ------------------------- 聊天历史恢复（第 94 步 / S4）


def test_chat_session_id_survives_a_restart():
    """聊天 id 必须存 `localStorage`（第 94 步）。

    这是"每次打开都是从头开始"的**直接原因**：`sessionStorage` 在关掉页面/窗口时
    就失效，于是每次打开都是新 id —— 后端存了历史也找不到。

    这条断言刻意写死"不能再用 sessionStorage 存 SESSION_KEY"：
    改回去会让持久化静默失效（界面看起来一切正常，就是没有历史）。
    """
    js = _js()

    body = js[js.index("function sessionId()"):]
    body = body[:body.index("\n  }")]
    assert "localStorage" in body, "聊天 id 要存 localStorage"
    assert "sessionStorage" not in body, (
        "不能再退回 sessionStorage —— 关窗即失效，历史就白存了"
    )


def test_open_chat_restores_history_from_the_backend():
    """打开对话窗口时要拉一次历史并画回来（第 94 步）。"""
    import re

    js = _js()

    # 1) openChat 里要触发恢复
    open_chat = re.search(r"function openChat\(focusInput = false\)\s*\{([\s\S]*?)\n  \}", js)
    assert open_chat, "找不到 openChat"
    assert "restoreChatHistory()" in open_chat.group(1), "打开窗口时要拉历史"

    # 2) 恢复函数：只请求一次、空结果不动界面、失败不打断
    restore = re.search(r"async function restoreChatHistory\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert restore, "找不到 restoreChatHistory"
    body = restore.group(1)
    assert "/session/" in body, "要打 /api/session/{id}"
    assert "chatHistoryLoaded" in body, "一个页面生命周期只拉一次"
    assert "if (messages.length)" in body, "空会话不该清空界面（聊天本来就没有欢迎语）"
    assert "catch" in body, "拉取失败不打扰用户"


def test_the_history_endpoint_is_used_with_an_encoded_session_id():
    """session_id 要 `encodeURIComponent` —— 它是前端生成的 UUID，但别赌它安全。"""
    js = _js()
    assert "encodeURIComponent(sessionId())" in js


# ---------------------- 清空这段对话（第 95 步 / S5）


def test_clear_chat_asks_inside_the_window_not_with_a_native_dialog():
    """确认要弹在**聊天窗口内部**，不许用浏览器原生 confirm（第 95 步）。

    和第 86 步签到补签同一条理由：原生框跟页面无关、把整个窗口遮住、还阻塞脚本。
    """
    js = _js()
    html = _html()

    assert 'id="chat-confirm"' in html, "缺少窗口内的清空确认框"
    block = html[html.index('id="chat-confirm"'):][:600]
    assert 'role="dialog"' in block, "小框也该是对话框，读屏软件才念得出来"

    assert "function openChatConfirm(" in js
    assert "openChatConfirm('清空这段对话？清空后无法恢复。')" in js or (
        "openChatConfirm(" in js and "清空这段对话" in js
    )


def test_clear_chat_deletes_only_this_session():
    """只删**这个会话**的聊天记录：会话 id 不变，长期忌口/菜谱库都不碰。"""
    import re

    js = _js()
    clear = re.search(r"async function clearChat\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert clear, "找不到 clearChat"
    body = clear.group(1)

    assert "DELETE" in body, "要调已有的 DELETE /api/sessions/{id}"
    assert "encodeURIComponent(sessionId())" in body
    assert "avesion" not in body and "aversions" not in body, "不该动长期忌口"
    assert "chatHistoryLoaded = true" in body, "清空后别再拉回历史"


def test_the_confirm_box_covers_only_the_chat_panel():
    """遮罩相对**聊天窗口**定位（absolute），不是整个视口（fixed）。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    block = css[css.index(".chat-confirm {"):]
    block = block[:block.index("}")]
    assert "position: absolute" in block, "遮罩要相对聊天窗口定位"
    assert "inset: 0" in block, "遮罩要盖住整个聊天窗口"
    assert "position: fixed" not in block


# ---------------------------- 模型开关（第 98–100 步）


def test_wheel_puts_the_model_entry_right_below_chat():
    """轮盘顺序是用户指定的：「模型」紧跟在「对话」下面。"""
    js = _js()

    assert "key: 'model'" in js
    assert "actions.push(WHEEL_MODEL, WHEEL_CHECKIN" in js, (
        "「模型」要插在「对话」与「签到」之间"
    )


def test_the_model_window_is_its_own_dialog():
    html = _html()
    js = _js()

    assert 'id="model-modal"' in html
    block = html[html.index('id="model-modal"'):][:300]
    assert 'role="dialog"' in block
    assert "if (kind === 'model')" in js, "openDialog 要认识 model"
    assert "el.modelModal" in js


def test_the_model_list_is_a_radio_group_driven_by_backend_data():
    """一家一个按钮，`aria-pressed` 标出当前那家 —— 点谁就只亮谁。

    用不了的那家**列出但禁用**，并把原因写进按钮里（比如 MiMo 缺接口地址）。
    """
    js = _js()

    assert "aria-pressed" in js
    assert "button.disabled = !provider.available" in js
    assert "provider.reason" in js, "禁用时要说明原因"
    # 文案与模型名都来自后端：前端不该写死任何一家
    assert "glm-4-flash" not in js and "deepseek-chat" not in js, (
        "模型名必须来自 /api/model，写死在 JS 里等于两套真相"
    )


def test_switching_posts_the_name_and_probing_posts_to_its_own_path():
    js = _js()

    assert "`${API}/model`" in js
    assert "JSON.stringify({ name })" in js
    assert "`${API}/model/probe`" in js, "「测一下」要打独立的探测接口"
    # 探测只在用户点击时发生 —— 不做自动探测，那会在启动时偷偷花钱
    assert "void probeModel();" in js


# ------------------------- 历史恢复的竞态（第 102 步）


def test_history_restore_does_not_wipe_a_message_sent_while_it_loaded():
    """历史是异步拉的；期间用户可能已经发了一句。

    以前无条件 `replaceChildren()`，会把用户刚发的那条**从界面上抹掉**
    （评价4 P1-4）。现在的规则：进入时记下条数，回来发现变了就**插到上面**。
    """
    js = _js()

    assert "const beforeCount = el.chatLog.children.length;" in js, "要先记下条数"
    assert "el.chatLog.children.length === beforeCount" in js, "回来要比对"
    assert "el.chatLog.prepend(fragment)" in js, "期间有新消息 -> 历史插到上面"
    assert "el.chatLog.replaceChildren(fragment)" in js, "没人动过 -> 正常整块换上"


def test_history_restore_is_discarded_after_clearing():
    """点了清空之后，晚到的历史响应不许再把对话画回来。"""
    js = _js()

    assert "chatCleared: false" in js
    assert "state.chatCleared = true" in js, "清空时要置位"
    assert "if (state.chatCleared)" in js, "恢复时要检查并丢弃"


def test_history_nodes_are_built_before_insertion():
    """历史要先造成 fragment 再一次性插入 —— 逐条 append 会触发多次重排。"""
    js = _js()

    assert "function makeMessageNode(kind, text)" in js
    assert "document.createDocumentFragment()" in js


def test_no_script_replaces_the_global_stdout() -> None:
    """`scripts/` 里**谁都不许**再写 `sys.stdout = ...`（第 103 步）。

    评价4 抓到的真 bug：`import_recipe_md.main()` 里替换了全局 stdout，
    任何调用方（含 pytest）的捕获流都被换掉，teardown 关闭 buffer 时抛
    `ValueError: I/O operation on closed file`。

    要改编码就用 `core.console.force_utf8()`（内部是 `reconfigure`，原地改、不换对象）。
    这条测试把"别再犯"变成机器可查的规则，而不是靠记性。
    """
    import re

    offenders = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            # 只看真正的赋值；注释里提到它不算
            if re.match(r"\s*sys\.stdout\s*=", line):
                offenders.append(f"{path.name}:{lineno}")

    assert not offenders, f"这些脚本替换了全局 stdout：{offenders}"


def test_node_is_looked_up_not_hardcoded():
    """测试里不许写死 node 的绝对路径（第 104 步）。

    评价4 那次 CI 红就是这么来的：5 个 UI 测试把本机路径
    `C:\\Users\\...\\node.exe` 写死，runner 上当然没有 ——
    `subprocess.run` 报 `WinError 2`，而 `pytest.mark.skipif` 当时还是空桩，
    于是"跑不了"变成了"失败"。

    现在统一用 `shutil.which("node")`，找不到就跳过（跳过 ≠ 通过，会单独计数）。
    """
    offenders = []
    for path in sorted(SCRIPTS_DIR.parent.joinpath("tests").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "NODE = " in line and 'shutil.which("node")' not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, f"这些地方没走动态查找：{offenders}"


def test_the_model_window_shows_today_usage_by_feature():
    """「模型」小窗要显示**按功能分开**的今日用量（第 111 步）。

    只给一个总数看不出钱花在聊天还是食谱上 —— 那正是账本要回答的问题。
    """
    js = _js()
    html = _html()

    assert 'id="model-usage"' in html
    block = html[html.index('id="model-usage"'):][:120]
    assert "></p>" in block, "静态 HTML 里不留文案，由 JS 填"

    assert "async function refreshUsageSummary()" in js
    assert "/usage/summary?days=1" in js
    assert "refreshUsageSummary()" in js, "打开窗口时要去取"
    assert "row.feature" in js, "必须按功能分别显示"


def test_the_usage_line_admits_how_many_calls_lack_usage():
    """缺 usage 的次数要写在界面上（第 114 步 / 评价6 第 5 条）。

    只显示 total 的话，一串 missing 会看起来像"今天花了 0"。
    """
    js = _js()

    assert "total.missing" in js
    assert "次没有用量记录" in js

def test_recipe_history_cannot_wipe_a_message_sent_during_init():
    """食谱的历史回来得晚，不许把用户刚发的消息/卡片清掉（第 124 步 / 评价8）。

    真事：`prepareRecipe()` 异步取历史，`restoreRecipeSession()` 会 replaceChildren()。
    用户在它回来之前发一句，那句和刚到的卡片就一起消失。

    修法：发送时 `recipeEpoch += 1`；`prepareRecipe` 抓一次代次，回来时代次变了就不覆盖。
    这里只做**结构断言**（三处都在、且顺序对）；行为验证由真浏览器烟测负责 ——
    它跑的是真事件、真时序，比在 DOM 桩里模拟延迟更接近实情。
    """
    js = _js()

    assert "recipeEpoch: 0" in js, "缺少代次计数器"
    assert "state.recipeEpoch += 1" in js, "发送时必须推进代次"
    assert "const epoch = state.recipeEpoch" in js, "prepareRecipe 必须抓代次"
    # 第 170 步（评价12 P3-1）在同一个守卫上又加了一条 `|| state.recipeSending`：
    # 流在途时重建历史会把正在打字的气泡摘掉。这里只要求"代次比对还在"。
    assert js.count("state.recipeEpoch !== epoch") >= 1, (
        "历史回来时必须比对代次，否则竞态还在"
    )


def test_meal_slots_are_shown_as_suggestions():
    """餐次要显示成"建议"，不能说得像计划/高德字段（第 125 步）。

    高德不返回"这是早餐店"。餐次是我们按位置与顺序分配的，界面必须诚实。
    """
    js = _js()

    assert "meal.slot" in js, "前端没有用到餐次字段"
    assert "建议${meal.slot}" in js, "餐次要写成「建议早餐/午餐/晚餐」，不能当成事实"


def test_shopping_list_ui_is_wired_without_doing_arithmetic():
    """买菜清单的界面（第 126 步）。

    三件事必须有：卡片上的勾选框（点了不打开详情）、生成入口、复制按钮。
    还有一件事必须有：**明确告诉用户数量没有相加** —— 否则用户会以为那是总用量。
    """
    js = _js()

    assert "/recipe/library/shopping-list" in js, "没有调用买菜清单接口"
    assert "state.pickedRecipes" in js, "没有记住勾选的菜"
    assert "event.stopPropagation()" in js, "点勾选框会顺带打开详情"
    assert "navigator.clipboard.writeText" in js, "没有复制按钮的能力"
    assert "数量按原菜谱保留，没有相加" in js, (
        "界面上必须写明数量没有相加，否则会被当成总用量"
    )

def test_the_trip_can_be_exported_as_text():
    """行程导出（第 130 步）：文本由服务端渲染，前端只复制。

    两件事必须有：调 `/api/trip/text`、失败时**不假装复制成功**。
    """
    js = _js()

    assert "/trip/text" in js, "没有调用导出接口"
    assert "navigator.clipboard.writeText(text)" in js, "没有真正复制文本"
    assert "已复制到剪贴板" in js, "复制成功要有反馈"
    assert "复制失败" in js, "复制失败也必须说出来，不能显示成功"

def test_the_destination_field_suggests_common_cities():
    """目的地候选（第 219 步）。

    三条边界：清单真的填进了 datalist、填过之后能记住、**没列出来的城市照样能提交**
    （所以不能在校验里拿清单当白名单）。
    """
    js = _js()
    html = _html()

    assert 'list="travel-city-options"' in html, "输入框没挂 datalist"
    assert 'id="travel-city-options"' in html, "没有 datalist 容器"
    assert "fillDestinationOptions()" in js, "候选没有被填充"
    assert "rememberDestination(" in js, "填过的城市没有被记住"
    assert "PET_CITY_WHITELIST" not in js, "不能拿清单当白名单：清单外的城市也要能提交"
    assert "本地清单" in js or "不联网" in js, "注释要写明这是本地清单，不是推荐系统"

def test_the_trip_can_be_downloaded_as_markdown():
    """行程存成 .md（第 218 步）。

    内容仍然由服务端渲染（和「复制行程」同一份渲染器），前端只负责下载；
    浏览器不支持存文件时必须**说实话**，不能显示「已存为」。
    """
    js = _js()
    html = _html()

    assert 'id="trip-markdown"' in html, "没有「存为 .md」按钮"
    assert "/trip/markdown" in js, "没有调用 Markdown 导出接口"
    assert "URL.createObjectURL" in js, "没有真正生成可下载的文件"
    assert "link.download" in js, "没有设置下载文件名（服务端拼的那个）"
    assert "这个环境不支持存文件" in js, "不支持时必须明说，不能假装存好了"

def test_the_recipe_window_has_a_new_session_entry():
    """食谱「新建会话」（第 131 步）。

    四条边界：真调 reset 接口、调之前先确认、会话 id 不变、顺手清掉勾选。
    """
    js = _js()
    html = _html()

    assert 'id="recipe-new"' in html, "没有新建会话按钮"
    assert 'id="recipe-confirm"' in html, "没有窗口内确认框"
    assert "/recipe/reset" in js, "没有调用 reset 接口"
    assert "startNewRecipeSession" in js
    assert "openRecipeConfirm" in js, "必须先确认再清空"
    assert "recipeSessionId()" in js, "不换会话 id：这是新建对话，不是换身份"
    assert "state.pickedRecipes.clear()" in js, "勾选的菜属于上一段对话，要一起清"

def test_the_recipe_log_is_not_wrapped_in_an_extra_div():
    """右侧栏消失的回归防线（第 131 步之后修）。

    真事：为了给确认框找定位上下文，我给 `#recipe-log` 包了一层
    `.recipe-confirm-host`。而 `#recipe-log` 是 `.recipe-chat-main`（flex 列）的
    直接子项，`flex: 1 1 auto` 与 `min-height: 0` 只在它是 flex 子项时生效 ——
    多包一层之后布局塌了，**右侧的忌口/想要栏被挤出窗口**（用户截图）。

    修法是让确认框挂到 `.recipe-chat-main` 上（它现在 `position: relative`），
    不去动 `#recipe-log` 的父级。这条测试钉住"别再包"。
    """
    html = _html()
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "recipe-confirm-host" not in html, "`#recipe-log` 又被包了一层"
    assert 'id="recipe-confirm"' in html
    # 定位上下文改由 .recipe-chat-main 提供
    assert ".recipe-chat-main { position: relative; }" in css, (
        "确认框需要 .recipe-chat-main 提供定位上下文"
    )
    # 右侧栏与 log 的 flex 语义都要在
    assert ".recipe-aside {" in css and "width: 296px" in css
    assert ".recipe-log {" in css and "flex: 1 1 auto" in css

def test_the_last_trip_can_be_resumed_after_reopening_the_window():
    """关掉出行窗口再打开，上次那份还能点回去（第 197 步 / 评价15 B2）。

    三条不能松的约束：
    1. 只记 `trip_id`，**不把整份行程存前端** —— 行程的唯一数据源是后端那次生成的结果；
    2. 用的必须是早就有的 `GET /api/trip/{id}`（第 41 步留的接口，一直没人调）；
    3. 后端说 404（服务重启过）时要**忘掉 id 且不挂提示条**，不做一个点进去必然失败的入口。
    """
    js = _js()

    assert "const TRIP_KEY = 'pet.trip.last'" in js, "记住上次行程的键没定义"
    assert "rememberTripId(data.trip_id || trip.trip_id)" in js, \
        "生成成功后要把后端给的 trip_id 记下来"
    assert "localStorage.setItem(TRIP_KEY" in js and "localStorage.removeItem(TRIP_KEY)" in js
    assert "`${API}/trip/${encodeURIComponent(tripId)}`" in js, \
        "恢复要走 GET /api/trip/{id}，且 id 必须编码"

    # 404 分支：忘掉 id 直接 return，不能往下走到"挂提示条"
    resume_block = js.split("async function offerResumeTrip()", 1)[1].split("\n  }", 1)[0]
    not_found = resume_block.split("response.status === 404", 1)[1].split("}", 1)[0]
    assert "forgetTripId()" in not_found, "404 时要忘掉记住的 id"
    assert "travelResume.hidden = false" not in not_found, "404 时不该挂出提示条"

    # 恢复动作要自己切页：正常提交是"生成中"那句顺手切的，恢复路径没有那一步
    resume_fn = js.split("function resumeTrip()", 1)[1].split("\n  }", 1)[0]
    assert "showTravelPage('plan')" in resume_fn, \
        "恢复后要显式切到计划页，否则概览渲染在看不见的页面上"


def test_the_trip_submit_sends_an_idempotency_key():
    """出行提交要带 `Idempotency-Key`（第 135 步）。

    一次"提交"对应一个 key：重发时服务端回放上一份行程，而不是再花 2~25 秒 + 配额生成一份。
    """
    js = _js()

    assert "Idempotency-Key" in js, "没有带幂等键"
    assert "crypto.randomUUID" in js, "key 要随机且唯一"

def test_the_recipe_chat_opens_scrolled_to_the_last_message():
    """打开食谱对话时要停在**最后一条**（第 137 步，用户反馈）。

    踩过的坑：`appendRecipeMessage` 里本来就有"滚到底"，但恢复历史时对话区还是
    `hidden`（`scrollHeight` 是 0），所以那句等于没写。必须在**露出来之后**再滚一次。
    """
    js = _js()

    assert "function scrollRecipeLogToBottom" in js, "缺少统一的滚到底助手"
    # 页面露出来之后（finish 里）要补一次，且要等一帧让布局算完
    finish_block = js[js.index("const finish = () => {"):][:600]
    assert "requestAnimationFrame(scrollRecipeLogToBottom)" in finish_block, (
        "露出对话区之后没有补滚动 —— 用户会从最上面开始翻"
    )
    # 历史晚到（页面已可见）时也要停在最后
    restore_block = js[js.index("function restoreRecipeSession"):][:1600]
    assert "scrollRecipeLogToBottom()" in restore_block

    # 卡片是异步补回来的（第 137 步用户改动）：补完之后要再滚一次，
    # 否则那次"滚到底"在卡片长出来之后就过期了
    cards_block = js[js.index("async function restoreRecipeAnswerCards"):][:900]
    assert "scrollRecipeLogToBottom()" in cards_block, (
        "卡片补回来之后没有再滚 —— 用户看到的仍不是最后一条"
    )

def test_the_chat_panel_keeps_a_gap_for_its_two_toolbar_buttons():
    """聊天面板顶部要留一条不参与滚动的空白带（第 140 步，用户截图）。

    `#chat-clear`（🗑）与 `#chat-close`（×）是绝对定位、top:6、高 28px；
    而 `.chat-log` 的第一条气泡原本从 18px 开始 —— 两条正好叠在一起。
    修法是给 `.chat` 加 `padding-top: 36px`：padding 属于容器，
    气泡再怎么滚动也进不到那条带子里。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    start = css.index(".chat {")
    block = css[start:css.index("}", start) + 1]

    assert "padding-top: 36px" in block, "面板顶部没有给按钮留位置"
    # 两个按钮都在 28px 高、top:6 的位置 —— 36px 足够盖住它们
    for selector in (".chat-clear {", ".chat-close {"):
        btn = css[css.index(selector):css.index("}", css.index(selector)) + 1]
        assert "height: 28px" in btn, f"{selector} 的高度变了，36px 的前提要重新算"

def test_the_chat_window_shows_the_opening_line_from_the_server():
    """开场白由后端给、前端只画（第 143 步）。

    三个前提缺一不可：用户没在此期间发过消息、没点清空、窗口里还是空的。
    少了任何一条都会出现"开场白盖住用户刚说的话"这种怪事。
    """
    js = _js()

    assert "data.greeting" in js, "前端没有用后端给的开场白"
    block = js[js.index("data.greeting"):][:400]
    assert "beforeCount" in block, "必须确认期间没人动过"
    assert "chatCleared" in block, "清空过的会话不许再画开场白"

def test_the_shortcut_row_collapses_behind_a_chevron():
    """功能栏收在一个「›」里（第 146 步，用户要求）。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = _js()

    assert 'id="chat-more"' in html, "缺少折叠按钮"
    assert 'aria-controls="chat-chips"' in html, "按钮应声明它控制哪块"
    assert 'aria-expanded="false"' in html, "默认收起"
    # 按钮和胶囊行要在同一个容器里，容器带 is-open 决定展开
    assert 'id="chat-tools"' in html
    tools_block = html[html.index('id="chat-tools"') - 200:][:600]
    assert 'id="chat-chips"' in tools_block, "胶囊行应当在 .chat-tools 里"

    assert "function toggleChatTools" in js
    assert "is-open" in js, "展开状态要有可断言的类名"
    # 打字时收起这条既有行为不能被弄丢
    assert "state.chatToolsOpen" in js


def test_the_chips_row_has_no_scrollbar():
    """功能栏的横向滚动条要去掉（第 146 步，用户明确"小侧边滑动条可以删掉"）。

    注意：去掉的是**滚动条**，不是滚动能力 —— 滚轮/触控板/拖动仍然能滑。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".chips {"):]
    block = block[:block.index("}") + 1]

    assert "scrollbar-width: none" in block, "Firefox 下仍会画滚动条"
    assert ".chips::-webkit-scrollbar" in css and "display: none" in css[
        css.index(".chips::-webkit-scrollbar"):][:80
    ], "Chromium 下仍会画滚动条"


def test_the_chat_log_scrollbar_is_a_gray_thin_bar():
    """聊天记录的滚动条改成灰色细条（第 146 步）。

    不写这几条的话，Windows 会拿系统样式：带上下箭头、带槽底，在深色面板里很跳。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".chat-log {"):]
    block = block[:block.index("}") + 1]

    assert "scrollbar-color:" in block, "Firefox 需要 scrollbar-color"
    assert "scrollbar-width: thin" in block, "细条"

    thumb = css[css.index(".chat-log::-webkit-scrollbar-thumb"):][:200]
    assert "background: #4b5058" in thumb, "Chromium 的滑块要设成灰色"
    assert "border-radius" in thumb, "滑块要圆角，否则还是方块"

    # 上下箭头要去掉
    assert ".chat-log::-webkit-scrollbar-button" in css
    assert "display: none" in css[css.index(".chat-log::-webkit-scrollbar-button"):][:60]


def test_the_wheel_has_an_actions_menu_instead_of_the_status_placeholder():
    """轮盘的「状态」换成「动作」（第 150 步，用户要求）。

    点它 → 桌宠旁边弹出**右键菜单样式**的竖排列表 → 点哪一项演哪个动作。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = _js()

    assert 'id="action-menu"' in html, "缺少动作菜单容器"
    assert 'role="menu"' in html, "菜单要声明 role=menu"

    assert "PET_ACTIONS" in js and "label: '擦擦灰'" in js, "缺少「擦擦灰」这个动作"
    assert "key: 'actions'" in js, "轮盘里缺少「动作」入口"
    # 「状态」占位应当已经不在了
    assert "label: '状态'" not in js, "「状态」占位应当被「动作」替换掉"
    assert "function openActionMenu" in js and "function closeActionMenu" in js
    # 桌面右键菜单的老规矩：点别处/按 Esc 都要收
    assert "document.addEventListener('click'" in js, "缺少「点外面关菜单」"
    assert "state.actionMenuOpen" in js


def test_the_action_menu_is_a_right_click_style_list():
    """动作菜单的样式：竖排 + 面板 + 悬停高亮（照桌面右键菜单，第 150 步）。

    第 159 步删掉了「晃晃」（CSS 抖动那个动作）后，`playDustWipe` / `is-wiping` /
    `.dust-puff` 这一套**一起删了** —— 两个动作现在都是视频素材。这里钉住菜单本身，
    以及"那套 CSS 特效没有被留下当死代码"。
    """
    js = _js()
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    menu = css[css.index(".action-menu {"):]
    menu = menu[:menu.index("}") + 1]
    assert "flex-direction: column" in menu, "菜单项应当由上而下排"
    assert ".action-item" in css

    assert "playDustWipe" not in js, "抖动函数应当随「晃晃」一起删掉"
    assert ".dust-puff" not in css, "灰尘粒子样式应当一起删掉"
    assert "@keyframes pet-wipe" not in css, "抖动关键帧应当一起删掉"


def _png_size(path) -> tuple[int, int]:
    """读 PNG 的宽高（IHDR 在固定偏移，不用第三方库）。"""
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG"
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def test_the_pet_has_an_idle_animation_played_every_ten_seconds():
    """待机动画：APNG 每 10 秒演一次（第 151 步，用户要求）。

    素材是 ffmpeg 从用户给的 mp4 里抠出来的（黑底 → 只删与边框连通的黑，
    所以黑裙子/蝴蝶结不会被吃掉），再由 app.js 定时重播。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = _js()
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert 'id="pet-idle"' in html, "缺少动作层"
    assert "<video" in html and 'id="pet-idle"' in html, "动作层得是 <video>（webm 要它才能播）"
    assert (STATIC_DIR / "actions" / "tea.webm").exists(), "缺少「喝茶」素材"


    # 节奏：**演完一个动作 → 静 10 秒 → 随机演下一个**
    # 第 174 步：照 dsh-pet"播完即接下一个"，间隔改成 0（不再静 10 秒）
    assert "IDLE_GAP_MS = 0" in js, "接力间隔应当为 0（连续动画链）"
    assert "function pickRandomAction" in js, "没有随机挑选"
    assert "Math.random" in js
    # 动作池里的**每个**动作都要有素材、有 id、有 label（数量不写死）
    for name in ("喝茶", "摸鱼", "擦擦灰"):
        assert f"label: '{name}'" in js, f"缺少动作：{name}"
    for asset in ("tea", "moyu", "wipe"):
        assert f"asset: 'actions/{asset}.webm'" in js, f"缺少素材：{asset}"
    assert "label: '晃晃'" not in js, "「晃晃」已经删掉了"
    assert "playDustWipe" not in js, "配套的 CSS 抖动函数也应当删掉"
    # webm 重播靠 currentTime 归零；APNG 那条（换 ?cycle= URL）作为兼容保留
    assert "currentTime = 0" in js, "视频动作没有把播放位置归零"
    assert "?cycle=" in js, "APNG 的回退路径被删掉了"

    assert ".pet-idle" in css and ".pet.is-idle-playing .pet-idle" in css

    # 静帧与动画要同一比例，否则每 10 秒跳一下
    still = _png_size(STATIC_DIR / "pet.png")
    anim = (300, 300)   # webm 的固定画布：300x300（1.0），由 import_action.py 统一产出
    assert abs(still[0] / still[1] - anim[0] / anim[1]) < 0.01, (
        f"静帧 {still} 与动画画布 {anim} 比例不一致，切换时会跳"
    )
    # 坑位的 CSS 变量也要与素材同比例
    css_pet = css[css.index("--pet-w:"):][:120]
    width = int(css_pet.split("--pet-w:")[1].split("px")[0])
    height = int(css_pet.split("--pet-h:")[1].split("px")[0])
    assert abs(width / height - anim[0] / anim[1]) < 0.02, (
        f"坑位 {width}x{height} 与素材比例 {anim[0]}/{anim[1]} 不一致"
    )


def test_the_pet_does_not_float_anymore():
    """桌宠不再有常驻的上下浮动（第 154 步，用户要求）。

    `pet-float` 是"桌宠还是一张静止图"那会儿加的，4 秒 ±6px，让它显得活着。
    现在动作素材本身就在动，两层叠在一起反而更晃 —— 所以整条删掉。
    """
    raw = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    # 只看**去掉注释**的 CSS：说明文字里当然可以提到 pet-float（那是在解释为什么删）
    css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)

    assert "pet-float" not in css, "浮动动画还有残留"
    assert "@keyframes pet-float" not in css
    # 拖着走 / 悬停时"暂停动画"的规则也应当一起清掉（没有动画可暂停了）
    assert "animation-play-state" not in css
    # 第 159 步：「晃晃」删掉后，配套的 CSS 抖动也应当一起消失
    assert ".pet-body.is-wiping" not in css
    assert "@keyframes pet-wipe" not in css


def test_actions_are_split_into_pools_with_weights():
    """动作分池 + 权重（第 162 步，照 dsh-pet 的 `animations` / `animationWeights`）。

    dsh-pet 把动画分成 idle / turn / drag / clicks / moves / events 六个池，
    用 `animationWeights` 决定"抽到哪一类"。我们动作少，只取最小可用的两档：
    `idle`（待机会自动播）与 `manual`（只在你点菜单时播），外加相对权重。
    """
    js = _js()

    assert "pool: 'idle'" in js, "没有 idle 池标记"
    assert "pool: 'manual'" in js, "没有 manual 池标记"
    assert "weight:" in js, "没有权重字段"
    # 随机挑选必须**按池过滤 + 按权重**
    block = js[js.index("function pickRandomAction"):][:900]
    assert "(action.pool || 'idle') === 'idle'" in block, "随机挑选没有按池过滤"
    assert "action.weight" in block, "随机挑选没有按权重"

    # 权重真的区分了频率：擦擦灰比喝茶常见
    wipe = js[js.index("id: 'wipe'"):][:260]
    tea = js[js.index("id: 'tea'"):][:260]
    assert "weight: 2" in wipe, "擦擦灰应当权重更高"
    assert "weight: 1" in tea, "喝茶应当权重更低"


def test_the_menu_lists_every_action_regardless_of_pool():
    """菜单要列出**全部**动作（手动点不受池限制）。"""
    js = _js()

    block = js[js.index("function renderActionMenu"):][:700]
    assert "PET_ACTIONS.forEach" in block, "菜单没有遍历全部动作"
    assert "pool" not in block, "菜单不应当按池过滤"


def test_the_drag_pose_is_not_a_menu_action():
    """拖拽姿势是**独立的一档**（第 163 步，参考 dsh-pet 的 `drag` 池）。

    dsh-pet 把"被拖拽"单独放一个池：只由拖拽触发，不进随机链、也不在动作菜单里。
    我们照做 —— `DRAG_ACTION` 不在 `PET_ACTIONS` 里，所以菜单里看不到它。
    """
    js = _js()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert (STATIC_DIR / "actions" / "drag-lift.webm").exists(), "缺少「被拎起来」素材"
    assert (STATIC_DIR / "actions" / "drag-swing.webm").exists(), "缺少「来回荡」素材"
    assert "const DRAG_LIFT" in js and "const DRAG_SWING" in js
    assert "function startDragPose" in js and "function stopDragPose" in js
    # 提起播完要自动切到「荡」并循环。两条路都要在：
    #  `ended`（真实播放结束）与**时长兜底计时器**（无头浏览器不推进视频播放，
    #  `ended` 永远不来 —— 烟测就是这种情况）。
    ended_block = js[js.index("addEventListener('ended'"):][:520]
    assert "switchDragToSwing()" in ended_block, "`ended` 里没有切到「来回荡」"
    switch_block = js[js.index("function switchDragToSwing"):][:600]
    assert "playDragSwing()" in switch_block, "切换函数没有真的去播「来回荡」"
    start_block2 = js[js.index("function startDragPose"):][:1600]
    assert "dragSwitchTimer" in start_block2 and "DRAG_LIFT.durationMs" in start_block2, (
        "缺少「提起 → 荡」的时长兜底（无头浏览器里 `ended` 不会触发）"
    )
    # 拖拽要**强制接管**：不因为"正在演别的动作"而拒绝
    start_block = js[js.index("function startDragPose"):][:900]
    assert "if (state.actionPlaying) return" not in start_block, (
        "拖拽应当强制接管正在演的动作，而不是拒绝"
    )
    assert "clearActionTimers()" in start_block, "拖拽开始前要清掉待机计时"

    drag_block = js[js.index("function onPetPointerMove"):][:900]
    assert "startDragPose()" in drag_block, "开始拖动时没有切姿势"
    up_block = js[js.index("function onPetPointerUp"):][:700]
    assert "stopDragPose()" in up_block, "松手时没有停姿势"

    # 循环播：手按着的时候一直在演
    assert "loop = true" in js

    # 菜单里不该出现它
    actions_block = js[js.index("const PET_ACTIONS = ["):]
    actions_block = actions_block[:actions_block.index("];")]
    assert "drag" not in actions_block, "拖拽姿势不该出现在动作菜单里"

    assert 'id="pet-idle"' in html and "<video" in html


def test_the_idle_breathing_clip_exists_and_has_the_highest_weight():
    """待机呼吸素材（第 174 步）：进 idle 池、权重最高、10 秒。

    dsh-pet 就是这么放待机的：`animations.idle = ["待机呼吸休闲"]`，
    再加上 `animationWeights.idle = 10` —— 它没有单独的"循环模式"，
    只是把待机当成池子里权重最高的一段。
    """
    meta = json.loads((STATIC_DIR / "actions" / "idle.json").read_text(encoding="utf-8"))
    assert meta["pool"] == "idle"
    assert meta["weight"] >= 4, f"待机权重应当最高（实际 {meta['weight']}）"
    assert 9.5 <= meta["seconds"] <= 11, f"待机素材应当是 10 秒左右（实际 {meta['seconds']}）"
    assert (STATIC_DIR / "actions" / "idle.webm").exists()


def test_moyu_is_a_manual_action():
    """「摸鱼」只在菜单点它时播（第 171 步，b3）。

    它是 11.5 秒的长动作：待机里自动演会显得"叫它也不理、自己在那儿忙"，
    所以和「喝茶」（10.1 秒）一样放 manual 池。待机只剩「擦擦灰」（8 秒）。
    """
    js = _js()
    assert (STATIC_DIR / "actions" / "moyu.webm").exists(), "缺少摸鱼素材"
    block = js[js.index("id: 'moyu'"):][:260]
    assert "label: '摸鱼'" in block
    assert "pool: 'manual'" in block, "长动作应当只在手动时播"

    # 第 178 步：擦擦灰也进了 manual，idle 池只剩「待机」
    wipe = js[js.index("id: 'wipe'"):][:260]
    assert "pool: 'manual'" in wipe, "擦擦灰应当改成手动"


def test_the_action_catalog_comes_from_the_backend():
    """动作目录由后端提供（第 172 步，c1）。

    以前加动作要改这里的 `PET_ACTIONS`；现在改成"扫 static/actions/*.json"，
    前端只负责拉一次并用它顶掉内置那份。拉不到时**必须**还能用内置那份 ——
    接口挂了不该让桌宠没动作。
    """
    js = _js()

    assert "async function loadPetActions()" in js
    assert "${API}/pet/actions" in js, "没有请求动作目录接口"
    assert "PET_ACTIONS.splice(0, PET_ACTIONS.length, ...usable)" in js, (
        "拿到目录后要顶掉内置那份"
    )
    assert "DRAG_LIFT.asset = lift.asset" in js, "拖拽素材也要跟着目录走"

    # 兜底：catch 里保留内置动作，不许清空
    loader = js[js.index("async function loadPetActions"):][:1600]
    assert "catch (error)" in loader and "用内置那份" in loader

    # 启动时真的调用
    init_block = js[js.index("function init()"):][:400]
    assert "loadPetActions()" in init_block, "启动时没有拉动作目录"


@pytest.mark.skipif(
    not (PROJECT_ROOT / "scripts" / "set_action_meta.py").is_file(),
    reason="set_action_meta.py 是一次性脚本，第 271 步起只在本机",
)
def test_set_action_meta_script_exists():
    """改元数据要用脚本，不能靠 shell 传中文参数（第 172 步踩过的坑）。"""
    script = PROJECT_ROOT / "scripts" / "set_action_meta.py"
    text = script.read_text(encoding="utf-8")
    assert "--label" in text and "--pool" in text and "--role" in text
    assert "GBK" in text, "注释里应当写明为什么不能走 shell 参数"


def test_a_finished_action_cannot_kill_the_next_one():
    """上一个动作的残留回调不许掐断下一个动作（第 176 步的 bug）。

    真事：`playAction` 排了一个兜底计时器（`durationMs + 400`），
    而 `finishAction()` 只把引用置空、**没有 clearTimeout**。
    于是 `ended` 先到、下一个动作开演之后，上一任的计时器到点仍会 `finishAction()`
    —— 守卫只看 `actionPlaying`（此时为真），拦不住，下一个动作就被掐断。
    用户看到的是"待机做一半去擦擦灰，擦擦灰没做完又回待机"。

    修法：动作带 `actionToken`，`finishAction(token)` 只处理当前代号；
    并且 `finishAction` 里**真清掉** `state.actionTimer`。
    """
    js = _js()

    assert "actionToken: 0" in js, "缺少动作代号"
    assert "state.actionToken += 1" in js, "换动作时没有推进代号"

    finish = js[js.index("function finishAction"):][:700]
    assert "token !== state.actionToken" in finish, "finishAction 没有代号守卫"
    assert "window.clearTimeout(state.actionTimer)" in finish, (
        "finishAction 必须真清掉兜底计时器（只置空引用正是这个 bug）"
    )

    play = js[js.index("function playAction"):][:2200]
    assert "const token = state.actionToken" in play
    assert "() => finishAction(token)" in play, "兜底计时器没有带上代号"
