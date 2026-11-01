"""食谱窗口：结构断言读 HTML/CSS/JS，点胶囊开窗、说一句话、出菜单这类行为交给 `scripts/verify_recipe.js`。
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
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_recipe.js"


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _recipe_block() -> str:
    """index.html 里食谱窗口那一段（紧挨在桌宠之前）。

    **先剥掉 HTML 注释**：注释里会写"去掉了顶部标题栏/用户区/设置"来解释设计，
    那是说明文字不是标记；不剥掉的话，"不该有设置"这条会撞上自己的注释。
    """
    html = _html()
    start = html.index('id="recipe-modal"')
    end = html.index('id="pet"')
    return re.sub(r"<!--[\s\S]*?-->", "", html[start:end])


# ------------------------------------------------------------------ 窗口


def test_recipe_window_size_matches_the_request():
    """尺寸按原文写死：min(1100px, 92vw) × min(820px, 90vh)，一个字都不能差。"""
    rule = re.search(r"\.modal-box--recipe\s*\{([^}]*)\}", _css())
    assert rule, "找不到 .modal-box--recipe 规则"
    body = rule.group(1)

    assert "width: min(1100px, 92vw)" in body, f"宽度不对：{body}"
    assert "height: min(820px, 90vh)" in body, f"高度不对：{body}"
    # 窗口内是两整屏（首页 / 对话页），仍然贴边
    assert "padding: 0" in body, "窗口内是整屏版式，不该留默认内边距"


def test_the_window_is_a_stack_of_two_screens_and_hosts_the_detail_overlay():
    """第 48 步改版后的骨架口径。

    首页与对话页是**两个互斥的整屏**（上下叠放，靠 hidden 切换），
    所以容器回到纵向排列；同时骨架必须 `position: relative`——
    菜谱详情浮层用 `absolute inset: 0` 盖在窗口内部，
    没有定位上下文的话它会跑到视口上、盖住整个页面。
    """
    rule = re.search(r"\.modal-box--recipe\s*\{([^}]*)\}", _css())
    body = rule.group(1)
    assert "flex-direction: column" in body, "首页/对话页是上下叠的两屏"
    assert "position: relative" in body, "详情浮层需要窗口作为定位上下文"
    # 骨架本身不设 display，免得压过 [hidden] 强制隐藏（第 5 步的约定）
    assert "display:" not in body, "窗口骨架不该自己设 display"


def test_the_removed_ui_from_the_reference_is_really_gone():
    """原文第 4 条：顶部标题栏、右上角用户区、左下角设置都不要。"""
    block = _recipe_block()

    for word in ("智能食谱助手", "用户", "设置"):
        assert word not in block, f"不该出现「{word}」（参考图里那部分按要求去掉了）"
    # 参考图右上角那个搜索框：按"控件"判，而不是按"搜索"两个字
    # （窗口里"正在搜索菜谱"是进度提示，不是搜索框）
    assert 'type="search"' not in block and "🔍" not in block, "不该有搜索框"
    # 第 60 步起**唯一允许**的图片是"待发送图片"的缩略图（上传识菜功能）。
    # 其余图片（logo / 头像 / 装饰图）仍然一律不许有。
    images = re.findall(r"<img[^>]*>", block)
    assert all("recipe-photo-thumb" in tag for tag in images), (
        f"食谱窗口里除了上传图片的缩略图，不该有其它图片：{images}"
    )
    # 也不该有头像之类的东西冒充"用户区"
    assert "avatar" not in block


def test_the_left_nav_is_gone():
    """第 48 步：左栏那两项导航（需求填写 / 饮食计划）去掉了。

    首页自己就是入口——点卡片看做法、点底部入口说话，两条并列的路。
    留着两项导航会让"首页已经能选菜"这件事显得多余。
    """
    block = _recipe_block()

    assert 'id="recipe-nav"' not in block, "左栏导航应当已删除"
    assert "需求填写" not in block, "「需求填写」这个标签不该再出现"
    assert "饮食计划" not in block, "「饮食计划」这个标签不该再出现"
    assert 'id="recipe-rail"' not in block and "recipe-rail" not in block


def test_home_shows_cards_and_a_chat_entry_at_the_bottom():
    """首页的骨架：上部是卡片横栏，底部是对话入口。

    用户原话："中上添加一个横栏，里面加上一些菜名的小卡片，支持左右滑动"、
    "对话入口改到最下方"。所以 DOM 顺序必须是 卡片 → 对话入口。
    """
    block = _recipe_block()
    home = block[block.index('id="recipe-home"'):block.index('id="recipe-chat-page"')]

    assert 'id="recipe-cards"' in home, "缺卡片横栏"
    assert 'id="recipe-chat-entry"' in home, "缺底部对话入口"
    assert home.index('id="recipe-cards"') < home.index('id="recipe-chat-entry"'), (
        "对话入口必须在卡片下面"
    )
    assert "您好，想吃点什么？" in home, "对话入口的文案按用户给的来"
    # 卡片横栏要能横滑
    assert "recipe-cards" in _css()
    cards_rule = re.search(r"\.recipe-cards\s*\{([^}]*)\}", _css())
    assert "overflow-x: auto" in cards_rule.group(1), "卡片栏必须能横向滚动"


def test_the_home_has_no_extra_meta_text_or_arrows():
    """用户明确去掉的三样：标题旁的"为你推荐 / 共 N 道"、卡片右侧的 ‹ ›、
    以及"已按你的忌口隐藏 N 条"这类说明。

    注意只切**首页那一段**来查：`_recipe_block()` 会一路含到后面的签到窗口，
    而签到窗口本来就有翻月用的 ‹ ›（那不是食谱的东西）。
    """
    block = _recipe_block()
    home = block[block.index('id="recipe-home"'):block.index('id="recipe-chat-page"')]

    for word in ("为你推荐", "共 30 道", "共30道", "已按你的忌口"):
        assert word not in home, f"不该出现「{word}」"
    for glyph in ("‹", "›", "◀", "▶"):
        assert glyph not in home, f"不该出现左右箭头「{glyph}」（翻页只靠「换一批」）"
    assert "换一批" in home, "翻页入口是「换一批」"


def test_home_and_chat_are_mutually_exclusive_screens():
    """首页与对话页在同一窗口里互斥显示，不新开窗口、不跳新页面。"""
    html = _html()
    block = _recipe_block()

    assert html.count('id="recipe-modal"') == 1, "食谱窗口只能有一个"

    home = re.search(r'<section[^>]*id="recipe-home"[^>]*>', block).group(0)
    chat = re.search(r'<section[^>]*id="recipe-chat-page"[^>]*>', block).group(0)
    assert "hidden" not in home, "打开窗口时停在首页（有卡片可看）"
    assert "hidden" in chat, "对话页初始应隐藏"


def test_the_card_detail_overlay_exists_and_is_inside_the_window():
    """点卡片弹出的详情浮层：盖在窗口内部，不是又一个全屏 modal。"""
    block = _recipe_block()

    detail = re.search(r'<section[^>]*id="recipe-detail"[^>]*>', block).group(0)
    assert "hidden" in detail, "详情浮层初始隐藏"
    assert 'role="dialog"' in detail and 'aria-modal="true"' in detail
    # 它必须落在 .modal-box--recipe 内部（而不是窗口外另开一层）
    assert block.index('id="recipe-detail"') > block.index("modal-box--recipe")
    for part in ("recipe-detail-title", "recipe-detail-body", "recipe-detail-close"):
        assert f'id="{part}"' in block, f"详情浮层缺 {part}"

    rule = re.search(r"\.recipe-detail\s*\{([^}]*)\}", _css())
    assert "position: absolute" in rule.group(1), "浮层要盖在窗口内部"
    assert "inset: 0" in rule.group(1), "浮层要铺满窗口内容区"


def test_recipe_content_is_not_hardcoded_in_the_frontend():
    """界面上的内容全部来自后端（用户要求"不要写死"）。

    第 50 步改版后右栏只剩一个"装标签的框"：标签的**文案、分组、选项**
    都来自 `GET /api/recipe/library/preferences`，前端不硬编码。
    """
    block = _recipe_block()

    for element_id in ("recipe-tags-avoid", "recipe-tags-want"):
        tag = re.search(rf'<[a-z]+[^>]*id="{element_id}"[^>]*>', block)
        assert tag, f"缺容器 {element_id}"

    # 后端提供的中文不该出现在 HTML 里（出现在 app.js 里也算写死）
    for text in ("减脂", "海鲜过敏", "忌口", "高蛋白", "不构成医疗或营养建议",
                 "香菜", "清淡", "小电锅"):
        assert text not in block, f"「{text}」不该写死在 HTML 里，应由后端给"

    js = _js()
    for key in ("days", "greeting"):
        assert f".{key}" in js, f"app.js 没有使用后端给的 schema.{key}"
    # 标签框的数据来源
    assert "/recipe/library/preferences" in js
    for key in ("avoid_options", "want_groups", "aversions"):
        assert f".{key}" in js, f"app.js 没有使用后端给的 preferences.{key}"


def test_the_multi_day_ui_is_completely_gone():
    """第 50 步：多天菜单整套从界面上删掉（用户："不需要生成多天食谱的功能，
    只要用户问的这一餐"）。

    旧断言（天数范围/夹取/生成中禁用/计划渲染/折叠区可达）按项目规矩
    **反转成"不该再有"**，而不是删掉了事。
    """
    js = _js()
    block = _recipe_block()

    for element_id in (
        "recipe-legacy", "recipe-days", "recipe-generate",
        "recipe-progress", "recipe-warnings", "recipe-plan", "recipe-empty",
    ):
        assert f'id="{element_id}"' not in block, f"界面上不该再有 {element_id}"

    for symbol in (
        "generateRecipePlan", "renderRecipePlan", "renderRecipeWarnings",
        "setRecipeGenerating", "recipeDaysValue", "showRecipeEmpty",
        "recipeMeal", "recipeSourceLinks",
    ):
        assert symbol not in js, f"代码里不该再有 {symbol}"

    assert "生成多天菜单" not in block, "旧流程入口应当已删除"


def test_recipe_window_keeps_accessibility_basics():
    """窗口有名字、输入有名字、状态区能读出来——改版不能把无障碍丢掉。"""
    block = _recipe_block()

    modal = re.search(r'<div[^>]*id="recipe-modal"[^>]*>', _html()).group(0)
    assert 'role="dialog"' in modal and 'aria-modal="true"' in modal
    assert 'aria-labelledby="recipe-title"' in modal
    assert 'id="recipe-title"' in block, "窗口标题元素要在（首页那个「食谱」小标题）"

    input_tag = re.search(r'<input[^>]*id="recipe-input"[^>]*>', block).group(0)
    assert 'aria-label="说说你的需求"' in input_tag

    log = re.search(r'<div[^>]*id="recipe-log"[^>]*>', block).group(0)
    assert 'role="log"' in log and 'aria-live="polite"' in log


# ------------------------------------------------------------------ 接线


def test_recipe_requests_are_wired_to_the_right_endpoints():
    js = _js()
    for endpoint in (
        "/recipe/schema", "/recipe/session",
        # 第 48 步：首页卡片来自知识库接口
        "/recipe/library",
        # 第 50 步：右栏标签框的选项与忌口词条
        "/recipe/library/preferences",
        # 第 50 步 50d：对话走 Agent 的「查库再答」
        "/recipe/ask",
    ):
        assert endpoint in js, f"app.js 没有调用 {endpoint}"

    # 旧的「一步步收集需求」与「多天生成」都不该再被调用
    assert "/recipe/message" not in js, "对话不再走需求收集接口"
    assert "/recipe/plan" not in js, "多天生成已删除"


def test_chat_answers_with_real_recipe_cards():
    """对话的回复后面要跟**真实菜谱卡片**（点开看做法）。

    卡片数据来自后端按 id 取库（`data.recipes`），不是模型说的话——
    这是"模型编不出库里没有的菜"的落地方式。
    """
    js = _js()

    send = re.search(r"async function sendRecipeMessage\(text\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/recipe/ask" in send
    assert "appendRecipeAnswerCards" in send
    # 第 50 步 50e：右栏「这次想要」的勾选要随请求发出去，
    # 否则点了标签等于没点（用户会遇到"勾了清淡还是给我辣的"）
    assert "wanted: state.recipeWanted" in send

    cards = re.search(r"function appendRecipeAnswerCards\(recipes\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "recipe-answer-card" in cards
    assert "openRecipeDetail(recipe.id)" in cards, "点卡片要打开详情"
    assert "recipe.name" in cards and "recipe.time_minutes" in cards

    rule = re.search(r"\.recipe-answer-card\s*\{([^}]*)\}", _css())
    assert rule, "缺少对话里卡片的样式"
    assert "cursor: pointer" in rule.group(1)


def test_home_cards_come_from_the_library_with_shuffle():
    """卡片是**真数据**（本地菜谱库），不是前端写死的示例。

    「换一批」= 再请求一次同一个接口、带 `shuffle=1` 让后端换一组；
    一次取 6 张（`RECIPE_CARDS_PER_BATCH`），正好一屏宽。
    """
    js = _js()

    assert "const RECIPE_CARDS_PER_BATCH = 6;" in js
    assert "limit=${RECIPE_CARDS_PER_BATCH}&shuffle=1" in js, (
        "卡片要按批次 + 随机取，否则「换一批」拿到的还是同一批"
    )

    loader = re.search(r"async function loadRecipeCards\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/recipe/library" in loader
    assert "renderRecipeCards(" in loader, "拿到数据要交给渲染"

    # 卡片上的中文（菜名/时长）来自接口数据，不是写死的数组
    card_fn = re.search(r"function renderRecipeCards\(payload\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "recipe.name" in card_fn and "recipe.time_minutes" in card_fn
    assert "dataset.id" in card_fn, "卡片要带上 id，点开详情时才找得到那道菜"


def test_empty_library_offers_an_action_instead_of_a_blank_home():
    """空库时首页不能是一片空白：给一个「载入示例菜谱」的明确动作。"""
    js = _js()

    render = re.search(r"function renderRecipeCards\(payload\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "el.recipeCardsEmpty.hidden = !empty" in render
    assert "el.recipeRefresh.hidden = empty" in render, "空库时「换一批」没有意义，要藏起来"

    seed = re.search(r"async function seedRecipeLibrary\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/recipe/library/seed" in seed and "method: 'POST'" in seed
    assert "loadRecipeCards()" in seed, "导入完要重新拉卡片，否则用户看不到变化"


def test_clicking_a_card_opens_a_detail_overlay_with_materials_and_steps():
    """点卡片 → 打开详情浮层，材料与做法都渲染出来；示例菜谱要标出来历。"""
    js = _js()

    open_fn = re.search(r"async function openRecipeDetail\(recipeId\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/recipe/library/${Number(recipeId)}" in open_fn
    assert "el.recipeDetail.hidden = false" in open_fn

    render = re.search(r"function renderRecipeDetail\(recipe\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "所需材料" in render and "制作流程" in render
    assert "recipe.ingredients" in render and "recipe.steps" in render
    assert "示例菜谱" in render, "source=seed 的菜必须标注来历（AI 整理、未核对）"

    # Esc 先收浮层，而不是把整个窗口关掉
    keydown = re.search(r"function onGlobalKeydown\(event\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "closeRecipeDetail()" in keydown, "Esc 要能关详情浮层"
    detail_at = keydown.index("closeRecipeDetail()")
    dialog_at = keydown.index("closeDialog()")
    assert detail_at < dialog_at, "详情浮层的 Esc 优先级要高于整个窗口"


def test_chat_entry_switches_to_the_full_conversation():
    """底部对话入口 → 首页滑走、对话页出现；对话页的 ← 回到首页。"""
    js = _js()

    view_fn = re.search(r"function showRecipeView\(view\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "is-leaving" in view_fn, "首页要播「向上滑走」的动画，不能直接 hidden"
    assert "el.recipeHome.hidden = true" in view_fn
    assert "el.recipeChatPage.hidden = false" in view_fn
    # 回首页时要把中间态清掉，否则下次进来首页还是"滑走"的样子
    assert "el.recipeHome.classList.remove('is-leaving')" in view_fn

    bind = re.search(r"function bindEvents\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "on(el.recipeChatEntry, 'click', () => showRecipeView('chat'))" in bind
    assert "on(el.recipeBack, 'click', () => showRecipeView('home'))" in bind
    assert "on(el.recipeRefresh, 'click'" in bind, "「换一批」要绑上"
    assert "on(el.recipeSeed, 'click'" in bind, "空库的「载入示例菜谱」要绑上"


def test_the_missing_gate_and_its_copy_are_gone():
    """第 50 步：整条"还差：…"提示与它的门禁都删掉了。

    用户原话："'还差…'文字可以去掉了，这有点影响用户体验，模型自己默默记忆和思考就行"。
    所以这里**反转**成"不该再有"：界面上没有那一行、代码里没有对应的渲染函数。
    """
    js = _js()
    block = _recipe_block()

    assert 'id="recipe-missing"' not in block, "「还差」那行应当已从界面删除"
    assert "renderRecipeMissing" not in js, "对应的渲染函数应当已删除"
    # 只看字符串字面量：注释里提到"不再显示还差…"是在解释这次改动，不是给用户看的文案
    literals = re.findall(r"'([^']*)'", js)
    assert not any("还差" in text for text in literals), (
        f"面向用户的文案里不该再有「还差」：{ [t for t in literals if '还差' in t] }"
    )
    assert "recipeMissing" not in js

    # 首页/对话页的界面文案里也不该再出现这两个旧标签
    for word in ("需求填写", "饮食计划"):
        assert word not in block, f"界面上不该再有「{word}」这个标签"


def test_closing_the_window_also_closes_the_detail_overlay():
    """回归：详情浮层是窗口内的覆盖层，关窗时必须一起收掉。

    否则"点卡片看详情 → 关窗 → 重开窗口"，旧详情会直接盖在首页上
    （自查时抓到的真缺陷，第 48 步修的）。
    """
    js = _js()

    close_dialog = re.search(r"function closeDialog\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "closeRecipeDetail()" in close_dialog, (
        "closeDialog 里要收掉详情浮层，否则重开窗口会看到旧详情"
    )


def test_recipe_uses_its_own_session_not_the_chat_one():
    """第 37 步：食谱窗口有自己的会话 id。

    理由有两条：需求画像不该和"跟九九闲聊"的记忆互相污染；
    将来做"创建新会话"时，只要换这个 key 就行，存储层不用动。
    """
    js = _js()

    assert "const RECIPE_SESSION_KEY = 'pet.recipe_session';" in js
    assert "function recipeSessionId()" in js
    assert "window.localStorage.setItem(RECIPE_SESSION_KEY, id)" in js

    # 食谱的请求必须带自己的 session_id，而不是聊天那个 sessionId()
    assert "session_id: recipeSessionId()" in js, "POST 要用食谱自己的会话 id"
    assert "session_id=${encodeURIComponent(recipeSessionId())}" in js, (
        "GET /session 也要用它"
    )


def test_the_window_state_comes_from_the_backend_on_open():
    """重开窗口时状态由后端给（服务重启内存清空 → 回到开场白），
    而不是"界面上次什么样就留什么样"（邮箱那一步的教训）。"""
    js = _js()

    prepare = re.search(r"async function prepareRecipe\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "loadRecipeSchema()" in prepare, "打开时要先取 schema"
    assert "/recipe/session" in prepare, "打开时要问一次这个会话聊到哪了"

    loader = re.search(r"async function loadRecipeSchema\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "/recipe/schema" in loader
    assert "if (state.recipeSchema) return state.recipeSchema;" in loader, (
        "schema 是静态结构，只该取一次"
    )

    restore = re.search(r"function restoreRecipeSession\(data\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "data.messages" in restore, "已有对话要恢复出来"
    assert "renderRecipeGreeting()" in restore, "没有对话时才加开场白（避免每次开窗都重复一遍）"
    # 第 50 步删掉多天后，这里不再恢复"上次生成的菜单"
    assert "data.plan" not in restore


def test_the_greeting_comes_from_the_backend_not_the_frontend():
    js = _js()
    assert "state.recipeSchema.greeting" in js, "开场白要用后端给的"
    # 开场白文案（后端 GREETING）不该出现在前端
    assert "我是菜谱助手" not in js
    assert "我是菜谱助手" not in _html()


def test_the_quick_prompt_card_is_gone():
    """第 50 步：「试试这样说」那张卡删掉了，换成右栏的标签框。

    旧断言（"点了直接发送"）按规矩反转成"这个界面不该再有"。
    """
    js = _js()
    block = _recipe_block()

    assert 'id="recipe-prompts"' not in block, "「试试这样说」应当已从界面删除"
    assert "renderRecipePrompts" not in js


def test_ask_has_a_timeout_and_can_be_aborted():
    """行为验证抓到的两个真缺陷，这里钉住修法：

    1. `/ask` 没有超时：后端挂住时发送按钮**永久**变灰（只有 finally 会恢复它）；
    2. 关窗不打断在途请求：响应回来后会往**新开的**窗口里追加一条
       "没有对应提问"的回复和卡片（串台）。
    """
    js = _js()

    assert "RECIPE_ASK_TIMEOUT_MS" in js, "对话要有自己的超时（不能借用 180 秒那个）"
    assert "new AbortController()" in js
    assert "signal: controller.signal" in js, "fetch 要接上 abort 信号"

    send = re.search(r"async function sendRecipeMessage\(text\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "controller.abort()" in send, "超时要真的 abort"
    assert "state.recipeAskController !== controller" in send, (
        "响应回来要先确认请求还有效，否则会画进别的会话"
    )
    assert "window.clearTimeout(timer)" in send

    # 关窗时掐掉在途请求
    close_dialog = re.search(r"function closeDialog\(\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    assert "abortRecipeAsk()" in close_dialog


def test_saving_aversions_clears_the_previous_error():
    """存上了就把红字清掉，否则"改好了但提示还挂着"看起来像没记住。"""
    js = _js()

    toggle = re.search(r"async function toggleAvoidTerm\(term\)\s*\{([\s\S]*?)\n  \}", js).group(1)
    success_at = toggle.index("renderRecipeTags(await response.json())")
    assert "setRecipeError('')" in toggle[success_at:], "成功路径要清错误"


def test_stated_aversions_also_get_a_removable_button():
    """词表外的忌口也要能看见、能取消（第 64 步）。

    为什么必须有：忌口是**整份替换**（`set_aversions`），而右栏原来只渲染
    `avoid_options` 那 8 个固定词。于是对话里记下的「花生」会永久生效、
    却没有任何按钮可以点掉它 —— "取消勾选即删除"就只覆盖了那 8 个词。
    """
    js = _js()
    render = re.search(
        r"function renderRecipeTags\(preferences\)\s*\{([\s\S]*?)\n  \}", js
    ).group(1)

    # 固定词表照旧出按钮
    assert "avoid_options" in render, "仍要渲染固定词表"
    # 追加：aversions 里不在 options 的那些
    assert "!options.includes(term)" in render, (
        "要把「已记住但不在固定词表里」的词也补成按钮"
    )
    # 补出来的按钮必须标来源，用户才知道它为什么在这儿
    assert "dataset.origin = 'stated'" in render, "词表外的按钮要标出来源"
    # 而且必须走**同一个** toggle，否则点它不会真的删
    assert render.count("toggleAvoidTerm(term)") == 1, (
        "新增按钮要复用同一个 toggle（只写一处，避免两条删除路径漂移）"
    )


def test_the_new_session_button_does_not_sit_under_the_window_close_button():
    """第 137 步（用户截图报的）：「新建」压在右上角的 × 下面，点不到也看不清。

    修法是给头部留出右边距。这条钉的是那 40+px 的空档——
    只改按钮自己的 margin 是不够的，因为 × 是绝对定位、不占流内空间。
    """
    import re as _re

    css = _css()
    match = _re.search(r"\.recipe-chat-head\s*\{([^}]*)\}", css)
    assert match, "找不到 .recipe-chat-head 的样式"
    rule = match.group(1)

    padding = _re.search(r"padding:\s*([^;]+);", rule)
    assert padding, "头部应当显式写 padding"
    values = padding.group(1).split()
    right = values[1] if len(values) >= 2 else values[0]
    assert int(right.rstrip("px")) >= 40, (
        f"头部右边距只有 {right}，会盖住右上角的关闭 ×"
    )


def test_the_shopping_bar_can_be_dismissed_on_its_own():
    """第 137 步：勾选工具条必须能单独关掉（以前只能靠"新建会话"消失）。"""
    block = _recipe_block()
    assert 'id="shopping-clear"' in block, "工具条上要有一个 × 清空已选"
    js = _js()
    assert "function clearRecipePicks" in js
    assert "on(el.shoppingClear, 'click', clearRecipePicks)" in js, "× 要真的接上"
    # 清空时要连卡片上的勾选框一起取消，否则会出现"条没了框还勾着"
    clear = js[js.index("function clearRecipePicks"):]
    clear = clear[: clear.index("\n  }")]
    assert "state.pickedRecipes.clear()" in clear
    assert "recipe-pick" in clear


def test_the_window_never_shows_nutrition_numbers_as_facts():
    """不显示热量/蛋白质这类数字（菜谱库是文本菜谱，不是营养数据库）。

    钉住这个决定：一旦有人往界面或提示词里加"千卡"，这条会红。
    第 51 步删掉旧的多天提示词后，这条规则**搬进了菜谱助手的提示词**，
    所以要断言的是那个提示词，而不是被删掉的 `RECIPE_SYSTEM_PROMPT`。
    """
    for source in (_js(), _html()):
        for word in ("千卡", "kcal", "卡路里", "蛋白质含量"):
            assert word not in source, f"不该出现「{word}」（没有可靠数据源）"

    from agents.agent import build_recipe_prompt

    prompt = build_recipe_prompt()
    assert "不写任何营养数值" in prompt, "提示词里要明确禁止营养数值"


def test_recipe_logic_stays_out_of_the_frontend():
    """忌口判定、菜单解析这些**逻辑**在后端，前端只管画。

    （前端做关键词匹配的话，浏览器里那份"忌口表"和后端那份一定会漂移。）
    """
    js = _js()
    assert "CATEGORY_TERMS" not in js
    assert "find_violations" not in js
    assert "normalize_plan" not in js


# ------------------------------------------------------------------ 行为


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑行为验证")
def test_recipe_flow_behaviour():
    """真跑一遍：开窗 → 说需求 → 右栏画像 → 快捷问题 → 生成菜单 → 重开窗口。"""
    assert VERIFY_SCRIPT.exists(), f"缺少验证脚本：{VERIFY_SCRIPT}"

    completed = subprocess.run(
        [NODE, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, "食谱窗口行为验证未全部通过：\n" + output
    assert "失败 0 项" in output, "验证脚本报告了失败项：\n" + output


def test_recipe_ask_uses_the_streaming_endpoint():
    """食谱问答走 SSE 流式（第 74 步），且三个兜底都在。

    钉住的四件事：
    1. 打到 `/api/recipe/ask/stream`（不是老的 `/ask`）；
    2. 有 `body.getReader` 就走增量，没有就整段 `text()` 再解析（老浏览器不至于坏掉）；
    3. 处理 `reset`（工具步的"边想边说"不能留在回答里）；
    4. 超时按**活动**续期 —— 固定总预算会在正常吐字时把回答腰斩。
    """
    js = _js()

    assert "/recipe/ask/stream" in js, "要打流式接口"
    assert "consumeRecipeStream" in js, "增量消费要独立成函数（便于单独推理）"

    consume = re.search(
        r"async function consumeRecipeStream\(response, options\)\s*\{([\s\S]*?)\n  \}\n",
        js,
    )
    assert consume, "找不到 consumeRecipeStream"
    body = consume.group(1)

    # 第 117 步：解析抽到了 `streamSse`，这里只负责"事件怎么画"。
    # 所以断言跟着搬家 —— 但**意图不变**：优先增量、拿不到流要能整段降级。
    assert "streamSse(response" in body, "consumeRecipeStream 要复用共享解析器"

    parser = re.search(
        r"async function streamSse\(response, options\)\s*\{([\s\S]*?)\n  \}\n",
        js,
    )
    assert parser, "找不到 streamSse"
    shared = parser.group(1)
    assert "getReader" in shared, "优先走增量读取"
    assert "response.text()" in shared, "拿不到流时要能整段降级，而不是直接坏掉"
    assert "event.type === 'reset'" in body, "reset 要把气泡清空"
    assert "meta" in body and "meta.reply" in body, "收尾要用服务端的完整回答覆盖一次"

    # 超时续期：armTimer 必须在消费到数据时被调用
    assert "armTimer" in js
    assert "onActivity: armTimer" in js or "onActivity" in js
    # 续期调用也搬进了共享解析器（每收到一块就调一次 onActivity）
    assert "if (onActivity) onActivity();" in shared, "每收到一块就要续期超时"
