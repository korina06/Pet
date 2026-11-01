"""`core/recipes.py` 的三块职责：界面文案、忌口词的清理与展开、进程内会话。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipes import (  # noqa: E402
    CATEGORY_TERMS,
    DISCLAIMER,
    GREETING,
    extract_negative_taste,
    extract_turn_exclude,
    extract_stated_aversions,
    extract_stated_aversions_detailed,
    SESSION_MAX_MESSAGES,
    SAMPLE_MESSAGE,
    RecipeSessionStore,
    clean_avoid_text,
    expand_term,
    mentioned_tags,
    session_payload,
)
from core.schemas import Message  # noqa: E402


# ------------------------------------------------------------ 界面文案


def test_greeting_has_no_markdown_and_keeps_the_allergy_reminder():
    """开场白要**纯文本**（气泡是 textContent，星号会露出来），
    但"忌口和过敏请一定说出来"必须留着——用户说它像人文关怀。"""
    assert "**" not in GREETING
    assert "#" not in GREETING
    assert "忌口" in GREETING and "过敏" in GREETING


def test_greeting_does_not_promise_strict_avoidance():
    """开场白**不许**承诺"严格避开"（第 62 步）。

    免责声明在第 50 步已按用户要求从界面去掉，于是开场白成了**唯一的边界说明**。
    而忌口判定是「菜名 + 食材」的文本匹配加一张手工大类表：没有结构化过敏原、
    不覆盖调料/腌料/交叉污染——"严格"这两个字机制上兑现不了。
    所以只能承诺"尽量"，并且必须把最终确认的责任交回用户。
    """
    assert "严格" not in GREETING, "不能承诺「严格避开」——机制兑现不了"
    assert "尽量" in GREETING, "应如实说「尽量避开」"
    assert "再确认" in GREETING, "必须把最终确认的责任交回用户"


def test_sample_message_is_a_plain_sentence():
    assert SAMPLE_MESSAGE and "**" not in SAMPLE_MESSAGE


def test_disclaimer_is_still_returned_even_though_the_ui_hides_it():
    """界面上不再展示（第 50 步），但字段还在——别处可能要用。"""
    assert "医生" in DISCLAIMER


# ------------------------------------------------------------ 忌口词清理


def test_clean_avoid_text_strips_particles_from_natural_sentences():
    """`海鲜过敏，不吃香菜` → `["海鲜", "香菜"]`（修饰语要去掉）。"""
    assert clean_avoid_text("海鲜过敏，不吃香菜") == ["海鲜", "香菜"]
    assert clean_avoid_text("我对花生过敏") == ["花生"]
    assert clean_avoid_text("忌口油炸") == ["油炸"]


def test_no_allergy_phrases_do_not_become_terms():
    """说"没有忌口"不等于忌口一个叫"没有"的东西。"""
    for text in ("没有", "无", "暂无特殊要求", "不忌口", "啥都吃", "都行", ""):
        assert clean_avoid_text(text) == [], text


def test_clean_avoid_text_dedupes_and_splits_on_punctuation():
    assert clean_avoid_text("香菜、香菜，不要香菜; 花生") == ["香菜", "花生"]


def test_clean_avoid_text_keeps_a_real_ingredient_named_like_a_negation():
    """`无花果` 不能被当成"无"删掉——去掉噪音字后还剩"花果"。"""
    assert clean_avoid_text("无花果") == ["无花果"]


# ------------------------------------------- 这一句提到了哪些标签（第 56 步）


def test_mentioned_tags_picks_up_what_the_sentence_asks_for():
    assert mentioned_tags("来个清淡的鱼") == ["清淡"]
    assert mentioned_tags("想吃川菜") == ["川菜"]
    assert mentioned_tags("来碗汤") == ["汤"]


def test_mentioned_tags_ignores_negation():
    """`不辣` / `少放辣椒` 里都有"辣"字，但意思正好相反。

    不排除否定的话，"少放辣椒"会被当成"想吃辣"——这是这类抽取最容易写错的地方。
    """
    assert mentioned_tags("不辣的") == []
    assert mentioned_tags("少放辣椒") == []
    assert mentioned_tags("不要辣") == []
    assert mentioned_tags("不想吃素") == []


def test_mentioned_tags_sees_through_a_longer_sentence():
    tags = mentioned_tags("别太辣，清淡一点，最好是个汤")
    assert "清淡" in tags and "汤" in tags
    assert "辣" not in tags, tags


# ------------------------------------------------------------ 大类展开


def test_category_expansion_catches_concrete_ingredients():
    """`海鲜` 要能连带排除虾蟹贝——菜名里写的是"蒜蓉粉丝蒸虾"。"""
    words = expand_term("海鲜")
    assert "海鲜" in words
    assert {"虾", "蟹", "贝"} <= set(words)


def test_expansion_is_one_way_and_unknown_words_pass_through():
    assert expand_term("香菜") == ["香菜"]
    assert expand_term("") == []
    assert "海鲜" not in expand_term("虾")


def test_every_category_lists_only_short_concrete_words():
    """大类词表是**有界**的：只放具体食材，不放"开心""用心"这种会误伤的短字。"""
    for category, words in CATEGORY_TERMS.items():
        assert words, f"{category} 是空表"
        for word in words:
            assert len(word) >= 1
        # 单字只允许明显是食材的（虾/蟹/贝…），防止"肚""心"这类误命中
        assert all(len(word) >= 2 or word in "虾蟹贝蛤蚝蛋辣" for word in words), category


# ------------------------------------------------------------ 会话


def test_store_keeps_messages_only():
    """第 51 步起会话里只有消息（画像与菜单记录都随多天功能删了）。"""
    store = RecipeSessionStore()
    store.append("s1", Message(role="user", content="吃什么"))
    store.append("s1", Message(role="assistant", content="清蒸鲈鱼"))

    session = store.get("s1")
    assert session is not None
    assert [m.content for m in session.messages] == ["吃什么", "清蒸鲈鱼"]
    assert not hasattr(session, "profile")
    assert not hasattr(session, "plans")


def test_store_evicts_the_oldest_session_and_trims_messages():
    store = RecipeSessionStore(max_sessions=2, max_messages=3)
    store.append("a", Message(role="user", content="1"))
    store.append("b", Message(role="user", content="2"))
    store.append("c", Message(role="user", content="3"))
    assert store.get("a") is None, "最旧的会话要被挤出去"
    assert store.get("c") is not None

    for index in range(5):
        store.append("c", Message(role="user", content=f"m{index}"))
    assert len(store.get("c").messages) == 3, "消息数要按上限裁剪"


def test_reset_only_drops_that_session():
    store = RecipeSessionStore()
    store.append("a", Message(role="user", content="x"))
    store.append("b", Message(role="user", content="y"))
    assert store.reset("a") is True
    assert store.get("a") is None
    assert store.get("b") is not None
    assert store.reset("a") is False


def test_session_payload_is_exactly_what_the_frontend_needs():
    """恢复对话要 role + content + recipes；不要把 tool 消息之类的东西漏给前端。

    `recipes` 是第 137 步补的：每条回答带着当时给出的菜 id，
    否则关窗重开后只剩文字、卡片全没了（用户实测报的问题）。
    """
    store = RecipeSessionStore()
    store.append(
        "s1",
        Message(role="user", content="吃什么"),
        Message(role="assistant", content="清蒸鲈鱼", recipes=[3]),
    )
    payload = session_payload(store.get("s1"), "s1")
    assert payload["session_id"] == "s1"
    assert payload["messages"] == [
        {"role": "user", "content": "吃什么", "recipes": []},
        {"role": "assistant", "content": "清蒸鲈鱼", "recipes": [3]},
    ]


def test_session_payload_for_an_unknown_session_is_empty_not_none():
    payload = session_payload(None, "没聊过")
    assert payload == {"session_id": "没聊过", "messages": []}


def test_message_limit_is_sane():
    assert SESSION_MAX_MESSAGES >= 10


# ------------------------------------------- 从对话里读出忌口（第 63 步）


def test_extract_stated_aversions_reads_allergy_phrases():
    assert extract_stated_aversions("我对花生过敏") == ["花生"]
    assert extract_stated_aversions("海鲜过敏源") == ["海鲜"]
    assert extract_stated_aversions("不能吃辣") == ["辣"]


def test_extract_stated_aversions_reads_avoid_phrases():
    """长期忌口只认「陈述式」：「不吃/忌口/过敏/戒」。

    `别放葱` 属于**这一轮**的要求（第 90 步 / A2），不在这里 —— 见下面那条分工测试。
    """
    assert extract_stated_aversions("不吃香菜") == ["香菜"]
    assert extract_stated_aversions("忌口内脏") == ["内脏"]
    assert extract_stated_aversions("我对花生过敏") == ["花生"]
    assert extract_stated_aversions("别放葱") == [], "别放是当轮要求，不该进长期"


def test_extract_stated_aversions_strips_pronouns_and_particles():
    """「我对花生过敏」不能把「我对」粘进去。"""
    assert extract_stated_aversions("我对芒果过敏啊") == ["芒果"]


def test_extract_stated_aversions_ignores_non_aversions():
    """**不能瞎记**：这些都不含"我在说我不能吃 X"，必须返回空列表。

    最后一条踩过坑：`我不过敏` 会被切成"我不"，剥掉"我"后只剩"不"——
    所以实现里专门挡了以否定字开头的残留。
    """
    for text in (
        "我不过敏",
        "不讨厌香菜",
        "今晚想吃鱼",
        "有没有清淡的菜",
        "",
        None,
    ):
        assert extract_stated_aversions(text) == [], f"{text!r} 不该抽出忌口"


def test_extract_stated_aversions_over_records_rather_than_under_records():
    """已知取舍：宁可多记一条（多过滤几道菜），也不漏记（漏了是安全风险）。"""
    assert extract_stated_aversions("我不吃太多油") == ["太多油"]


def test_a_list_with_a_trailing_conjunction_is_fully_recognized():
    """并列说法（…，还有牛羊肉）**两个都要认**（第 79 步修好了这条局限）。

    以前只认第一个词，注释里写的是"刻意不把『还有』当触发词"；
    现在的做法是**触发词照旧、但把捕获到的整段按连接词拆开**，
    所以「还有别的菜吗」这类问句仍然不会被误记（它压根没有触发词）。
    """
    assert extract_stated_aversions("忌口内脏，还有牛羊肉") == ["内脏", "牛羊肉"]


def test_extract_stated_aversions_dedupes_and_keeps_order():
    got = extract_stated_aversions("不吃香菜，也不吃香菜")
    assert got == ["香菜"], f"应当去重，实际 {got}"

# ------------------------------- 当轮否定口味的抽取（第 77 步）


def test_extract_negative_taste_reads_spicy_refusals():
    assert extract_negative_taste("川菜里不辣的有吗") == ["辣"]
    assert extract_negative_taste("想吃鸡，但是不要辣的") == ["辣"]
    assert extract_negative_taste("别放辣") == ["辣"]
    assert extract_negative_taste("不要太辣") == ["辣"]


def test_extract_negative_taste_ignores_positive_requests():
    """「想吃辣的」不是否定 —— 别把正向口味当成排除条件。"""
    for text in ("今天想吃辣的", "川菜", "清淡点", ""):
        assert extract_negative_taste(text) == [], f"{text!r} 不该抽出否定口味"


def test_extract_stated_aversions_ignores_questions_about_aversions():
    """问「忌口是什么」不能把「是什么」记成忌口（第 78 步）。

    这是**真事**：用户问了一句「我的忌口是什么」，右栏里就多出一个「是什么」，
    而且它会被当成忌口注入检索。第 63 步的"不许瞎记"测试只覆盖了否定句
    （不过敏/不讨厌/想吃鱼），**完全没想到问句**。
    """
    for text in (
        "忌口是什么",
        "我的忌口是什么",
        "忌口有哪些",
        "有什么忌口",
        "什么菜不能吃",
        "我有哪些忌口",
        "不吃啥好呢",
        "忌口是什么意思",
    ):
        assert extract_stated_aversions(text) == [], f"{text!r} 不该抽出忌口"


def test_the_trigger_word_is_not_split_into_a_bare_character():
    """「忌」不能把「忌口」拆开 —— 否则「有什么忌口」会抽出「口」。"""
    assert extract_stated_aversions("有什么忌口") == []


# ------------------------------- 并列说法要拆开再记（第 79 步）


def test_compound_aversions_are_split_before_the_length_check():
    """「我对花生和坚果过敏」原来**整条丢失**（5 字 > 长度上限）。

    这是最自然的说法之一，而它漏掉时用户完全看不出来：库里什么都没记，
    回复里却照样说"已避开"。先拆再校验，两段都能过。
    """
    assert extract_stated_aversions("我对花生和坚果过敏") == ["花生", "坚果"]
    assert extract_stated_aversions("我对花生、坚果过敏") == ["花生", "坚果"]
    assert extract_stated_aversions("我对花生与坚果过敏") == ["花生", "坚果"]
    assert extract_stated_aversions("我对花生、坚果、芒果过敏") == ["花生", "坚果", "芒果"]


def test_compound_with_a_verb_after_it_is_also_split():
    """「海鲜和芒果都不能吃」这种"并列 + 否定在后"的说法以前整条漏。"""
    assert extract_stated_aversions("海鲜和芒果都不能吃") == ["海鲜", "芒果"]


def test_compound_does_not_become_one_fake_term():
    """「我不吃香菜和葱」不能存成一个不存在的词条「香菜和葱」。"""
    assert extract_stated_aversions("我不吃香菜和葱") == ["香菜", "葱"]


def test_partial_recognition_is_reported_to_the_caller():
    """认出来一部分时，**没认出来的那段必须交出去**（第 79 步）。

    用户说「花生和巧克力蛋糕」，我们只认识花生。如果什么都不说，
    他会以为两样都生效了 —— 比漏记更危险。
    """
    terms, unrecognized = extract_stated_aversions_detailed(
        "我对花生和巧克力蛋糕过敏"
    )

    assert terms == ["花生"]
    assert unrecognized == ["巧克力蛋糕"], (
        f"没识别出来的那段要回显，实际 {unrecognized}"
    )


def test_question_forms_are_neither_recorded_nor_reported():
    """问句既不该落库，也不该被当成"没识别出来的食材"回显（第 78 步的延伸）。"""
    for text in ("忌口是什么", "有什么忌口", "我对那个什么过敏"):
        terms, unrecognized = extract_stated_aversions_detailed(text)
        assert terms == [], f"{text!r} 不该抽出忌口"
        assert unrecognized == [], f"{text!r} 不该回显未识别"


# ------------------- 长期忌口 vs 这一轮的要求（第 90 步 / A2）


def test_long_term_and_this_turn_are_separated():
    """边界（用户拍板）：过敏/我忌口/我不吃 X 算长期；不要 X/别放 X 算这一轮。"""
    cases = [
        ("我对花生过敏", ["花生"], []),
        ("我不吃香菜", ["香菜"], []),
        ("忌口内脏", ["内脏"], []),
        ("不能吃辣", ["辣"], []),
        ("不要辣的", [], ["辣"]),
        ("别放葱", [], ["葱"]),
        ("不要香菜和葱", [], ["香菜", "葱"]),
    ]
    for text, long_term, turn in cases:
        assert extract_stated_aversions(text) == long_term, f"{text!r} 的长期判定错了"
        assert extract_turn_exclude(text) == turn, f"{text!r} 的当轮判定错了"


def test_a_one_off_request_does_not_become_a_permanent_aversion():
    """「不要辣的」不该写进长期库 —— 否则下次说"想吃辣"会被**静默拦掉**。
    """
    assert extract_stated_aversions("不要辣的") == []
    assert extract_stated_aversions("别放葱") == []
