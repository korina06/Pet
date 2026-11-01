"""场景与功效词表：把「下酒」「发汗」这类库里没有的说法映射到具体的菜名与食材。

只负责扩召回，后续的忌口、口味、时长等硬约束照旧生效。
"""

from __future__ import annotations

#: 场景词 → 扩展词。键是**用户可能怎么说**，值是**库里真有的词**。
SCENE_TERMS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("下酒", "配酒", "佐酒", "喝酒"): (
        "盐水鸭", "夫妻肺片", "白切鸡", "卤", "豆腐干", "花生米",
    ),
    ("发汗", "出汗", "驱寒", "驱风"): (
        "酸辣", "胡辣汤", "螺蛳粉", "酸汤", "辣",
    ),
    ("解腻", "有点腻", "太油", "去油", "腻"): (
        # 具体菜名优先：泛词"酸甜"在库里更多指向甜品（菠萝包/蛋挞），
        # 重排会把甜点顶到前面 —— 实测踩过。
        "西湖醋鱼", "延吉冷面", "姜撞奶", "醋",
    ),
    ("补身子", "补身体", "补一下", "调理", "养身体", "坐月子"): (
        "老火靓汤", "排骨", "汽锅鸡", "养胃", "猪骨",
    ),
    ("牙口不好", "咬不动", "软的", "软一点", "软烂"): (
        "清蒸", "豆腐", "汤圆", "蒸", "奶",
    ),
    ("快手", "赶时间", "来不及", "办公室", "十分钟", "快速"): (
        "煎饼", "面", "粉", "早餐",
    ),
    ("暖身子", "暖胃", "暖暖", "热乎", "暖"): (
        # 同样：泛词"汤"会把 24 道都拉进来，先给具体的热饮/汤品
        "蒙古锅茶", "酥油茶", "老火靓汤", "排骨藕汤", "姜撞奶",
    ),
}

#: 一条扩展最多带几个词（太多会把候选池稀释）。
MAX_SCENE_TERMS = 6


def expand_scene_terms(text: str) -> list[str]:
    """按**最长命中**的键返回扩展词；没命中返回空列表。

    为什么要"最长命中"而不是"全部命中"：用户说「办公室快手午饭」同时含
    「办公室」和「快手」，两个键指向同一组词，没必要重复；而「牙口不好想吃软的」
    里「牙口不好」比「软的」更具体，应当优先 —— 取最长的那次命中即可。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return []

    best_length = 0
    best_terms: tuple[str, ...] = ()
    for phrases, terms in SCENE_TERMS.items():
        for phrase in phrases:
            if phrase in cleaned and len(phrase) > best_length:
                best_length = len(phrase)
                best_terms = terms

    return list(best_terms[:MAX_SCENE_TERMS])


def describe_scene(text: str) -> str:
    """命中的场景词（用于日志/结果说明）。没命中返回空串。"""
    cleaned = str(text or "").strip()
    best = ""
    for phrases in SCENE_TERMS:
        for phrase in phrases:
            if phrase in cleaned and len(phrase) > len(best):
                best = phrase
    return best
