"""《中国菜谱大全》markdown 解析：解析错了不会报错、只会让库里出现残缺菜谱，所以口径要逐条钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_md import (  # noqa: E402
    estimate_minutes,
    parse_markdown,
    place_tag,
    split_ingredients,
    to_seed_item,
)

#: 一份最小样本，覆盖两种标题层级（菜系下直接挂菜、省份下挂菜）
SAMPLE = """# 一、粤菜（广东）

**风味特点**：清、鲜、嫩。

## 1. 白切鸡

**食材**：三黄鸡半只（约 750g）、姜 1 大块；蘸料：沙姜 2 粒、小葱 1 根。

**做法**：
1. 鸡洗净，冷水下锅没过鸡身，放姜片葱段。
2. 浸煮 18~20 分钟，捞出放冰水浸 5 分钟。
3. 沥干斩件装盘。

## 2. 白灼虾

**食材**：基围虾 500g、姜 3 片。

**做法**：
1. 虾剪去虾须，冲洗干净。
2. 水开后下虾煮 2 分钟捞出。

# 九、直辖市菜品（京津沪渝）

## 北京 · 京菜

### 1. 炸酱面

**食材**：手擀面 500g、干黄酱 100g。

**做法**：
1. 干黄酱加水澥开备用。
2. 面煮熟，浇酱拌匀即可。

# 附录：常用烹饪术语速查

## 火候与油温

| 术语 | 说明 |
|---|---|
| 大火 | 旺火快炒 |
"""


# ------------------------------------------------------------------ 结构解析


def test_parses_both_heading_levels():
    """菜系下的菜是 `##`，省份下的菜是 `###`——两种都要认。"""
    dishes = parse_markdown(SAMPLE)
    assert [dish.name for dish in dishes] == ["白切鸡", "白灼虾", "炸酱面"]


def test_appendix_headings_are_not_dishes():
    """附录（火候术语表）的标题不带序号，不能变成菜。"""
    dishes = parse_markdown(SAMPLE)
    assert not any("火候" in dish.name for dish in dishes)
    assert all("术语" not in dish.name for dish in dishes)


def test_place_tag_prefers_area_then_drops_province_note():
    """省份区块用省市名，菜系区块用菜系名（去掉括号里的省份说明）。"""
    assert place_tag("粤菜（广东）", "") == "粤菜"
    assert place_tag("九、直辖市菜品", "北京") == "北京"
    dishes = parse_markdown(SAMPLE)
    assert dishes[0].tags[0] == "粤菜"
    assert dishes[2].tags[0] == "北京"


# ------------------------------------------------------------------ 食材


def test_ingredients_are_split_and_kept_faithful():
    assert split_ingredients("三黄鸡半只（约 750g）、姜 1 大块") == [
        "三黄鸡半只（约 750g）",
        "姜 1 大块",
    ]


def test_missing_amount_gets_a_placeholder():
    """没写用量的（"荷叶饼""甜面酱"）补"适量"，否则校验会判成缺陷。"""
    assert split_ingredients("荷叶饼、甜面酱") == ["荷叶饼 适量", "甜面酱 适量"]


def test_chinese_numerals_count_as_amounts():
    """中文数字就是量：不能因为"一盆""半茶匙"没有阿拉伯数字就补"适量"。"""
    result = split_ingredients("冰水一盆、糖半茶匙、蒜两瓣、香油几滴、盐少许")
    assert result == ["冰水一盆", "糖半茶匙", "蒜两瓣", "香油几滴", "盐少许"]


def test_dipping_sauce_prefix_is_stripped_and_merged():
    """`蘸料：` 这类前缀去掉后并入主料——调料本来也是食材。"""
    result = split_ingredients("鸡 1 只；蘸料：沙姜 2 粒、小葱 1 根")
    assert result == ["鸡 1 只", "沙姜 2 粒", "小葱 1 根"]


# ------------------------------------------------------------------ 时长


def test_estimates_time_from_the_longest_wait():
    """时长取"最长的一次等待 + 备料"，不能把要炖两小时的说成十五分钟。"""
    quick = estimate_minutes(["大火足汽蒸 8 分钟，关火虚蒸 2 分钟。"])
    slow = estimate_minutes(["转小火加盖慢煲 1.5 小时，再加玉米煲 30 分钟。"])
    assert 15 <= quick <= 25, quick
    assert slow >= 100, slow


def test_falls_back_to_a_conservative_time_without_clues():
    assert estimate_minutes(["把材料拌匀装盘。"]) == 30 + 10


def test_overnight_marinade_counts_as_hours():
    """第 53 步修正：`冷藏腌过夜` 不能再被算成零成本。

    实测过的坑：蜜汁叉烧步骤里明明是"冷藏腌过夜"，却被估成 40 分钟，
    于是它出现在"30 分钟内"的筛选结果里。
    """
    minutes = estimate_minutes(
        ["所有调料拌匀抹肉，冷藏腌过夜。", "烤箱 180℃ 烤 30 分钟。"]
    )
    assert minutes >= 480, f"腌过夜至少要按 8 小时算，实际 {minutes}"


def test_cooking_words_with_soak_prefix_are_not_wait_time():
    """`浸煮 18~20 分钟` 是火候，不是等待。

    第一版把单字「浸」也当成等待动作，白切鸡从 35 分钟被算成 55 分钟。
    """
    minutes = estimate_minutes(["保持微沸状态浸煮 20 分钟，捞出放冰水浸 5 分钟。"])
    assert minutes <= 45, f"浸煮不该被重复计时，实际 {minutes}"


def test_sequential_cooking_times_add_up():
    """做法是一步接一步的：炖 1 小时 + 再煮 20 分钟 ≈ 90 分钟，不是取最大值。"""
    minutes = estimate_minutes(["小火炖 1 小时。", "加配菜再煮 20 分钟。"])
    assert 80 <= minutes <= 100, minutes


# ------------------------------------------------------------------ 标签


def test_staple_tag_only_from_the_dish_name():
    """「主食」只能看菜名——否则"表面刷蜂蜜"的"面"会把叉烧判成主食。"""
    dishes = {dish.name: dish for dish in parse_markdown(SAMPLE)}
    assert "主食" in dishes["炸酱面"].tags
    assert "主食" not in dishes["白切鸡"].tags


def test_cold_dish_tag_needs_a_real_salad_not_just_mixing():
    """「凉拌」不能因为蘸料要"拌匀"就打上（白切鸡就是这么被误判的）。"""
    dishes = {dish.name: dish for dish in parse_markdown(SAMPLE)}
    assert "凉拌" not in dishes["白切鸡"].tags


def test_spiciness_comes_from_ingredients():
    text = SAMPLE.replace("基围虾 500g、姜 3 片", "基围虾 500g、干辣椒 5 个、花椒 1 勺")
    dishes = {dish.name: dish for dish in parse_markdown(text)}
    assert "辣" in dishes["白灼虾"].tags


def test_cooking_method_tag_follows_the_main_method():
    """步骤里有"蒸 N 分钟"就算蒸菜；主做法是蒸时不再加「炒」。

    实测过：剁椒鱼头、凯里酸汤鱼都标成了「炒」——它们其实是蒸/煮，
    标错会让"想炒个菜"的人被带偏。
    """
    text = """
# 七、湘菜（湖南）

## 1. 剁椒鱼头

**食材**：胖头鱼头 1 个、剁椒 3 勺。

**做法**：
1. 剁椒下锅炒香，铺在鱼头上。
2. 水开后上锅大火蒸 12 分钟。
3. 淋热油，撒葱花即可上桌。
"""
    dish = parse_markdown(text)[0]
    assert "蒸" in dish.tags, f"步骤里写了「蒸 12 分钟」，应当算蒸菜：{dish.tags}"
    assert "炒" not in dish.tags, dish.tags
    assert "辣" in dish.tags or "微辣" in dish.tags


def test_no_time_based_tag_is_added():
    """「快手」这个标签已被去掉（第 55 步，用户："意义不大"）。

    理由：它和右栏的「时间」组完全重复（15/30 分钟内、30 分钟以上），
    而且名字有歧义（和短视频平台同名）。时长本身仍然算、仍然能按它筛，
    只是不再往数据里写一个同义标签。
    """
    dishes = parse_markdown(SAMPLE)
    for dish in dishes:
        assert "快手" not in dish.tags, f"{dish.name} 不该再有快手标签"


# ------------------------------------------------------------------ 真实数据


def test_real_seed_file_parses_cleanly():
    """真实种子（96 道）必须零问题——这是"数据能进库"的最后一道闸。"""
    from core.recipe_seed import load_seed, validate_seed

    items = load_seed()
    assert len(items) == 96
    assert validate_seed(items) == []

    # 每道菜都带一个地区/菜系标签，且第一个标签就是它（前端按第一个显示）
    for item in items:
        assert item["tags"], item["name"]
        assert item["tags"][0] in (
            "粤菜", "川菜", "鲁菜", "苏菜", "浙菜", "闽菜", "湘菜", "徽菜",
            "北京", "天津", "上海", "重庆", "河北", "山西", "内蒙古",
            "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "青海", "宁夏", "新疆",
            "云南", "贵州", "西藏", "广西", "河南", "湖北", "江西", "海南",
            "香港", "澳门", "台湾",
        ), f"{item['name']} 的第一个标签不是地区：{item['tags']}"


def test_to_seed_item_marks_time_as_an_estimate():
    """时长是**算出来的**，note 里必须说明，不能让它看起来像精确值。"""
    dish = parse_markdown(SAMPLE)[0]
    item = to_seed_item(dish)
    assert "估算" in item["note"]
    assert "source" not in item, "来源由导入器统一盖章"
    assert set(item) <= {"name", "ingredients", "steps", "tags", "time_minutes", "note"}
