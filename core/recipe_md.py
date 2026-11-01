"""把《中国菜谱大全》那份 markdown 解析成结构化菜谱：菜系、菜名、食材与做法。

源文件没有标签与总时长，这里按规则派生，并把估算值在 note 里标清楚。
"""

from __future__ import annotations

import re

#: 与校验层共用同一组上限，避免两边各写一个数字（曾经一个 12 一个 20）
from core.recipe_seed import MAX_INGREDIENTS
from dataclasses import dataclass, field
from typing import Iterable

#: 菜品标题：`## 1. 白切鸡` / `### 1. 北京烤鸭（家庭烤箱版）`
_DISH_RE = re.compile(r"^(#{2,3})\s*(\d+)\s*[.、]\s*(.+?)\s*$")
#: 大区标题：`# 一、粤菜（广东）`
_REGION_RE = re.compile(r"^#\s+(.+?)\s*$")
#: 省份/城市分组标题：`## 北京 · 京菜`（不带序号）
_AREA_RE = re.compile(r"^##\s+(.+?)\s*$")
#: 字段行：`**食材**：…` / `**做法**：`
_FIELD_RE = re.compile(r"^\*\*(食材|做法|原料|步骤)\*\*\s*[:：]?\s*(.*)$")
#: 步骤行：`1. …` / `1、…`
_STEP_RE = re.compile(r"^\d+\s*[.、]\s*(.+)$")

#: 食材没有写用量时补的东西（中文菜谱的常规写法）
NO_AMOUNT = "适量"


#: 带前缀的调料分组：`蘸料：沙姜 2 粒` → 去掉前缀只留内容
_GROUP_PREFIX_RE = re.compile(r"^(蘸料|调料|腌料|浇汁|料汁|脆皮水|酱汁|配菜|菜码)\s*[:：]\s*")

#: 大区 → 地区标签（去掉括号里的省份说明）
_REGION_TAG_RE = re.compile(r"^[一二三四五六七八九十]+、\s*(.+?)\s*$")

#: 估算总时长时加的备料时间
PREP_MINUTES = 10

#: 时间线索
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分钟")
_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?\s*小时")

#: 需要单独计时的**等待段落**（腌制/浸泡/冷藏/发面…），截到下一个分句。
#:
#: ⚠️ 只认真正的等待动作。第一版把单字「浸」也放进来了，
#: 结果"浸煮 18~20 分钟"被当成等待时间，白切鸡从 35 分钟变成 55 分钟。
_WAIT_MARKER_RE = re.compile(
    r"(?:腌制|腌|浸泡|泡发|泡软|冷藏|风干|发面|醒面|静置)[^。；;]{0,16}"
)

#: "过夜"类等待：腌过夜、冷藏过夜、风干过夜、浸泡过夜、发面过夜
_OVERNIGHT_RE = re.compile(r"(?:腌|浸泡|冷藏|风干|发面|醒面|静置)[^。，,]{0,8}过夜")

#: "过夜"按 8 小时折算（晚上腌、第二天做，是家常做法）
OVERNIGHT_MINUTES = 480.0

#: 关键词 → 标签（顺序有意义：先判的优先）
_SPICY_MARKERS = ("辣椒", "花椒", "豆瓣", "小米辣", "干辣椒", "剁椒", "泡椒", "辣")
_PROTEIN_MARKERS = ("鸡", "牛", "猪", "鱼", "虾", "蛋", "豆腐", "肉", "鸭", "鹅", "排骨")
_STAPLE_MARKERS = ("面", "饭", "粉", "饼", "馒头", "饺子", "粥", "年糕", "河粉", "米线", "馍")
_SWEET_MARKERS = ("奶", "糕", "冻", "酥", "蜜", "糖", "甜", "羹")
#: 清淡：白切/白灼/清蒸/清炒/清炖/清汤 这些都是"以本味为主"的做法
_LIGHT_MARKERS = ("清蒸", "白灼", "白切", "清炒", "清炖", "清汤", "凉拌", "素")
#: 蒸：菜名里带这些字就是蒸菜；此外"蒸 N 分钟"这种带时长的蒸制也算（见 derive_tags）
_STEAM_MARKERS = ("蒸", "扣肉")
#: 步骤里的蒸制：`大火足汽蒸 8 分钟`、`上锅蒸 12~15 分钟`
_STEAM_STEP_RE = re.compile(r"蒸[^。]{0,10}\d+\s*(?:~\s*\d+\s*)?分钟")


@dataclass(frozen=True)
class MdDish:
    """从 markdown 解析出的一道菜（还没落库）。"""

    name: str
    region: str
    area: str
    ingredients: tuple[str, ...]
    steps: tuple[str, ...]
    tags: tuple[str, ...]
    time_minutes: int
    #: 解析时发现的可疑点（例如缺食材、步骤太少），供人工复核
    warnings: tuple[str, ...] = field(default_factory=tuple)


def clean_name(title: str) -> str:
    """菜名去掉 markdown 修饰与首尾空白。"""
    text = title.strip().strip("*`")
    # 去掉结尾的句号/逗号这类标点，但保留括号里的说明（"（家庭烤箱版）"是菜名的一部分）
    return text.rstrip("。．.、，")


def split_ingredients(text: str) -> list[str]:
    """把一行食材拆成条目。

    以 `、` 和 `；` 为界；`蘸料：` 这类前缀去掉后再拆（调料本来就是食材的一部分）。
    没有用量数字的条目补 `适量`——落库后界面上"盐适量"比"盐"更有用。
    """
    cleaned = text.strip().rstrip("。．.")
    # 先把分组前缀连同后面的冒号去掉，让"蘸料：沙姜 2 粒"变成"沙姜 2 粒"
    cleaned = re.sub(r"(蘸料|调料|腌料|浇汁|料汁|脆皮水|酱汁|配菜|菜码)\s*[:：]\s*", "", cleaned)
    parts = re.split(r"[、；;]", cleaned)
    out: list[str] = []
    for part in parts:
        item = part.strip().strip("。．.").strip()
        if not item:
            continue
        item = _GROUP_PREFIX_RE.sub("", item).strip()
        if not item:
            continue
        if not _has_amount(item):
            item = f"{item} {NO_AMOUNT}"
        out.append(item)
    return out


def _has_amount(text: str) -> bool:
    """这条食材有没有写"用量"（口径与校验层共用一份实现，别各写一套）。"""
    from core.recipe_seed import has_amount

    return has_amount(text)


def estimate_minutes(steps: Iterable[str], name: str = "") -> int:
    """按文字里的火候线索估算**从开始到能吃**的总时长（分钟）。

    第 53 步修正：以前只算"最长的一次火候"，于是把这两类算成了快手菜——
    **椰子鸡火锅 20 分钟**（备料、烧锅、涮着吃都不止）、
    **蜜汁叉烧 40 分钟**（步骤里明明写着"冷藏腌过夜"）。
    时间一错，右栏「时间」筛出来的结果就是假的。

    现在的口径：
    - **等待类**（腌/泡/冷藏/风干/发面/醒面/过夜）取最长的那一段；
      "过夜"按 8 小时算（一次性腌好第二天做，是家常做法）；
    - **火候类**（煮/蒸/炖/烤 N 分钟或 N 小时）取最长的那一次；
    - 两者**相加**（腌制通常在火候之前），再加备料时间；
    - 向上取整到 5 分钟，上限 600（10 小时，界面上会显示成"约 10 小时"）。

    宁可偏长：用户看到"约 8 小时"会自己决定要不要提前腌；
    看到"40 分钟"却发现要腌过夜，那才是真的坑。
    """
    text = " ".join([name, *steps])

    # 1) 等待类：腌制/浸泡/冷藏/风干/发面，可能是"过夜"，也可能是"N 小时/分钟"。
    #    多个等待段取**最长**的那段（一般也就一段腌料）。
    wait_minutes = 0.0
    if _OVERNIGHT_RE.search(text):
        wait_minutes = OVERNIGHT_MINUTES
    for clause in _WAIT_MARKER_RE.finditer(text):
        chunk = clause.group(0)
        for value in _HOURS_RE.findall(chunk):
            wait_minutes = max(wait_minutes, float(value) * 60)
        for value in _MINUTES_RE.findall(chunk):
            wait_minutes = max(wait_minutes, float(value))

    # 2) 火候类：把等待段从文本里剔掉，剩下的时间mention**求和**。
    #    为什么是求和：做法是一步接一步的（"炖 1 小时"接着"再煮 20 分钟"），
    #    总时长应当累加；只取最大值会把台湾牛肉面那种 120+90 算成 120。
    cook_text = _WAIT_MARKER_RE.sub(" ", text)
    cook_minutes = 0.0
    for value in _HOURS_RE.findall(cook_text):
        cook_minutes += float(value) * 60
    for value in _MINUTES_RE.findall(cook_text):
        cook_minutes += float(value)

    if not cook_minutes and not wait_minutes:
        # 完全没有时间线索：按"一顿快手菜"给个保守值，并在 note 里说明是估算
        cook_minutes = 30

    total = wait_minutes + cook_minutes + PREP_MINUTES
    rounded = int(5 * round(total / 5))
    return max(5, min(600, rounded))


def place_tag(region: str, area: str) -> str:
    """地区/菜系标签。

    菜系区块（`# 一、粤菜（广东）`）用**菜系名**（"粤菜"），
    省份区块（`## 北京 · 京菜`）用**省市名**（"北京"）。
    两者都是用户真会说的词——"想吃川菜"比"想吃四川"更常见，
    而"北京烤鸭"这种当然是按城市找。
    """
    if area.strip():
        return area.strip()
    # 去掉"（广东）"这种省份说明，只留菜系名
    return re.sub(r"[（(].*?[)）]", "", region).strip()


def derive_tags(region: str, area: str, name: str, ingredients: str, steps: str) -> list[str]:
    """按关键词派生标签，**只用 `TAG_VOCABULARY` 里有的词**。

    一条重要教训（第一版就是这么错的）：**不能在"全部文本"里找关键词**。
    - 蜜汁叉烧因为步骤里有"表面刷蜂蜜"的"面"被判成「主食」；
    - 白切鸡因为蘸料要"拌匀"被判成「凉拌」；
    - 44 道菜因为"加高汤"被判成「养胃」。

    所以这里的口径是：
    - **菜名**决定品类（主食/汤/蒸……），这是最可靠的信号；
    - **食材**决定口味与蛋白（辣椒、豆瓣、鸡鸭鱼虾……）；
    - 步骤只用来补做法（炒/炸/烤），且只认明确的动作词。
    """
    from core.recipe_seed import TAG_VOCABULARY

    dish_name = name
    ingredient_text = ingredients
    step_text = steps
    tags: list[str] = []

    # 1) 地区/菜系：最可靠的一维，放第一个
    place = place_tag(region, area)
    if place:
        tags.append(place)

    # 2) 口味：辣度只看**食材**（辣椒/豆瓣/花椒），不看步骤里的"辣椒面"引用
    spicy_hits = sum(ingredient_text.count(marker) for marker in _SPICY_MARKERS)
    if spicy_hits >= 2:
        tags.append("辣")
    elif spicy_hits == 1:
        tags.append("微辣")
    elif ("糖" in ingredient_text and "醋" in ingredient_text) or "酸甜" in dish_name:
        tags.append("酸甜")
    elif any(word in dish_name for word in _LIGHT_MARKERS):
        tags.append("清淡")
    elif dish_name:
        tags.append("咸香")

    # 3) 做法：菜名优先，其次是明确的动作词
    # 「蒸」还有第二种判据：步骤里出现"蒸 N 分钟"这种**带时长的蒸制**。
    # 名字不一定写"蒸"（剁椒鱼头、粉蒸肉），但"上锅蒸 12 分钟"就说明它是蒸菜。
    steamed = any(word in dish_name for word in _STEAM_MARKERS) or bool(
        _STEAM_STEP_RE.search(step_text)
    )
    cold = "凉拌" in dish_name or "拌" in dish_name
    soup = any(word in dish_name for word in ("汤", "羹", "煲", "炖"))
    if steamed:
        tags.append("蒸")
    if cold:
        tags.append("凉拌")
    if soup:
        tags.append("汤")
    # 「炒」只在**主做法不是蒸/凉拌/汤**时才加：剁椒鱼头、凯里酸汤鱼的步骤里
    # 也有"炒香配料"，但它们主要靠蒸/煮，标成炒会让"想炒个菜"的人被误导。
    if not (steamed or cold or soup) and any(
        word in step_text for word in ("炒", "爆", "煸", "煎", "炸", "烤")
    ):
        tags.append("炒")

    # 4) 品类：**只看菜名**
    if any(word in dish_name for word in _STAPLE_MARKERS):
        tags.append("主食")
    if any(word in dish_name for word in ("汤", "羹", "粥")):
        tags.append("养胃")
    if any(word in dish_name for word in _SWEET_MARKERS):
        tags.append("加餐")

    # 5) 蛋白：只看前几样主料，避免"有葱有姜"就算高蛋白
    mains = ingredient_text[:60]
    if any(word in mains for word in _PROTEIN_MARKERS):
        tags.append("高蛋白")

    # 6) 下饭：辣菜或红酱类
    if "辣" in tags or any(word in dish_name for word in ("红烧", "酱", "卤", "梅菜")):
        tags.append("下饭")


    seen: list[str] = []
    for tag in tags:
        if tag in TAG_VOCABULARY and tag not in seen:
            seen.append(tag)
    return seen[:6]


def parse_markdown(text: str) -> list[MdDish]:
    """把整份 markdown 解析成菜谱列表（保序）。"""
    lines = text.splitlines()
    dishes: list[MdDish] = []

    region = ""
    area = ""
    index = 0
    while index < len(lines):
        line = lines[index]

        region_match = _REGION_RE.match(line)
        # `# ` 是空行级标题：更新大区，同时把"当前省份"清掉
        if region_match and not line.startswith("##"):
            region = _REGION_TAG_RE.sub(r"\1", region_match.group(1))
            area = ""
            index += 1
            continue

        area_match = _AREA_RE.match(line)
        if area_match and not _DISH_RE.match(line):
            # `## 北京 · 京菜` 这种不带序号的分组标题
            area = area_match.group(1).split("·")[0].strip()
            index += 1
            continue

        dish_match = _DISH_RE.match(line)
        if not dish_match:
            index += 1
            continue

        level = len(dish_match.group(1))
        name = clean_name(dish_match.group(3))
        body, index = _collect_body(lines, index + 1, level)
        ingredients, steps = _split_body(body)

        warnings: list[str] = []
        if len(ingredients) < 2:
            warnings.append(f"食材只有 {len(ingredients)} 项")
        if len(steps) < 2:
            warnings.append(f"做法只有 {len(steps)} 步")
        if len(ingredients) > MAX_INGREDIENTS:
            warnings.append(f"食材 {len(ingredients)} 项（超过 {MAX_INGREDIENTS}）")
        if len(steps) > 10:
            warnings.append(f"做法 {len(steps)} 步（超过 10）")

        dishes.append(
            MdDish(
                name=name,
                region=region,
                area=area,
                ingredients=tuple(ingredients),
                steps=tuple(steps),
                tags=tuple(
                    derive_tags(
                        region, area, name, " ".join(ingredients), " ".join(steps)
                    )
                ),
                time_minutes=estimate_minutes(steps, name),
                warnings=tuple(warnings),
            )
        )

    return dishes


def _collect_body(lines: list[str], start: int, level: int) -> tuple[list[str], int]:
    """收集一道菜的行，直到遇到同级或更高级的标题。"""
    stop = re.compile(rf"^#{{1,{level}}}\s")
    body: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if stop.match(line):
            break
        body.append(line)
        index += 1
    return body, index


def _split_body(body: list[str]) -> tuple[list[str], list[str]]:
    """从"**食材** / **做法**"两段里分别取出条目。"""
    ingredients: list[str] = []
    steps: list[str] = []
    mode = ""

    for raw in body:
        line = raw.strip()
        if not line:
            continue

        field_match = _FIELD_RE.match(line)
        if field_match:
            kind = field_match.group(1)
            mode = "steps" if kind in ("做法", "步骤") else "ingredients"
            remainder = field_match.group(2).strip()
            if remainder:
                if mode == "ingredients":
                    ingredients.extend(split_ingredients(remainder))
            continue

        if mode == "steps":
            step_match = _STEP_RE.match(line)
            if step_match:
                steps.append(step_match.group(1).strip())
        elif mode == "ingredients":
            # 食材允许续行（少数条目太长被折到下一行）
            if line.startswith(("**", "#", "|")):
                mode = ""
                continue
            ingredients.extend(split_ingredients(line))

    return ingredients, steps


def to_seed_item(dish: MdDish) -> dict:
    """转成 `seed_recipes.json` 的一条（不含 source，由导入器盖章）。"""
    place = dish.area or dish.region
    note_bits = []
    if place:
        note_bits.append(f"{place}风味")
    note_bits.append("时长按做法里的火候估算")
    return {
        "name": dish.name,
        "ingredients": list(dish.ingredients),
        "steps": list(dish.steps),
        "tags": list(dish.tags),
        "time_minutes": dish.time_minutes,
        "note": "；".join(note_bits),
    }
