"""食谱窗口的领域逻辑：开场白与免责声明、忌口词的清理与展开、进程内的会话存储。

多天菜单那套计划生成已经删除，对话检索由接口层调用模型完成。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import Any

from core import history
from core import recipe_session_meta
from core.schemas import Message

logger = logging.getLogger(__name__)

#: 单个会话保留的最近消息条数（防呆：会话 id 由前端生成，不清会一直攒）
SESSION_MAX_MESSAGES = 40

#: 输入框的示例句。**由后端给**（前端不硬编码中文），`GET /schema` 会带回去。
SAMPLE_MESSAGE = "我在减脂，想让菜清淡一点"

#: 免责声明。界面上已经按要求不再展示（第 50 步），但字段保留：
#: 别处（比如将来的导出/分享）可能要用。
DISCLAIMER = (
    "菜谱来自本地菜谱库与网络参考，仅供参考，不构成医疗或营养建议；"
    "有过敏史或慢性病请以医生意见为准。"
)

#: 开场白。**刻意不含 markdown 记号**：对话气泡是纯文本渲染（textContent），
#: 写了 `**…**` 会原样显示成星号（实测出现过）。
#: 那句"忌口和过敏请一定说出来"是**有意保留**的——用户说它像人文关怀。
#:
#: 但**不承诺"我会严格避开"**（第 62 步）。原因不是谨慎，是事实：
#: 忌口判定是「菜名 + 食材」的文本匹配加一张手工大类表，没有结构化过敏原、
#: 不覆盖调料/腌料/交叉污染，所以"严格"两个字机制上兑现不了。
#: 界面上的免责声明第 50 步已按用户要求去掉，于是这句话成了**唯一的边界说明**，
#: 更不能写成绝对承诺——真实能力是"尽量避开"，严重过敏必须由用户自己再确认。
GREETING = (
    "你好！我是菜谱助手。想吃点什么？跟我说一句就行。"
    "另外，忌口和过敏请一定说出来——这比什么都重要，我会尽量避开；"
    "如果是严重过敏，请你也再确认一下用料。"
)


# --------------------------------------------------------------- 忌口词的清理

#: 这些词出现说明"没有忌口"，不是忌口本身。
_NO_INFO_VALUES = frozenset(
    {"", "无", "没有", "暂无", "none", "null", "-", "—", "无。", "没有。", "暂无。"}
)
_EXPLICIT_NONE_VALUES = frozenset(
    {"无", "没有", "暂无", "无特殊要求", "没有特殊要求", "暂无特殊要求", "不忌口",
     "没什么", "啥都吃", "都行", "随意"}
)
#: 两者合起来 = "这个词没有信息量"，忌口切词时用它过滤。
_EMPTY_TERMS = _NO_INFO_VALUES | _EXPLICIT_NONE_VALUES

#: 忌口词前后的修饰语：`不吃香菜` → `香菜`，`海鲜过敏` → `海鲜`，`忌口油炸` → `油炸`。
#:
#: ⚠️ 多字选项必须排在单字前面（`不要` 在 `不` 之前）。正则的 `|` 是**从左到右**
#: 取第一个能匹配的，反过来会把"不要香菜"切成"要香菜"——留下一个反向的意思。
#: 这条是第 51 步重写时被新加的测试当场抓到的。
_TERM_PREFIX_RE = re.compile(
    r"^(?:我)?(?:对)?(?:不能|不要|不太|不|别|勿|忌|戒)?(?:吃|碰|用|口)?"
)
_TERM_SUFFIX_RE = re.compile(r"(?:过敏源|过敏原|过敏|忌口)?$")
#: 判断"这个词其实什么都没说"：把这些噪音字去掉，剩下空的就是没有信息量。
#: 必须用这招而不是死记一张表——"暂无特殊要求""都行""不忌口"这种说法是无穷的。
_TERM_NOISE_RE = re.compile(
    r"暂无|没有|没什么|没啥|无|不|都|也|特殊要求|特殊|要求|忌口|过敏源|过敏原|过敏|"
    r"什么|啥|可以|都行|随意|不限|none|\s"
)
#: 忌口词按这些符号切成词。
_TERM_SPLIT_RE = re.compile(r"[、,，;；/|和与及+\s]+")


# ---------------------------------------- 从对话里读出「说出来的忌口」（第 63 步）

#: 触发说法：只有出现这些，才认为用户是在**陈述**"我不能吃 X"。
#: 刻意**不含裸「不」**——"不讨厌香菜""不错"会被误判成忌口。
#: `忌口` 必须排在 `忌` 前面，否则"忌口海鲜"会切成"口海鲜"。
_STATED_AVOID_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 花生过敏 / 海鲜过敏源 / 虾过敏原（前面是"不没无非"时不算，挡掉"不过敏"）
    re.compile(
        r"(?<![不没无非])([\u4e00-\u9fa5、,，;；/|]{1,8})(?:过敏源|过敏原|过敏)"
    ),
    # 不能吃辣 / 不吃香菜 / 忌口海鲜 / 戒酒 / 别放葱 / 不要香菜
    # 「海鲜和芒果都不能吃」「香菜、葱都不吃」：并列 + 否定**在词条后面**。
    # 第 79 步加：这种说法以前整条漏掉（模式要求否定词在前）。
    re.compile(
        r"([\u4e00-\u9fa5、,，;；/|]{1,8}?)(?:都不能吃|都不能碰|都不能喝|都不吃|都忌)"
    ),
    # 「忌(?!口)」很重要：否则正则会退而求其次，把「忌口」拆成「忌」+「口」，
    # 于是「有什么忌口」抽出「口」——实测踩过。
    #
    # 第 90 步（A2）：把「别放」「不要」**移出去**。它们是"这一轮不想要"，
    # 不是"我这辈子都不吃"——见下面的 _TURN_AVOID_PATTERNS。
    re.compile(
        r"(?:不能吃|不可以吃|不要吃|不吃|忌口|忌讳|忌(?!口)|戒)"
        r"([\u4e00-\u9fa5、,，;；/|]{1,8})"
    ),
)

#: 「别放葱」「不要香菜」这类**当轮**诉求（第 90 步 / A2）。
#:
#: 为什么不跟长期忌口混在一起：用户随口一句"不要辣的"，若写进长期库，
#: 下次他说"想吃辣"会被**静默拦掉**，右栏还会多出一个他没点过的词条。
#:
#: 判定边界（用户拍板）：**过敏 / 我忌口 / 我不吃 X** 算长期陈述；
#: **不要 X / 别放 X** 算这一轮的要求。
_TURN_AVOID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:别放|不要|别加|不用放|不加)([\u4e00-\u9fa5、,，;；/|]{1,8})"
    ),
)

#: 词条前面常粘上的虚词，逐个剥掉（"我海鲜" → "海鲜"、"对花生" → "花生"）。
_STATED_LEAD_NOISE = "我你他她它对把和跟与也还很都就这那些"
#: 词条后面常粘上的语气词。
_STATED_TAIL_NOISE = "了吗呢吧啊呀的哦"
#: 中文食材名基本不超过 4 个字；更长多半是把整句吞进来了，宁可丢弃也不乱记。
_STATED_MAX_LEN = 4

#: 这些不是食材，是**人在问忌口**时冒出来的功能词（第 78 步）。
#:
#: 实测踩过的坑：用户在问「我的忌口是什么」，抽取器把「是什么」写进长期忌口，
#: 右栏就冒出一个莫名其妙的词条；「忌口有哪些」会写进「有哪些」。
#: 我第 63 步的"不许瞎记"测试只覆盖了否定句，**完全没想到问句**。
_STATED_STOP_TERMS = frozenset({
    "什么", "啥", "哪些", "哪个", "哪种", "怎么", "为什么", "多少", "几个",
    "时候", "意思", "区别", "东西", "推荐", "菜", "菜谱", "口", "呢", "吗",
    "是", "有", "个", "些", "么", "的", "了", "过敏", "忌口",
})

#: 以这些词**开头**的，多半是问句的尾巴（「什么菜」「哪些菜」），也丢掉。
_STATED_QUESTION_PREFIXES = ("什么", "哪些", "哪个", "怎么", "为什么", "多少", "啥")

#: 并列连接词：用户列举忌口常说「花生和坚果」「香菜、葱」「虾与蟹」（第 79 步）。
#:
#: ⚠️ 已知代价：「和牛」会被切成「牛」。中文食材里以「和」开头的极少，
#: 权衡后接受——**漏掉一整条并列**比误切一个罕见词严重得多。
_STATED_CONJUNCTIONS = re.compile(r"以及|还有|、|,|，|;|；|/|\||和|与|及")

#: 以这些词**结尾**的同样要丢：问句常带着「是/有」这些字，
#: 于是「是什么」「有哪些」不会命中上面那个精确匹配（它们不是「什么」本身）。
_STATED_QUESTION_SUFFIXES = ("什么", "哪些", "哪个", "啥", "呢", "吗")


def _looks_like_food(raw: str) -> bool:
    """这段残余是否"看起来像个食材"——决定要不要回显给用户。

    太短（1 字）或命中功能词黑名单的，属于问句残渣，报出来只会吵；
    2~6 字的残余才值得让用户确认（「巧克力蛋糕」这种我们确实处理不了）。
    """
    text = str(raw or "").strip()
    if not 2 <= len(text) <= 6:
        return False
    if text in _STATED_STOP_TERMS:
        return False
    if text.startswith(_STATED_QUESTION_PREFIXES):
        return False
    if text.endswith(_STATED_QUESTION_SUFFIXES):
        return False
    # 中间夹着疑问词的（「个什么」「那个哪些」）同样是问句残渣
    if any(word in text for word in ("什么", "哪些", "哪个", "怎么", "为什么")):
        return False
    return True


def _strip_stated_noise(raw: str) -> str:
    """剥掉词条两端的虚词；剥完不合法就返回空串（**不猜**）。"""
    term = str(raw or "").strip()
    while term and term[0] in _STATED_LEAD_NOISE:
        term = term[1:]
    while term and term[-1] in _STATED_TAIL_NOISE:
        term = term[:-1]
    if not term or len(term) > _STATED_MAX_LEN:
        return ""
    if term in _EMPTY_TERMS:
        return ""
    # 功能词不是忌口：「我的忌口是什么」不能记下「是什么」（第 78 步）
    if term in _STATED_STOP_TERMS:
        return ""
    if term.startswith(_STATED_QUESTION_PREFIXES):
        return ""
    if term.endswith(_STATED_QUESTION_SUFFIXES):
        return ""
    # 「我不过敏」会被切成"我不"→剥掉"我"后只剩"不"。否定词本身不是忌口，
    # 必须以否定字开头的残留判为无效（食材名不会以"不没无非"开头）。
    if term[0] in "不没无非":
        return ""
    return term


#: 「这一轮不想吃辣」这类**当轮**否定说法（第 77 步）。
#:
#: 为什么只做「辣」：标签词表里**只有它既是标签名、又常被否定**
#: （清淡/酸甜/咸香不会有人专门说"不要"）。别的口味没有对应标签可排，
#: 硬做一个只会误导。
_NEGATIVE_TASTE_PATTERNS: dict[str, re.Pattern[str]] = {
    # 「不要太辣」「不太辣」也算：「别太辣」是常见说法，
    # 而工具只能按标签排除，没有"减辣"这种中间档，只能整档排掉。
    "辣": re.compile(r"不(?:要|想)?(?:太|那么|很)?辣|别放辣|不要放辣|不加辣|不吃辣|去辣"),
}


def extract_negative_taste(message: str) -> list[str]:
    """从这句话里读出**当轮**的否定口味（目前只有「辣」）。

    为什么要服务端抽（第 77 步）：实测把 `exclude_tags` 交给模型，
    它**不传** —— 更糟的是它会在回答里写「已帮你避开了辣」，
    而工具根本没收到这个条件。那正是"嘴上说避开"。
    项目一贯的做法是：**约束压在工具边界，不靠模型自觉**。

    与 `extract_stated_aversions` 的分工：那个是**长期**忌口（会落库），
    这个是**这一轮**的条件（不落库，只影响本次检索）。
    """
    text = str(message or "")
    if not text:
        return []
    return [taste for taste, pattern in _NEGATIVE_TASTE_PATTERNS.items()
            if pattern.search(text)]


def extract_stated_aversions(message: str) -> list[str]:
    """从用户这句话里读出**他自己说出来的**忌口 / 过敏原。

    为什么要这一步（第 63 步）：`aversions` 原来只来自 `list_aversions(storage)`，
    也就是"存过库的才算"。用户在对话里新说一句「我对花生过敏」，如果不落库，
    工具层就过滤不到，只能靠模型自觉 —— 而过敏是高风险场景，不能靠自觉。

    刻意**不用模型**：这是一组可枚举的说法（过敏 / 不吃 / 忌口 / 戒 / 别放…），
    正则能做、能单测、零延迟、零成本；交给模型反而多一个失败点。

    误判的取舍：宁可**多记**一条（多过滤几道菜），也不漏记（漏了是安全风险）。
    所以这里只保证"看起来像忌口就记下"，不追求把自然语言完全读懂。

    返回去重后的词条（保持出现顺序）；取不到就返回空列表。
    """
    return _stated_terms(message)[0]


def extract_stated_aversions_detailed(message: str) -> tuple[list[str], list[str]]:
    """返回（已识别的忌口, **看起来像食材却没识别出来**的残余）。

    为什么要第二项（第 79 步）：`"我对花生和坚果过敏"` 原来整条丢失，
    而库里什么都没记、回复里还照样说"已避开"——**用户无从知道系统没记住**。
    现在把没识别出来的那段交给上层回显，让用户自己确认，
    比加密正则更可靠：正则永远会有覆盖不到的说法。
    """
    return _stated_terms(message)


def extract_turn_exclude(message: str) -> list[str]:
    """读出**这一轮**的排除要求（「别放葱」「不要香菜」）。

    与 `extract_stated_aversions` 的分工（第 90 步 / A2）：
    - 这个：**不落库**，只作为本次请求的 `exclude` 注入；
    - 那个：长期忌口，落库、会出现在右栏。
    """
    return _stated_terms(message, _TURN_AVOID_PATTERNS)[0]


def _stated_terms(
    message: str,
    patterns: tuple[re.Pattern[str], ...] | None = None,
) -> tuple[list[str], list[str]]:
    text = str(message or "")
    if not text:
        return [], []

    found: list[str] = []
    dropped: list[str] = []
    for pattern in (patterns if patterns is not None else _STATED_AVOID_PATTERNS):
        for match in pattern.finditer(text):
            # 先按并列拆开再逐段校验（第 79 步）：
            # 「花生和坚果」是 5 个字，整段会被长度校验丢掉；
            # 拆成「花生」「坚果」两段就都能过。
            for part in _STATED_CONJUNCTIONS.split(match.group(1) or ""):
                term = _strip_stated_noise(part)
                if term:
                    if term not in found:
                        found.append(term)
                elif _looks_like_food(part):
                    leftover = str(part).strip()
                    if leftover and leftover not in dropped:
                        dropped.append(leftover)
    return found, dropped

#: 忌口里的「大类」词 → 具体食材。
#:
#: 为什么必须有这张表：纯子串匹配挡不住最常见的说法。用户说"海鲜过敏"，
#: 切出来的词是「海鲜」，而菜名里写的是"蒜蓉粉丝蒸虾"、"香煎带鱼"——
#: **一次都命中不了**，安全检查形同虚设。所以给家常菜里最常见的大类
#: 配一份**有界**的词表。
#:
#: 刻意只覆盖这几类、且尽量用不容易歧义的双字词（不用"肚""心"这种
#: 会命中"开心""用心"的单字）。
CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "海鲜": (
        "虾", "蟹", "贝", "蛤", "蚝", "生蚝", "扇贝", "花甲", "鱿鱼", "章鱼",
        "墨鱼", "海参", "鲍鱼", "龙虾", "海鱼", "带鱼", "三文鱼", "鳕鱼",
        "金枪鱼", "黄花鱼", "鲈鱼",
        # 淡水鱼也是鱼（第 79 步补）：只列**具体鱼名**，绝不加裸「鱼」——
        # 加了会把「鱼香肉丝」误伤（那菜里一条鱼都没有）。实测原来 14 道鱼菜里
        # 有 8 道逃过了「海鲜」忌口，就是这一块缺的。
        "鲤鱼", "草鱼", "鳜鱼", "胖头鱼", "武昌鱼", "鳊鱼", "黑鱼", "海鳗",
        "鳗鱼", "鲫鱼", "鲢鱼", "鲶鱼", "鳝鱼", "鲳鱼", "稻花鱼", "鱼头", "鱼丸",
    ),
    "海产品": ("虾", "蟹", "贝", "鱿鱼", "海参", "海带", "紫菜"),
    "牛羊肉": ("牛肉", "羊肉", "牛腩", "牛排", "肥牛", "羊排", "牛筋", "牛杂"),
    "内脏": ("猪肝", "鸡肝", "鹅肝", "腰花", "猪腰", "肥肠", "大肠", "猪肚", "鸡胗", "鸡心"),
    "乳制品": ("牛奶", "奶油", "黄油", "芝士", "奶酪", "酸奶", "炼乳", "淡奶油"),
    "乳糖": ("牛奶", "奶油", "黄油", "芝士", "奶酪", "酸奶", "炼乳", "淡奶油"),
    "坚果": ("花生", "核桃", "腰果", "杏仁", "榛子", "开心果", "松子", "芝麻", "瓜子"),
    "麸质": ("面粉", "面条", "面包", "馒头", "饺子", "包子", "意面", "挂面", "馄饨"),
    "蛋": ("鸡蛋", "鸭蛋", "蛋清", "蛋黄", "鹌鹑蛋", "蛋液"),
    "大豆": ("黄豆", "豆腐", "豆浆", "豆皮", "腐竹", "毛豆", "豆干"),
    "辣": ("辣椒", "花椒", "小米椒", "干辣椒", "辣酱", "剁椒", "泡椒"),
}


def clean_avoid_text(text: str) -> list[str]:
    """把"忌口"的自由文本切成词条。

    `海鲜过敏，不吃香菜` → `["海鲜", "香菜"]`；"没有忌口" → `[]`。

    这里**必须有损**，而且宁可多切几个词：检索工具拿它做排除，
    多排一道菜用户能看出来，漏排一道过敏原就是安全事故。
    """
    if not text:
        return []

    terms: list[str] = []
    for chunk in _TERM_SPLIT_RE.split(str(text)):
        item = chunk.strip()
        if not item:
            continue
        item = _TERM_PREFIX_RE.sub("", item)
        item = _TERM_SUFFIX_RE.sub("", item)
        # 词尾可能还留着"不能""不要"这类残渣
        item = re.sub(r"(?:不能|不要|不太|不吃|别)$", "", item).strip()
        if not item or item.lower() in _EMPTY_TERMS:
            continue
        # "暂无特殊要求"这种整句其实什么都没说：噪音字去掉后是空的就丢掉。
        # 注意"无花果"不会因此被误删——去掉"无"还剩"花果"。
        if not _TERM_NOISE_RE.sub("", item.lower()).strip():
            continue
        if item not in terms:
            terms.append(item)
    return terms


#: 忌口词 -> 词表里对应的键（第 79 步）。
#:
#: 右栏的忌口选项写的是「奶」，而词表里的键是「乳制品」——不搭这座桥，
#: `expand_term("奶")` 只会返回 `["奶"]`，于是「芝士」「黄油」「奶酪」全都漏掉。
#: 种子里刚好都写成"牛奶/奶油"（含"奶"字），所以测试一直没暴露。
CATEGORY_ALIASES: dict[str, str] = {
    "奶": "乳制品",
}


def expand_term(term: str) -> list[str]:
    """把一个忌口词展开成"实际拿去匹配"的一组词。

    `海鲜` → `["海鲜", "虾", "蟹", ...]`；表里没有的词原样返回。
    展开是**单向**的：只从大类到具体食材，不做反向推断。
    """
    text = (term or "").strip()
    if not text:
        return []
    words = [text]
    for word in CATEGORY_TERMS.get(CATEGORY_ALIASES.get(text, text), ()):
        if word not in words:
            words.append(word)
    return words


# ------------------------------------------------ 主料（第 52 步：按结构检索）
#
# 用户说"有没有辣的鸡肉"，库里写的是"三黄鸡""鸡腿肉"——直接拿"鸡肉"去
# 关键词检索是**0 条**（实测），只能靠 nearby 兜一堆无关的菜。
#
# 所以主料必须和口味分开：
#   口味 → tags（结构化标签）
#   主料 → 这张表（大类 → 具体写法）
# 20 类，覆盖家常说法里绝大多数；表外的词照样按字面匹，不会漏。

MAIN_INGREDIENTS: dict[str, tuple[str, ...]] = {
    "鸡": ("鸡", "三黄鸡", "童子鸡", "土鸡", "乌鸡", "鸡腿", "鸡胸", "鸡翅",
           "鸡丁", "鸡块", "鸡爪", "鸡丝", "鸡脯"),
    "鸭": ("鸭", "老鸭", "鸭腿", "鸭胸", "板鸭", "烤鸭", "鸭掌"),
    "鹅": ("鹅", "鹅肉", "鹅掌"),
    "猪": ("猪", "猪肉", "五花肉", "里脊", "排骨", "猪蹄", "肉末", "肉丝",
           "肉片", "肉丁", "前腿肉", "梅花肉"),
    "牛": ("牛", "牛肉", "牛腩", "牛里脊", "牛腱", "牛柳", "肥牛", "牛排", "牛尾"),
    "羊": ("羊", "羊肉", "羊排", "羊腿", "羊蝎子", "羊杂"),
    "鱼": ("鱼", "鲈鱼", "草鱼", "鲤鱼", "鲫鱼", "带鱼", "鳜鱼", "鲳鱼",
           "鱼头", "鱼片", "鱼身", "黄鱼", "酸汤鱼"),
    "虾蟹": ("虾", "蟹", "龙虾", "基围虾", "虾仁", "大闸蟹", "青蟹"),
    "贝类": ("贝", "蛤", "蚝", "扇贝", "花甲", "蛏子", "生蚝"),
    "蛋": ("蛋", "鸡蛋", "鸭蛋", "蛋清", "蛋黄", "皮蛋", "咸蛋", "鹌鹑蛋"),
    "豆腐": ("豆腐", "豆干", "豆皮", "腐竹", "油豆腐", "豆腐皮", "豆泡"),
    "内脏": ("肝", "猪肝", "鸡肝", "腰花", "猪腰", "肥肠", "大肠", "猪肚",
             "鸡胗", "肚"),
    "土豆": ("土豆", "马铃薯", "洋芋"),
    "茄子": ("茄子",),
    "白菜": ("白菜", "娃娃菜", "大白菜", "圆白菜"),
    "菌菇": ("蘑菇", "香菇", "木耳", "金针菇", "杏鲍菇", "松茸", "平菇"),
    "面食": ("面", "面条", "手擀面", "挂面", "拉面", "面粉", "馍", "饼"),
    "米饭": ("米饭", "饭", "大米", "糯米", "米"),
    "米粉": ("米粉", "米线", "河粉", "粉丝", "粉条", "螺蛳粉", "粉"),
    "蔬菜": ("青菜", "菠菜", "西兰花", "花菜", "豆角", "青椒", "尖椒",
             "芹菜", "韭菜", "生菜", "娃娃菜"),
}

#: 标签的**包含关系**：用户说"想吃辣的"，微辣的菜也该算数。
#: 图省事让模型同时传"辣"和"微辣"并不可靠——模型会忘。
#: 这里在检索层做包含，模型只传一个词。
TAG_IMPLIES: dict[str, tuple[str, ...]] = {
    "辣": ("辣", "微辣"),
    "微辣": ("微辣",),
}

#: 标签属于哪个维度 —— **跨维度是"与"，同维度是"或"**。
#:
#: 这是第 52 步修的一个真 bug：用户勾了「微辣 + 粤菜 + 下饭」再问「鱼」，
#: 结果里出现了湘菜（只命中微辣）和清蒸鲈鱼（只命中粤菜）——
#: 因为当时把所有标签当成一个集合取交集，命中**任意一个**就算过。
#:
#: 正确语义：
#:   微辣 + 粤菜   → 又要辣、又要是粤菜（跨维度「与」）
#:   辣 + 微辣     → 辣或微辣都行（同维度「或」，见 TAG_IMPLIES）
TAG_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "口味": ("清淡", "微辣", "辣", "酸甜", "咸香"),
    "菜系": (
        "粤菜", "川菜", "鲁菜", "苏菜", "浙菜", "闽菜", "湘菜", "徽菜",
        "北京", "天津", "上海", "重庆", "河北", "山西", "内蒙古",
        "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "青海", "宁夏", "新疆",
        "云南", "贵州", "西藏", "广西", "河南", "湖北", "江西", "海南",
        "香港", "澳门", "台湾",
    ),
    # 品类：这顿是"汤/主食/加餐"这类**饭的结构**
    "品类": ("汤", "主食", "加餐", "早餐", "下饭"),
    # 功效：养胃/减脂这类**身体诉求**——和品类是两回事，必须能同时成立
    # （"养胃的汤" = 品类汤 AND 功效养胃）
    "功效": ("养胃", "减脂", "高蛋白", "低卡", "平价"),
    # 便捷：多久能做好、要不要开火
    "便捷": ("一锅出", "免开火", "电饭煲", "小电锅"),
    # 做法
    "做法": ("蒸", "煮", "炒", "凉拌", "烤", "炖", "煎", "炸", "焖"),
    # 饮食偏好：素与其他维度都应当能组合（"清淡的素菜"）
    "偏好": ("素",),
}

#: 放宽顺序：口味最容易让，菜系最不让（"我就要吃川菜"是很强的意图）。
RELAX_ORDER: tuple[str, ...] = ("口味", "便捷", "品类", "做法", "功效", "偏好", "菜系")


def tag_dimension(tag: str) -> str:
    """这个标签属于哪个维度（表里没有的归「其它」）。"""
    text = str(tag or "").strip()
    for dimension, tags in TAG_DIMENSIONS.items():
        if text in tags:
            return dimension
    return "其它"


def group_tags_by_dimension(tags: Sequence[str]) -> dict[str, list[str]]:
    """把勾选的标签按维度分组：`["微辣","粤菜"]` → `{"口味":["微辣"],"菜系":["粤菜"]}`。"""
    grouped: dict[str, list[str]] = {}
    for tag in tags or []:
        text = str(tag).strip()
        if not text:
            continue
        grouped.setdefault(tag_dimension(text), []).append(text)
    return grouped


# ------------------------------------------------ 右栏勾选 → 检索约束（第 55 步）
#
# 勾选是**已知状态**：用户点了什么，后端完全知道。第 50e 那版把它塞进提示词，
# 让模型"记得"转成工具参数——模型忘了传就静默失效，而且没法端到端断言。
# 现在拆成两半：
#   - 约束部分（口味/菜系/品类/功效/做法/偏好 + 时间）：**服务端强制注入**；
#   - 口径部分（自己做/外卖/都行）：只作为提示词里的推荐倾向。
#
# 这样模型仍然可以**追加**条件（从话里听出"想吃鱼"），但不能**丢掉**用户点明的。

#: 「时间」组 → 上限（分钟）
WANTED_MAX_MINUTES: dict[str, int] = {"15 分钟内": 15, "30 分钟内": 30}
#: 「30 分钟以上」→ 下限（分钟）
WANTED_MIN_MINUTES: dict[str, int] = {"30 分钟以上": 30}

#: 这些不是检索条件，而是"怎么吃"的口径说明
WANTED_MANNER: dict[str, str] = {
    "外卖": "这次倾向点外卖：优先推做法简单、适合外带、凉了也不难吃的菜",
    "自己做": "这次想做：正常推荐，可以适当推有点动手乐趣的菜",
    "都行": "自己做或外卖都行，不用特别偏向",
}


def wanted_constraints(wanted: Sequence[str]) -> dict[str, object]:
    """右栏勾选 → 可以直接塞给检索工具的参数。

    返回 `{"tags": [...], "min_minutes": n|None, "max_minutes": n|None}`。
    「自己做/外卖/都行」不进约束（它们不是标签，也不该过滤结果）。
    """
    tags: list[str] = []
    min_minutes: int | None = None
    max_minutes: int | None = None
    for label in wanted or []:
        text = str(label).strip()
        if not text:
            continue
        if text in WANTED_MAX_MINUTES:
            max_minutes = WANTED_MAX_MINUTES[text]
            continue
        if text in WANTED_MIN_MINUTES:
            min_minutes = WANTED_MIN_MINUTES[text]
            continue
        if text in WANTED_MANNER:
            continue          # 只是推荐口径，不是过滤条件
        if tag_dimension(text) != "其它":
            tags.append(text)
    return {"tags": tags, "min_minutes": min_minutes, "max_minutes": max_minutes}



def expand_ingredient(term: str) -> list[str]:
    """主料词展开成"菜谱里可能怎么写"的一组词。

    `鸡肉` / `鸡` → `["鸡", "三黄鸡", "鸡腿", ...]`；表里没有的原样返回。

    先做一次**别名归一**：用户和模型会说"鸡肉""鸡腿肉"，而表里的大类叫"鸡"。
    所以"鸡肉"要能被认成"鸡"这一类，而不是拿去字面匹配。
    """
    text = (term or "").strip()
    if not text:
        return []

    words: list[str] = [text]
    # 直接命中大类
    for alias in MAIN_INGREDIENTS.get(text, ()):
        if alias not in words:
            words.append(alias)
    # "鸡肉""牛肉""猪肉"这类：去掉"肉"字后再试一次
    stripped = text[:-1] if text.endswith("肉") and len(text) > 1 else ""
    if stripped:
        for alias in MAIN_INGREDIENTS.get(stripped, ()):
            if alias not in words:
                words.append(alias)
        if stripped not in words:
            words.append(stripped)
    # 反过来：用户说"鸡"，表里的别名里也可能有"鸡肉"这种写法
    for category, aliases in MAIN_INGREDIENTS.items():
        if text in aliases and category not in words:
            words.append(category)
    return words


def expand_tags(tags: Sequence[str]) -> list[str]:
    """标签展开（含包含关系）：`辣` → `["辣", "微辣"]`。"""
    out: list[str] = []
    for tag in tags or []:
        text = str(tag).strip()
        if not text:
            continue
        for word in TAG_IMPLIES.get(text, (text,)):
            if word not in out:
                out.append(word)
        if text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------- 会话


#: 否定词：出现在标签前面时，说明**不是**要它（"不辣""少放辣""不想吃素"）
_NEGATION_BEFORE = ("不", "别", "少", "没", "勿", "无", "免", "忌", "去掉", "不要", "不用")

#: 抽取"这句话里提到了哪些标签"时看前面几个字
_NEGATION_WINDOW = 3


def mentioned_tags(text: str) -> list[str]:
    """这句话里**明确提到**的标签（用于判断"这一轮到底要什么"）。

    只认词表里的词，且**必须排除否定**：`不辣` 里虽然有"辣"，但意思正好相反。
    不做这一步，"少放辣椒"会被当成"想吃辣"，是最容易写错的地方。

    用途（第 56 步）：判断某个标签是"这一轮说的"还是"上一轮遗留下来的"。
    """
    from core.recipe_seed import TAG_VOCABULARY

    content = str(text or "")
    if not content:
        return []

    found: list[str] = []
    for tag in TAG_VOCABULARY:
        start = 0
        while True:
            index = content.find(tag, start)
            if index < 0:
                break
            before = content[max(0, index - _NEGATION_WINDOW):index]
            if not any(word in before for word in _NEGATION_BEFORE):
                if tag not in found:
                    found.append(tag)
                break
            start = index + 1
    return found


@dataclass
class RecipeSession:
    """一个食谱会话。

    第 51 步起只存对话消息（画像与菜单记录都随多天功能删掉了）。
    将来加"查看历史 / 新建会话"时，把 messages 读出来渲染即可。

    `recent_tags`（第 56 步）：最近用过的标签。用来实现用户的规则——
    "第三轮取消勾选后，不该再参照前两轮的关键词"。没有这份记录，
    模型会自己从对话历史里把旧条件补回来，而服务端无从判断。

    `aversions_snapshot`（第 57 步）：上一轮注入给模型的忌口快照。
    忌口被取消时，只改提示词是不够的——模型会拿自己上一轮说过的
    "你对奶忌口"当事实继续拒绝（实测截图里就是这样）。所以变化时要
    在对话里插一条**比旧话更新**的说明。
    """

    session_id: str
    messages: list[Message] = field(default_factory=list)
    recent_tags: list[str] = field(default_factory=list)
    aversions_snapshot: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RecipeSessionStore:
    """按会话 id 记住食谱会话。

    第 91 步（A3）起：传入 `storage` 时，消息**以库为准**（`core.history`），
    重启后能恢复；不传时退化成原来的进程内 dict（测试与降级用）。

    两个上限不是一回事：
    - `max_messages`（40）= 喂给模型的上下文（由调用方切片）；
    - `history.MAX_ROWS_PER_SESSION`（200）= 界面能翻到的历史。
    有 storage 时内存里保留到 200 条供 `/session` 返回，模型那边自己切 40。

    用 threading.Lock 而不是 asyncio.Lock：这里面的操作是同步的、
    没有 await，用线程锁更省心，也让测试不必先起事件循环。
    """

    def __init__(
        self,
        *,
        max_sessions: int = 8,
        max_messages: int = SESSION_MAX_MESSAGES,
        storage: object | None = None,
    ):
        self._sessions: dict[str, RecipeSession] = {}
        self._max_sessions = max(1, int(max_sessions))
        self._max_messages = max(2, int(max_messages))
        self._storage = storage
        # 有库时内存里多留一些（界面要翻历史）；没有库时维持原来的 40
        self._cap = (
            history.MAX_ROWS_PER_SESSION if storage is not None else self._max_messages
        )
        self._lock = threading.Lock()

    def ensure(self, session_id: str) -> RecipeSession:
        """取会话，没有就建一个（并把最旧的挤出去）。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = RecipeSession(session_id=session_id)
                if self._storage is not None:
                    # 首次接触就从库把历史捞回来（重启后的第一次请求走这里）
                    session.messages = history.load_messages(
                        self._storage,
                        history.SCOPE_RECIPE,
                        session_id,
                        self._cap,
                    )
                    # 第 102 步：**小状态也要捞回来**。只恢复消息不恢复 recent_tags，
                    # 重启后"上一轮用过的标签不该再参照"这条约束就失效了 ——
                    # 数据回来了、规则没回来，是最难查的一种不一致。
                    tags, snapshot = recipe_session_meta.load_meta(
                        self._storage, session_id
                    )
                    session.recent_tags = tags
                    session.aversions_snapshot = snapshot
                self._sessions[session_id] = session
                while len(self._sessions) > self._max_sessions:
                    oldest = next(iter(self._sessions))
                    if oldest == session_id:      # 别把自己挤掉
                        break
                    self._sessions.pop(oldest, None)
            session.updated_at = time.time()
            return session

    def get(self, session_id: str) -> RecipeSession | None:
        """取会话；**内存里没有但有 storage 时，从库懒加载一次**（第 92 步）。

        为什么要这一步：`GET /session`（前端打开窗口时恢复对话）走的正是 `get()`。
        如果它只看内存，重启后第一次打开窗口就永远是空的 —— 存了也白存。
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            return session
        if self._storage is None:
            return None
        if history.count_messages(self._storage, history.SCOPE_RECIPE, session_id) == 0:
            return None          # 库里也没有：不凭空造一个空会话
        return self.ensure(session_id)

    def append(self, session_id: str, *messages: Message) -> RecipeSession:
        session = self.ensure(session_id)
        with self._lock:
            session.messages.extend(messages)
            if len(session.messages) > self._cap:
                overflow = len(session.messages) - self._cap
                del session.messages[:overflow]
            session.updated_at = time.time()
        if self._storage is not None:
            history.append_messages(
                self._storage, history.SCOPE_RECIPE, session_id, messages
            )
        return session

    def save_meta(self, session: RecipeSession) -> None:
        """把会话的小状态写回库（`recent_tags` / 忌口快照）。

        路由改完这两个字段就调它 —— 不放在 `append()` 里，是因为
        `append()` 发生在"这一轮结束"时，而这两个字段是在**轮次中途**改的。
        """
        if self._storage is None:
            return
        recipe_session_meta.save_meta(
            self._storage,
            session.session_id,
            list(session.recent_tags),
            list(session.aversions_snapshot),
        )

    def reset(self, session_id: str) -> bool:
        with self._lock:
            existed = self._sessions.pop(session_id, None) is not None
        if self._storage is not None:
            # 库里也要删：否则"新建会话"之后历史又冒出来了
            history.clear_messages(self._storage, history.SCOPE_RECIPE, session_id)
            recipe_session_meta.clear_meta(self._storage, session_id)
        return existed

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def session_payload(session: RecipeSession | None, session_id: str) -> dict[str, Any]:
    """`GET /session` 的返回体：只带消息（前端用它恢复对话）。

    `recipes`（第 137 步）：每条回答当时给出的菜 id，前端据此把卡片一起恢复。
    不返回它的话，关掉窗口再打开就只剩文字、卡片全没了（用户实测报的）。
    """
    messages = session.messages if session is not None else []
    return {
        "session_id": session_id,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "recipes": list(message.recipes or []),
            }
            for message in messages
        ],
    }
