"""内置种子菜谱的加载、校验与导入。

内容在 `core/seed_recipes.json`，入库时统一盖 `source="seed"`；按菜名幂等跳过已有条目，
不覆盖用户自己录的版本。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.recipe_library import DuplicateRecipe, Recipe, create_recipe, normalize_name_key
from core.storage import Storage

logger = logging.getLogger(__name__)

#: 种子数据文件（与代码同目录，是源码资产而不是运行时数据）
SEED_PATH = Path(__file__).resolve().parent / "seed_recipes.json"

#: 允许的标签词表。**种子只能用这些**，因为第 50 步的标签面板要按这份词表
#: 归类展示；自创标签会让面板出现一堆只出现一次的孤儿标签。
#: 注意：**只约束种子**。用户自己录的菜谱（第 49 步）不受这份表限制——
#: 用户有权用自己的说法。
TAG_VOCABULARY: tuple[str, ...] = (
    # 口味
    "清淡", "微辣", "辣", "酸甜", "咸香",
    # 需求
    "减脂", "高蛋白", "低卡", "养胃", "平价",
    # 做法/设备
    "一锅出", "免开火", "电饭煲", "小电锅", "蒸", "煮", "炒", "凉拌", "汤",
    # 第 53 步补的做法标签：96 道地方菜里"烤/炖/煎/炸/焖"很常见，
    # 没有这些词，蜜汁叉烧只能被硬塞进「炒」（实测发现的误标）。
    "烤", "炖", "煎", "炸", "焖",
    # 场景
    "早餐", "主食", "下饭", "加餐",
    # 第 53 步：素食（"想吃素"是很常见的诉求，原来的词表表达不了）
    "素",
    # 地区/菜系（50f：换成《中国菜谱大全》后，按省份找菜是主要入口）
    # 八大菜系用菜系名（用户更常说"想吃川菜"而不是"想吃四川"），
    # 其余用省市名（"北京烤鸭"当然按城市找）。
    "粤菜", "川菜", "鲁菜", "苏菜", "浙菜", "闽菜", "湘菜", "徽菜",
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "陕西", "甘肃", "青海", "宁夏", "新疆",
    "云南", "贵州", "西藏", "广西",
    "河南", "湖北", "江西", "海南",
    "香港", "澳门", "台湾",
)

#: 没有数字、但确实是"用量"的词。**中文菜谱本来就这么写**：
#: "盐适量""香油几滴""淀粉各少许"——只认阿拉伯数字会把它们全判成缺陷。
AMOUNT_WORDS: tuple[str, ...] = (
    "适量", "少许", "若干", "一点", "些许", "按需", "几滴", "几粒", "一把", "一小把",
)

#: 中文数字也算用量："冰水一盆""蒜两瓣""糖半茶匙"里的"一/两/半"就是量。
CJK_NUMERALS = "一二三四五六七八九十百千万两半几"


def has_amount(text: str) -> bool:
    """这条食材有没有写"用量"。

    三种都认：阿拉伯数字（`500g`）、中文数字（`一盆`/`半茶匙`）、
    模糊量词（`适量`/`少许`/`几滴`）。
    """
    if any(ch.isdigit() for ch in text):
        return True
    if any(word in text for word in AMOUNT_WORDS):
        return True
    return any(ch in CJK_NUMERALS for ch in text)


#: 校验口径：用量必须能看出来、步骤必须够长（挡住"炒熟即可"这种空话）
MIN_INGREDIENTS = 2
#: 上限放到 20：真实家常菜/面食（螺蛳粉、牛肉面）的配料表本来就会到 15~18 项，
#: 原来的 12 会把"真实但丰富"的菜谱判成缺陷。界面上仍只展示前面的主料。
MAX_INGREDIENTS = 20
MIN_STEPS = 2
MAX_STEPS = 10
#: 5 个字：能挡住"炒熟即可"(4)、"装盘即可"(4)，但放过
#: "斩件摆盘"(5)、"加盐调味"(5) 这种真实存在的收尾步骤。
#: 原来的 8 把 5 条好步骤判成了缺陷（换成真实菜谱后立刻暴露）。
MIN_STEP_CHARS = 5
MIN_TIME_MINUTES = 1
MAX_TIME_MINUTES = 600


#: 标签数据文件（第 56 步 P1-1）。
#:
#: 标签**从种子里拆出来单独放**，因为它是模型离线打标的产物，
#: 而种子是"内容"（食材/做法/时长）。分开之后：
#: - 重跑 `import_recipe_md.py` 只影响内容，不会冲掉标签；
#: - 标签能单独审查、单独回滚；
#: - 文件头能记录**血缘**（模型、提示词哈希、打标日期、复核条数）。
LABELS_PATH = Path(__file__).resolve().parent / "recipe_labels.json"


class SeedError(Exception):
    """种子数据本身有问题（读不到 / 格式错 / 没通过校验）。"""


def load_labels(path: Path | None = None) -> dict[str, list[str]]:
    """读标签文件；不存在时返回空 dict（兼容只有种子的老数据）。"""
    target = path or LABELS_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(f"标签文件读不了（{target}）：{type(exc).__name__}: {exc}") from exc
    labels = data.get("labels") if isinstance(data, dict) else None
    return dict(labels) if isinstance(labels, dict) else {}


def load_seed(
    path: Path | None = None, *, labels_path: Path | None = None
) -> list[dict[str, Any]]:
    """读种子 JSON，返回原始条目列表（**已合并标签文件**）。

    只做"能不能读成 JSON、顶层结构对不对"这一层；
    字段级校验交给 :func:`validate_seed`（这样测试能一次看到全部问题，
    而不是修一条报一条）。
    """
    target = path or SEED_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeedError(f"读不到种子文件 {target}：{type(exc).__name__}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeedError(f"种子文件不是合法 JSON（{target}）：{exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("recipes"), list):
        raise SeedError(f"种子文件顶层必须是 {{\"recipes\": [...]}}（{target}）")

    items = list(data["recipes"])
    labels = load_labels(labels_path)
    if labels:
        # 标签文件优先：种子里残留的 tags 只是历史遗留，不覆盖新标签
        for item in items:
            name = str(item.get("name") or "").strip()
            if name in labels:
                item["tags"] = list(labels[name])
    return items


def validate_seed(items: list[dict[str, Any]]) -> list[str]:
    """逐条校验种子，返回**问题清单**（空清单 = 通过）。

    刻意返回清单而不是抛第一个异常：种子是数据，一次看全所有毛病
    比"修一条跑一次"高效得多，测试里也能直接断言清单为空。
    """
    problems: list[str] = []
    seen_keys: dict[str, int] = {}

    for index, item in enumerate(items, start=1):
        where = f"第 {index} 条"
        if not isinstance(item, dict):
            problems.append(f"{where}：不是对象")
            continue

        name = str(item.get("name") or "").strip()
        if not name:
            problems.append(f"{where}：缺 name")
        else:
            where = f"{where}（{name}）"
            key = normalize_name_key(name)
            if key in seen_keys:
                problems.append(f"{where}：菜名与第 {seen_keys[key]} 条重复")
            else:
                seen_keys[key] = index

        ingredients = item.get("ingredients")
        if not isinstance(ingredients, list):
            problems.append(f"{where}：ingredients 必须是数组")
        else:
            if not MIN_INGREDIENTS <= len(ingredients) <= MAX_INGREDIENTS:
                problems.append(
                    f"{where}：ingredients 应为 {MIN_INGREDIENTS}-{MAX_INGREDIENTS} 项，"
                    f"实际 {len(ingredients)}"
                )
            for position, entry in enumerate(ingredients, start=1):
                text = str(entry).strip()
                if not text:
                    problems.append(f"{where}：ingredients 第 {position} 项为空")
                elif not has_amount(text):
                    problems.append(
                        f"{where}：ingredients 第 {position} 项没有用量：{text!r}"
                    )

        steps = item.get("steps")
        if not isinstance(steps, list):
            problems.append(f"{where}：steps 必须是数组")
        else:
            if not MIN_STEPS <= len(steps) <= MAX_STEPS:
                problems.append(
                    f"{where}：steps 应为 {MIN_STEPS}-{MAX_STEPS} 步，实际 {len(steps)}"
                )
            for position, entry in enumerate(steps, start=1):
                text = str(entry).strip()
                if len(text) < MIN_STEP_CHARS:
                    problems.append(f"{where}：steps 第 {position} 步太短（像空话）：{text!r}")

        tags = item.get("tags")
        if not isinstance(tags, list) or not tags:
            problems.append(f"{where}：tags 必须是非空数组")
        else:
            unknown = [str(tag) for tag in tags if str(tag) not in TAG_VOCABULARY]
            if unknown:
                problems.append(f"{where}：标签不在词表内：{unknown}")
            if len(set(map(str, tags))) != len(tags):
                problems.append(f"{where}：标签有重复")

        minutes = item.get("time_minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            problems.append(f"{where}：time_minutes 必须是整数")
        elif not MIN_TIME_MINUTES <= minutes <= MAX_TIME_MINUTES:
            problems.append(
                f"{where}：time_minutes 应在 {MIN_TIME_MINUTES}-{MAX_TIME_MINUTES} 之间，"
                f"实际 {minutes}"
            )

        if "note" in item and not isinstance(item["note"], str):
            problems.append(f"{where}：note 必须是字符串")

    return problems


def import_seed(storage: Storage, path: Path | None = None) -> dict[str, Any]:
    """把种子导入知识库，**幂等**。

    返回 ``{"added": n, "skipped": n, "problems": [...], "total": n}``：
    - 已存在的（`name_key` 撞上）算 skipped，**不覆盖**——
      用户自己录的版本更该保留；
    - `problems` 非空时**不导入任何一条**（先修数据，别把半套种子灌进去）；
      这条口径让"点一次导入"的结果永远是可解释的。
    """
    items = load_seed(path)
    problems = validate_seed(items)
    if problems:
        raise SeedError("种子数据没通过校验，已拒绝导入：\n- " + "\n- ".join(problems[:20]))

    added = 0
    skipped = 0
    for item in items:
        try:
            create_recipe(
                storage,
                name=item["name"],
                ingredients=item["ingredients"],
                steps=item["steps"],
                tags=item["tags"],
                time_minutes=item["time_minutes"],
                # 来源由导入器统一盖章：JSON 里不写，避免 30 条里漏几条
                source="seed",
                origin_url="",
                note=str(item.get("note") or ""),
            )
        except DuplicateRecipe:
            skipped += 1
    added = len(items) - skipped

    logger.info("导入种子菜谱：新增 %s 条，跳过已存在 %s 条", added, skipped)
    return {
        "added": added,
        "skipped": skipped,
        "problems": [],
        "total": len(items),
    }


def replace_seed(storage: Storage, path: Path | None = None) -> dict[str, Any]:
    """**整体替换**示例菜谱：先删掉旧的 `source="seed"`，再导入当前的种子文件。

    第 50 步（50f）新增：用户换了整套菜谱数据（《中国菜谱大全》96 道），
    原来那 30 道要一起清掉——只 `import_seed` 的话，旧菜会和新菜混在一起，
    "想吃鱼"时还会翻出旧库里的示例菜。

    两条安全边界：
    - **只删 `seed`**：用户自己录的（`source="user"`）一条都不动；
    - **先校验后删除**：种子没通过校验就直接抛错，绝不出现"旧的删了、新的没进来"。
    """
    items = load_seed(path)
    problems = validate_seed(items)
    if problems:
        raise SeedError(
            "新种子没通过校验，已拒绝替换（旧数据原样保留）：\n- "
            + "\n- ".join(problems[:20])
        )

    removed = storage.execute("DELETE FROM recipes WHERE source = 'seed'").rowcount
    result = import_seed(storage, path)
    logger.info("替换示例菜谱：删除 %s 条旧的，导入 %s 条", removed, result["added"])
    return {**result, "removed": removed}


def seed_recipes(path: Path | None = None) -> list[Recipe]:
    """"种子长什么样"的自检入口：把 JSON 解析成 Recipe 形态（**不落库**）。

    给测试与"导入前预览"用；落库一律走 :func:`import_seed`。
    """
    items = load_seed(path)
    problems = validate_seed(items)
    if problems:
        raise SeedError("种子数据没通过校验：\n- " + "\n- ".join(problems[:20]))

    out: list[Recipe] = []
    for index, item in enumerate(items, start=1):
        out.append(
            Recipe(
                id=index,                      # 未落库，id 只是占位序号
                name=str(item["name"]).strip(),
                ingredients=tuple(str(x) for x in item["ingredients"]),
                steps=tuple(str(x) for x in item["steps"]),
                tags=tuple(str(x) for x in item["tags"]),
                time_minutes=int(item["time_minutes"]),
                source="seed",
                origin_url="",
                note=str(item.get("note") or ""),
                created_at="",
                updated_at="",
            )
        )
    return out
