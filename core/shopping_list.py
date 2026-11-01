"""买菜清单：把几道菜的食材按名字合并，数量原样保留并标注来源。

不做数量加法，也不猜同义词——清单要能逐条核对。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

#: 数量段的分隔符：空格（半角/全角）与制表符
_SPLIT = re.compile(r"[\s\u3000]+")

#: 括号里的补充说明（"（约 750g）"、"（增色）"）：拆名字时先摘掉
_PARENS = re.compile(r"[（(][^）)]*[）)]")

#: 括号里**就是数量**的写法：`鸡蛋（2个）`、`葱（两根）`。
#: 只在"括号外没找到数量"时采用 —— `三黄鸡半只（约 750g）` 的 750g 是说明，不该顶掉"半只"。
_PAREN_AMOUNT = re.compile(r"^[\d半一两二三四五六七八九十百几]")

#: 中文数量词开头：半/一/两/三…/几 + 常见量词
_CN_AMOUNT = re.compile(
    r"^(?P<name>.*?)(?P<amount>[半一两二三四五六七八九十百几]+"
    r"[只根段块条个把粒颗朵碗盘盆滴勺片杯斤两克份瓣头束]"
    r"|半[茶汤大]匙|几[滴勺片根段块个把粒])$"
)

#: 数字开头的数量（"1 大块" / "500g" / "2 勺"）
_NUM_AMOUNT = re.compile(r"^(?P<name>.*?)[\s]*(?P<amount>\d.*)$")

#: 只表示"要一点"的词：单独出现时整条都是名字，不当数量
_VAGUE = {"适量", "少许", "若干", "随意"}


def normalize_name(text: str) -> str:
    """名字归一化：全角转半角、去多余空白、英文小写。"""
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return _SPLIT.sub("", folded).strip().lower()


def split_ingredient(text: str) -> tuple[str, str]:
    """把一条食材拆成 ``(名字, 数量)``；拆不出来时数量为空串。

    顺序很重要：**先摘括号**再拆数字，否则
    `'三黄鸡半只（约 750g）'` 会被括号里的 750 带偏，名字变成 ``'三黄鸡半只（约 '``。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""

    # 1) 括号里如果是纯说明，就整段摘掉（`（约 750g）` 里的 750 不是要买的数量）
    without_parens = _PARENS.sub("", raw).strip()
    if not without_parens:
        without_parens = raw

    # 2) "适量/少许"这种：只有一个词，或者结尾就是这个
    if without_parens in _VAGUE:
        return without_parens, ""
    if without_parens.endswith(("适量", "少许")) and len(without_parens) > 2:
        amount = "适量" if without_parens.endswith("适量") else "少许"
        return without_parens[: -len(amount)].strip(), amount

    # 3) 按空白拆（`姜 1 大块` -> `姜` / `1 大块`）
    parts = _SPLIT.split(without_parens, maxsplit=1)
    if len(parts) == 2 and parts[1] and re.match(r"[\d半一两二三四五六七八九十百]", parts[1]):
        return parts[0].strip(), parts[1].strip()

    # 4) 中文数量词收尾（`三黄鸡半只` -> `三黄鸡` / `半只`）
    matched = _CN_AMOUNT.match(without_parens)
    if matched:
        return matched.group("name").strip(), matched.group("amount").strip()

    # 5) 数字收尾（`猪梅花肉500g`）
    matched = _NUM_AMOUNT.match(without_parens)
    if matched and matched.group("name").strip():
        return matched.group("name").strip(), matched.group("amount").strip()

    # 6) 括号里就是数量（`鸡蛋（2个）`）：括号外没数量时才采用，且括号内容要**以数量开头**。
    #    这样 `三黄鸡半只（约 750g）` 不会被"约 750g"顶掉（它已经在上一步拿到"半只"），
    #    而 `红曲粉半茶匙（增色）` 也不会把"增色"当成数量。
    for inner in re.findall(r"[（(]([^）)]*)[）)]", raw):
        candidate = inner.strip()
        if candidate and _PAREN_AMOUNT.match(candidate):
            return without_parens, candidate

    return without_parens, ""


@dataclass
class ShoppingItem:
    """清单里的一项。数量按**原文**保留，不合并、不换算。"""

    name: str
    #: 每条数量原文（同一道菜重复出现时只留一条）
    amounts: list[str] = field(default_factory=list)
    #: 需要这一项的菜名
    recipes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "amounts": list(self.amounts),
            "recipes": list(self.recipes),
        }


def build_shopping_list(
    recipes: list[tuple[str, tuple[str, ...] | list[str]]],
) -> list[ShoppingItem]:
    """把若干道菜的食材合并成清单。

    `recipes` 是 ``(菜名, 食材列表)``；返回按"第一次出现的顺序"排的清单 ——
    保持顺序是为了让界面看起来像用户刚才挑菜的顺序，而不是字母表。
    """
    order: list[str] = []
    by_key: dict[str, ShoppingItem] = {}

    for recipe_name, ingredients in recipes:
        for raw in ingredients:
            name, amount = split_ingredient(str(raw))
            if not name:
                continue
            key = normalize_name(name)
            item = by_key.get(key)
            if item is None:
                item = ShoppingItem(name=name)
                by_key[key] = item
                order.append(key)
            if amount and amount not in item.amounts:
                item.amounts.append(amount)
            if recipe_name and recipe_name not in item.recipes:
                item.recipes.append(recipe_name)

    return [by_key[key] for key in order]


def render_text(items: list[ShoppingItem]) -> str:
    """给"复制到剪贴板"用的纯文本。"""
    lines = ["买菜清单"]
    for item in items:
        amounts = "、".join(item.amounts) if item.amounts else "按需"
        who = "、".join(item.recipes[:3])
        more = f" 等 {len(item.recipes)} 道" if len(item.recipes) > 3 else ""
        lines.append(f"· {item.name}：{amounts}（{who}{more}）")
    return "\n".join(lines)
