"""邮箱窗口：结构断言直接读 HTML/CSS，点击与渲染行为交给 `scripts/verify_mail.js`。
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
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_mail.js"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _mail_block() -> str:
    """index.html 里邮箱窗口那一段。

    **先剥掉 HTML 注释**：注释里会写"没有勾选框/删除/移动/更多"来解释设计，
    那是说明文字不是标记；不剥掉的话，"不该有删除"这条会撞上自己的注释。

    边界取到 `mail-modal` 与 `recipe-modal` 之间——第 60 步食谱窗口新增了
    图片预览的 `<img>`，如果取到 `id="pet"` 就会把食谱窗口那段也框进来，
    让"邮箱里不该有图片"这条误报。
    """
    html = _html()
    start = html.index('id="mail-modal"')
    end = html.index('id="recipe-modal"')
    return re.sub(r"<!--[\s\S]*?-->", "", html[start:end])


# ------------------------------------------------------------------ 窗口


def test_mail_window_size_matches_the_request():
    """尺寸按原文写死：min(1100px, 92vw) × min(820px, 90vh)，一个字都不能差。

    用 height 而不是 min-height：用户给的是一组确定的两维尺寸，
    窗口不该被内容撑高（内容装不下由内部区域自己滚）。
    """
    rule = re.search(r"\.modal-box--mail\s*\{([^}]*)\}", _css())
    assert rule, "找不到 .modal-box--mail 规则"
    body = rule.group(1)

    assert "width: min(1100px, 92vw)" in body, f"宽度不对：{body}"
    assert "height: min(820px, 90vh)" in body, f"高度不对：{body}"
    # 贴边两栏版式：默认内边距必须归零
    assert "padding: 0" in body, "窗口内是左右两栏，不该留默认内边距"


def test_login_and_inbox_share_one_window():
    """原文第 4 条：登入前后内容都在**这一窗口内**，不新开窗口。"""
    html = _html()
    block = _mail_block()

    assert html.count('id="mail-modal"') == 1, "邮箱窗口只能有一个"
    assert html.count('id="mail-page-login"') == 1
    assert html.count('id="mail-page-inbox"') == 1

    # 两页都必须落在同一个窗口块里
    login_at = block.index('id="mail-page-login"')
    inbox_at = block.index('id="mail-page-inbox"')
    assert login_at < inbox_at, "登入页应在收件箱页之前"

    # 窗口里只有这两个 section（没有第三个"新窗口"）
    assert block.count('class="mail-page ') == 2, "只应有两页"


def test_login_page_is_the_initial_state():
    html = _html()
    login_tag = re.search(r'<section[^>]*id="mail-page-login"[^>]*>', html).group(0)
    assert "hidden" not in login_tag, "登入页应是打开窗口时的初始状态"

    inbox_tag = re.search(r'<section[^>]*id="mail-page-inbox"[^>]*>', html).group(0)
    assert "hidden" in inbox_tag, "收件箱页初始应隐藏"


def test_logo_is_removed_but_space_is_reserved():
    """原文 3a：左上角 logo 去除，**暂时占位空白**。

    所以不能只是删掉——要留一块等高空白，否则版式会整体往上跳。
    """
    block = _mail_block()

    assert "mail-logo-slot" in block, "应保留一块等高的 logo 占位"
    assert "<img" not in block, "邮箱窗口里不该有任何图片（logo 已去除）"
    assert "Moil" not in block, "不该照搬参考图里的品牌名"

    # 两页各留一个（登入页在左上角，收件箱页在左栏顶部）
    assert block.count("mail-logo-slot") == 2, "登入页与收件箱页各应留一块占位"


def test_toolbar_below_search_is_removed():
    """原文 3b：搜索框下方的 勾选框 / 删除 / 移动 / 更多 全部去掉。"""
    block = _mail_block()

    assert 'type="checkbox"' not in block, "不该有勾选框"
    for word in ("删除", "移动", "更多"):
        assert word not in block, f"不该有「{word}」"


def test_login_page_has_the_centered_elements_from_the_reference():
    """原文第 2 条：中心有这些元素，其余地方空白即可（参考图 2）。"""
    block = _mail_block()

    assert 'id="mail-title"' in block and "添加邮箱" in block
    assert "支持个人邮箱和企业邮箱" in block
    assert 'id="mail-address"' in block, "缺邮箱账号输入框"
    assert 'id="mail-secret"' in block, "缺授权码输入框"
    assert 'id="mail-submit"' in block and ">添加<" in block
    assert 'id="mail-service-toggle"' in block and "选择邮箱服务" in block


def test_login_inputs_keep_accessible_names():
    """输入框照图 2 不写可见标签（只有灰字占位），但无障碍不能跟着丢。"""
    block = _mail_block()

    for element_id, label in (("mail-address", "邮箱账号"), ("mail-secret", "授权码")):
        tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', block).group(0)
        assert f'aria-label="{label}"' in tag, f"{element_id} 缺 aria-label"
        assert f'placeholder="{label}"' in tag, f"{element_id} 缺占位文字"


# ------------------------------------------------------------------ 收件箱


def test_inbox_has_rail_search_and_list():
    block = _mail_block()

    assert 'class="mail-rail"' in block, "缺左侧文件夹栏"
    assert 'id="mail-search"' in block, "缺搜索框"
    assert 'id="mail-list"' in block, "缺邮件列表"
    assert 'id="mail-state"' in block, "缺状态提示（否则失败就是一片空白）"


def test_folder_list_comes_from_the_backend_not_the_html():
    """左侧四项由后端按账号实际情况给，HTML 里一项都不写死。"""
    block = _mail_block()
    nav = block[block.index('id="mail-folders"'):]
    nav = nav[:nav.index("</nav>")]

    for label in ("收件箱", "星标邮件", "草稿箱", "垃圾桶"):
        assert label not in nav, f"「{label}」不该写死在 HTML 里，应由后端给"
    assert "<button" not in nav, "文件夹按钮应由 app.js 生成"


def test_only_imap_reachable_folders_are_offered():
    """「重要联系人」「附件管理」IMAP 拿不到，已跟用户确认本轮不做。

    这条钉住那个决定：不能因为参考图上画了就摆两个点不动的项。
    """
    from core.mailbox import FOLDERS

    assert [folder.key for folder in FOLDERS] == ["inbox", "flagged", "drafts", "trash"]
    labels = [folder.label for folder in FOLDERS]
    assert "重要联系人" not in labels, "重要联系人是通讯录，IMAP 没有这个接口"
    assert "附件管理" not in labels, "IMAP 没有「列出所有附件」的接口"


# ------------------------------------------------------------------ 接线


def test_mail_chip_opens_the_mail_window():
    """胶囊「邮箱」从第 24 步的 disabled 占位改成真实入口。"""
    js = _js()

    entries = re.search(r"const CHIP_ENTRIES = \[([\s\S]*?)\];", js).group(1)
    assert "id: 'mail', label: '邮箱', panel: 'mail'" in entries

    dialogs = re.search(r"function dialogEls\(kind\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "kind === 'mail'" in dialogs, "dialogEls 要认识 mail"
    assert "el.mailModal" in dialogs, "mail 应指向邮箱窗口"


def test_mail_state_is_reset_on_open():
    """每次打开都要问后端"还登着吗"，而不是留着上次的界面。"""
    js = _js()
    open_dialog = re.search(r"function openDialog\(kind\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "kind === 'mail'" in open_dialog
    assert "prepareMail()" in open_dialog, "打开邮箱窗口时应先问一次登入状态"

    # 登入状态由后端说了算（服务重启内存清空 → 退回登入页）
    prepare = re.search(r"async function prepareMail\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/status" in prepare, "要用 /api/mail/status 恢复状态"


def test_secret_is_never_kept_in_the_frontend():
    """授权码只往后端送一次，不写进任何前端存储。"""
    js = _js()

    assert "mailSecret.value = ''" in js, "登入成功后应清空授权码输入框"
    # 不能把授权码塞进 localStorage / sessionStorage
    for store in ("localStorage", "sessionStorage"):
        found = re.findall(rf"{store}\.setItem\(([^)]*)\)", js)
        assert all("secret" not in call.lower() for call in found), (
            f"{store} 里不该出现授权码：{found}"
        )


def test_mail_requests_are_wired_to_the_right_endpoints():
    js = _js()
    for endpoint in (
        "/mail/providers", "/mail/login", "/mail/logout",
        "/mail/status", "/mail/folders", "/mail/messages", "/mail/flag",
    ):
        assert endpoint in js, f"app.js 没有调用 {endpoint}"


# ------------------------------------------------------------ 历史邮件翻页


def test_history_paging_is_wired_on_the_frontend():
    """用户报的"历史邮件获取不到"：必须能往回翻，而且滚到底自动续。

    以前这里只有"取最新 N 封"，更早的**没有任何入口**——不是服务器挡着，
    是压根没提供这个参数。所以这条要钉住 offset 与自动续载两件事。
    """
    js = _js()

    assert "offset=" in js, "请求里必须带 offset，否则历史邮件永远取不到"
    assert "mailCache" in js, "缺按文件夹的缓存"
    assert "newestUid" in js, "缓存要记住最大 UID，后台查新才有基准"
    assert "loading" in js, "缺重入保护（滚得快会连着触发）"
    assert "async function loadMailMessages(reset = true)" in js, (
        "loadMailMessages 要能区分「首次加载」和「追加下一页」"
    )
    assert "function onMailListScroll" in js, "缺少滚到底自动加载"
    assert "on(el.mailList, 'scroll', onMailListScroll)" in js, "scroll 事件没绑上"
    # 列表末尾要说一句话，否则滚到底不动会让人分不清"到底了"还是"卡住了"
    assert "已经到底了" in js and "mail-more" in js


def test_mail_state_is_per_folder_not_shared():
    """第 36 步问题 2 的根因：以前只有一份共享列表状态。

    只要有 `state.mailMessages` 这种"当前列表"，A 文件夹的响应回来时
    用户可能已经切到 B，邮件就串到 B 的界面上。所以那份状态必须彻底消失，
    改成按文件夹各自缓存。
    """
    js = _js()

    for stale in ("state.mailMessages", "state.mailOffset",
                  "state.mailHasMore", "state.mailTotal", "state.mailLoading"):
        assert stale not in js, f"{stale} 是共享的单文件夹状态，必须改成按文件夹缓存"

    assert "state.mailCache = {}" in js
    assert "function mailCacheFor(key)" in js, "缺按文件夹取缓存的入口"


def test_stale_responses_cannot_paint_another_folder():
    """响应必须写回**发起请求时**那个文件夹的缓存，且只有还看着它时才渲染。

    这三条一起才挡得住"点收件箱、立刻点草稿箱，结果草稿箱里出现收件箱的邮件"。
    """
    js = _js()
    load = re.search(
        r"async function loadMailMessages\(reset = true\)\s*\{([\s\S]*?)\n  \}", js
    ).group(1)

    assert "const folder = state.mailFolder;" in load, (
        "必须在发请求前把文件夹固定下来"
    )
    assert "const cache = mailCacheFor(folder);" in load, "结果要写进那个文件夹的缓存"
    assert "cache.token += 1" in load and "token !== cache.token" in load, (
        "同一文件夹的并发请求要靠 token 分出新旧，否则会互相覆盖"
    )
    assert "if (folder === state.mailFolder) {" in load, (
        "只有还在看这个文件夹时才渲染——否则就是往别的文件夹界面上画"
    )
    assert "renderMailListSafely(" in load, "渲染要走「别丢位置」的包装器"
    # 请求 URL 也要用固定下来的那个 folder，不能用 state.mailFolder
    assert "folder=${encodeURIComponent(folder)}" in load, (
        "URL 里的 folder 要用固定下来的那个"
    )

def test_revisiting_a_folder_uses_the_cache_and_only_checks_for_new_mail():
    """用户要求：加载过的文件夹再回来时先显示缓存，之后只默默查有没有新邮件。"""
    js = _js()

    selector = re.search(
        r"function selectMailFolder\(key\)\s*\{([\s\S]*?)\n  \}", js
    ).group(1)

    assert "if (cache.loaded)" in selector, "有缓存时应走缓存这条路"
    assert "renderMailListSafely('top')" in selector, (
        "有缓存时要立刻渲染（并且回到顶部，因为是换了文件夹），不能先闪加载中"
    )
    assert "checkForNewMail(key)" in selector, "之后要后台静默查新"
    assert "loadMailMessages();" in selector, "没缓存时才真去加载"

    # 后端：查新接口要接上，并且带上基准 UID
    assert "/mail/check" in js, "app.js 没有调用查新接口"
    assert "since_uid=" in js, "查新要带上「我手上最大的 UID」作基准"
    # 虚拟文件夹要限流（它只能整份重算）
    assert "MAIL_CHECK_THROTTLE_MS" in js and "MAIL_CHECK_MIN_GAP_MS" in js


def test_check_endpoint_is_wired_on_the_backend():
    routes = (PROJECT_ROOT / "api" / "mail_routes.py").read_text(encoding="utf-8")
    mailbox = (PROJECT_ROOT / "core" / "mailbox.py").read_text(encoding="utf-8")

    assert '@router.get("/check"' in routes
    assert "UIDNEXT" in mailbox, "STATUS 要多要一个 UIDNEXT，否则没法便宜判断有没有新邮件"
    assert "def check_new(" in mailbox
    # 便宜路径：UIDNEXT 没超过基准就直接返回，不 SELECT、不 FETCH
    assert "uid_next <= since + 1" in mailbox, "缺「没有新邮件」的便宜路径"
    assert 'f"{since + 1}:*"' in mailbox, "有新邮件时只该按 UID 区间取新的那几封"


def test_history_paging_is_wired_on_the_backend():
    mailbox = (PROJECT_ROOT / "core" / "mailbox.py").read_text(encoding="utf-8")
    routes = (PROJECT_ROOT / "api" / "mail_routes.py").read_text(encoding="utf-8")

    # 分页按 offset 从 UID 列表的尾部往回切
    assert "uids[start:end]" in mailbox, "应按 offset 切片，而不是只取尾巴那一段"
    assert "has_more" in mailbox and "has_more" in routes

    assert "offset: int = Query" in routes, "接口要收 offset"
    assert 'Query(default=0, ge=0' in routes, "offset 默认 0 且不能为负"


# ------------------------------------------------ 第 61 步：不丢位置 / 防抖 / 并发


def _block(js: str, signature: str, label: str) -> str:
    """截出一个顶层函数的函数体（非贪婪到下一个顶格 `}`）。"""
    found = re.search(
        rf"{re.escape(signature)}\s*\{{([\s\S]*?)\n  \}}", js
    )
    assert found, f"找不到 {label}"
    return found.group(1)


def test_render_preserves_scroll_and_focus():
    """全量重建会把 scrollTop 归零、把焦点丢到 <body>。

    这条在浏览器里是 DOM 行为；DOM 桩里靠"清空容器就把 scrollTop 归零"模拟。
    桩不模拟的话这段代码删掉测试也不会红——第 61 步的变异测试就是这么发现的。
    """
    js = _js()
    safe = _block(js, "function renderMailListSafely(place = 'keep')", "renderMailListSafely")

    assert "el.mailList.scrollTop = scrollTop" in safe, "重建前要记下 scrollTop，重建后放回去"
    assert "describeMailFocus()" in safe and "restoreMailFocus(remembered)" in safe, (
        "重建后要把焦点放回原处"
    )
    # 'top' 是换文件夹/换搜索词用的：那时保持旧位置反而是错的
    assert "place === 'top'" in safe and "scrollTop = 0" in safe

    focus = _block(js, "function describeMailFocus()", "describeMailFocus")
    assert "document.activeElement" in focus, "要读真实焦点"
    assert "'.mail-item'" in focus, "靠行定位"
    assert "mail-star" in focus, "要区分「焦点在星标上」和「焦点在行上」"

    restore = _block(js, "function restoreMailFocus(remembered)", "restoreMailFocus")
    assert "dataset.uid" in restore, "用 uid 找回同一行"


def test_star_toggle_does_not_rebuild_the_list():
    """普通文件夹里点星标只重画那个按钮，**不重建列表**。

    重建会把滚动位置和焦点一起丢掉；原地改元素既没这问题，也便宜得多。
    """
    js = _js()
    toggle = _block(js, "async function toggleMailStar(message)", "toggleMailStar")

    assert "repaintMailStars(message)" in toggle, "应当只重画星标"
    assert "renderMailList();" not in toggle, "普通情况不该全量重建"
    # 只有「星标邮件」里取消星标（整行要消失）才允许重排，而且要保持位置
    assert "renderMailListSafely('keep')" in toggle

    paint = _block(js, "function paintStarButton(star, starred)", "paintStarButton")
    assert "is-on" in paint and "aria-pressed" in paint, "笔画与 aria 一起改"


def test_search_input_is_debounced():
    """以前每敲一个字符就全量重建列表；加载 500 封时输入明显卡顿。"""
    js = _js()

    assert "MAIL_SEARCH_DEBOUNCE_MS = 120" in js
    assert "function onMailSearchInput" in js
    assert "window.clearTimeout(state.mailSearchTimer)" in js, "要做的是防抖不是节流"
    assert "on(el.mailSearch, 'input', onMailSearchInput)" in js
    assert "on(el.mailSearch, 'input', renderMailList)" not in js, (
        "绑回裸的 renderMailList 就等于没有防抖"
    )

    # 换文件夹 / 退出时要清掉还没触发的重排，否则会在新界面上重排旧结果
    select = _block(js, "function selectMailFolder(key)", "selectMailFolder")
    assert "cancelMailSearch()" in select
    logout = _block(js, "async function logoutMail()", "logoutMail")
    assert "cancelMailSearch()" in logout


def test_background_check_cannot_clobber_a_changed_list():
    """查新在 await 期间列表可能被整页换过/追加过，那份合并口径就作废了。

    不挡的话：查新按旧 offset 做 `+= fresh.length`，紧接着翻页用旧 offset 覆盖，
    结果 offset 少记几封 → 下次"加载更多"重复请求已加载的区间。
    """
    js = _js()
    load = _block(js, "async function loadMailMessages(reset = true)", "loadMailMessages")
    assert "cache.revision += 1" in load, "列表变了要告诉在途的查新"

    check = _block(js, "async function checkForNewMail(folder)", "checkForNewMail")
    assert "const revision = cache.revision;" in check, "await 之前要记下 revision"
    assert "if (cache.revision !== revision)" in check, "回来发现变过就丢弃这次合并"
    assert "cache.checkedAt = 0" in check, "丢弃后要让下次还能立刻重试（不能丢数据）"


def test_search_response_is_read_in_full():
    """IMAP 允许把一长串 UID 拆成多条响应行发的。

    以前 `uids_of()` 碰到第一个 bytes 就 return，第一行之后的 UID 被**静默丢掉**，
    表现成"历史邮件少了一截"且毫无报错。这条钉住修复。
    """
    mailbox = (PROJECT_ROOT / "core" / "mailbox.py").read_text(encoding="utf-8")

    assert "found.extend(bytes(item).split())" in mailbox, (
        "SEARCH 响应要把每一行都收进来"
    )
    assert "return bytes(item).split()" not in mailbox, (
        "不能读到第一行就返回——那会静默丢邮件"
    )


# ------------------------------------------------------------------ 行为


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_mail_flow_behaviour():
    """真跑一遍：打开窗口 → 登入 → 收件箱 → 搜索 → 星标 → 切文件夹 → 退出。"""
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"

    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "邮箱窗口行为验证未全部通过：\n" + output
    assert "失败 0 项" in output, "验证脚本报告了失败项：\n" + output
