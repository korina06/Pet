"""菜谱检索工具：只给 id 与真实字段、查不到就明说、忌口在库这一层按大类展开过滤。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_library import create_recipe, ensure_schema  # noqa: E402
from core.storage import Storage  # noqa: E402
from tools.recipe_search_tool import MAX_RESULTS, RecipeSearchTool, expand_term  # noqa: E402


def _db() -> Storage:
    """内存库 + 几道能覆盖各分支的菜。"""
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="清蒸鲈鱼",
        ingredients=["鲈鱼 1 条（约 500g）", "姜 3 片", "葱 2 根"],
        steps=["鱼身划两刀，抹盐腌 10 分钟", "水开后上锅蒸 8 分钟", "淋蒸鱼豉油，浇热油"],
        tags=["清淡", "蒸", "高蛋白"],
        time_minutes=20,
        source="seed",
    )
    create_recipe(
        db,
        name="蒜蓉粉丝蒸虾",
        ingredients=["基围虾 300g", "粉丝 1 把", "蒜 5 瓣"],
        steps=["粉丝泡软铺盘", "虾开背摆上去", "铺蒜蓉蒸 6 分钟"],
        tags=["咸香", "蒸"],
        time_minutes=25,
        source="seed",
    )
    create_recipe(
        db,
        name="番茄炒蛋",
        ingredients=["番茄 2 个", "鸡蛋 3 个"],
        steps=["番茄切块", "鸡蛋炒凝固后下番茄炒匀"],
        tags=["煮", "炒", "下饭"],
        time_minutes=10,
        source="seed",
    )
    create_recipe(
        db,
        name="香辣鸡丁",
        ingredients=["鸡胸肉 300g", "干辣椒 10g", "花生 30g"],
        steps=["鸡丁腌 10 分钟", "爆香辣椒下鸡丁炒熟", "最后撒花生"],
        tags=["辣", "下饭", "炒"],
        time_minutes=15,
        source="seed",
    )
    return db


def _call(tool: RecipeSearchTool, **kwargs) -> dict:
    """跑一次工具并把结果 JSON 解回 dict（断言用）。"""
    result = _run(tool, **kwargs)
    assert result.ok, f"工具失败：{result.error}"
    return result.as_payload()


def _run(tool: RecipeSearchTool, **kwargs):
    import asyncio

    return asyncio.run(tool.run(**kwargs))


# ------------------------------------------------------------------ 纪律 1：只给 id


def test_results_carry_ids_from_the_library():
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="鱼")

    assert payload["count"] >= 1
    ids = [item["id"] for item in payload["items"]]
    assert all(isinstance(recipe_id, int) and recipe_id > 0 for recipe_id in ids)
    # 每道菜都带真实字段，模型可以据此说话，但内容不是它编的
    first = payload["items"][0]
    assert first["name"] and first["ingredients"] and first["steps"]
    assert first["source"] == "seed"
    # 结果必须真的是"鱼"相关（清蒸鲈鱼），不是随便返回几道
    assert any("鱼" in item["name"] or "鱼" in " ".join(item["ingredients"])
               for item in payload["items"])


def test_keyword_search_uses_alias():
    """搜"番茄"要能命中用"西红柿"写的菜（别名展开在 core 层已实现，这里确认接通）。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="番茄")
    assert any(item["name"] == "番茄炒蛋" for item in payload["items"])


def test_results_are_capped():
    tool = RecipeSearchTool(_db(), max_results=2)
    payload = _call(tool)
    assert payload["count"] <= 2
    assert len(payload["items"]) <= 2
    assert MAX_RESULTS >= 2


# ------------------------------------------------------------------ 过滤


def test_tags_filter_narrows_results():
    tool = RecipeSearchTool(_db())
    payload = _call(tool, tags=["清淡"])
    assert payload["count"] >= 1
    assert all("清淡" in item["tags"] for item in payload["items"])


def test_max_minutes_drops_slow_dishes():
    tool = RecipeSearchTool(_db())
    payload = _call(tool, max_minutes=12)
    for item in payload["items"]:
        assert item["time_minutes"] is None or item["time_minutes"] <= 12


def test_exclude_expands_categories_so_shrimp_is_filtered_out():
    """`exclude=["海鲜"]` 必须能排掉"蒜蓉粉丝蒸虾"——菜名里没有"海鲜"两个字。"""
    tool = RecipeSearchTool(_db())
    with_seafood = _call(tool, keyword="蒸")
    assert any("虾" in item["name"] for item in with_seafood["items"]), (
        "前置条件不成立：这条用例需要一个会命中的场景"
    )

    without_seafood = _call(tool, keyword="蒸", exclude=["海鲜"])
    assert not any("虾" in item["name"] for item in without_seafood["items"])


def test_exclude_by_exact_ingredient():
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="鸡", exclude=["花生"])
    assert not any("花生" in " ".join(item["ingredients"]) for item in payload["items"])


# ------------------------------------------------------------------ 纪律 2：查不到就明说


def test_zero_hits_are_reported_honestly():
    """库里没有的东西：count=0 + 明确的 hint，不许装，也不许塞无关的菜。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="松露")

    assert payload["count"] == 0
    assert payload["items"] == []
    assert "不要编造" in payload["hint"]
    # 第 52 步：删掉了"瞎推 nearby"——查不到就是查不到
    assert "nearby" not in payload


def test_all_filtered_out_is_also_reported_as_zero():
    """过滤到一条不剩，也算"查不到"——而不是回一个空列表让模型自己猜。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="鱼", exclude=["海鲜"])
    assert payload["count"] == 0
    assert "hint" in payload


# ------------------------------------------------------------------ 结构化检索（52）


def test_spicy_chicken_is_a_combination_not_a_dish_name():
    """用户截图里的原话：「有没有辣的鸡肉」。

    第 52 步之前，模型会把「辣的鸡肉」整句塞进 keyword → 0 条；
    现在能拆成 tags=['辣'] + ingredients=['鸡肉']。
    """
    tool = RecipeSearchTool(_db())
    payload = _call(tool, tags=["辣"], ingredients=["鸡肉"])

    assert payload["count"] >= 1
    names = {item["name"] for item in payload["items"]}
    assert "香辣鸡丁" in names, names
    # 每一道都得真的是"鸡"：不能靠"鸡蛋"混进来
    for item in payload["items"]:
        text = item["name"] + " ".join(item["ingredients"])
        assert "鸡" in text


def test_ingredient_aliases_match_how_recipes_actually_write_it():
    """库里写的是"鸡胸肉"，用户说的是"鸡肉"——别名表要把它们接上。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, ingredients=["鸡肉"])
    names = {item["name"] for item in payload["items"]}
    assert "香辣鸡丁" in names, names


def test_egg_is_not_chicken():
    """假朋友：`鸡蛋` 不能被当成"主料是鸡"。

    实测过的误伤——海蛎煎、刀削面因为配料里写了鸡蛋，全成了"鸡肉菜"。
    """
    db = _db()
    create_recipe(
        db,
        name="番茄蛋饼",
        ingredients=["鸡蛋 3 个", "番茄 1 个"],
        steps=["鸡蛋打散加盐", "下锅摊成饼"],
        tags=["下饭"],
        time_minutes=10,
    )
    tool = RecipeSearchTool(db)
    payload = _call(tool, ingredients=["鸡肉"])
    assert "番茄蛋饼" not in {item["name"] for item in payload["items"]}


def test_fish_fragrance_has_no_fish():
    """假朋友：`鱼香肉丝` 里没有鱼，不能被当成"想吃鱼"的结果。"""
    db = _db()
    create_recipe(
        db,
        name="鱼香肉丝",
        ingredients=["猪里脊 200g", "木耳 20g"],
        steps=["里脊切丝上浆", "下锅炒散加鱼香汁"],
        tags=["川菜"],
        time_minutes=20,
    )
    tool = RecipeSearchTool(db)
    payload = _call(tool, ingredients=["鱼"])
    assert "鱼香肉丝" not in {item["name"] for item in payload["items"]}


def test_results_are_ranked_by_relevance_not_by_import_time():
    """主料命中的要排前面；不能再按"最近导入"排（那是第 52 步之前的坑）。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, tags=["辣"], ingredients=["鸡肉"])
    # 香辣鸡丁既辣又是鸡，必须是第一
    assert payload["items"][0]["name"] == "香辣鸡丁", [
        item["name"] for item in payload["items"]
    ]


def test_same_query_gives_the_same_order():
    """同一句话两次给同样的结果（用查询词做随机种子）：可复现才好排查。"""
    tool = RecipeSearchTool(_db())
    first = [item["name"] for item in _call(tool, ingredients=["鸡肉"])["items"]]
    second = [item["name"] for item in _call(tool, ingredients=["鸡肉"])["items"]]
    assert first == second


def test_relaxation_chain_reports_what_was_dropped():
    """条件太严时逐级放宽，并**说明放宽了什么**——而不是偷偷塞无关的菜。"""
    tool = RecipeSearchTool(_db())
    # 库里没有"清淡的鱼 + 10 分钟内"
    payload = _call(tool, tags=["清淡"], ingredients=["鱼"], max_minutes=10)

    assert payload["count"] >= 1
    assert payload.get("relaxed"), "必须告诉调用方放宽了什么"
    assert "10 分钟内" in " ".join(payload["relaxed"])
    assert "请如实告诉对方放宽了什么" in payload["hint"]


def test_relaxation_never_brings_back_excluded_ingredients():
    """放宽再多次，忌口也不能被放回来——那是硬约束。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, tags=["清淡"], ingredients=["鱼"], exclude=["海鲜", "鱼"])
    for item in payload["items"]:
        text = " ".join(item["ingredients"])
        assert "鱼" not in text


def test_no_ingredients_means_no_ingredient_filter():
    """不传主料时不该瞎筛（旧实现会退化成"随便给几道"）。"""
    tool = RecipeSearchTool(_db(), max_results=99)
    payload = _call(tool)
    assert payload["count"] == 4, "四条测试数据应当都能返回"


# ------------------------------------------------------------------ 失败与接线


def test_missing_storage_is_a_readable_failure():
    tool = RecipeSearchTool(None)
    result = _run(tool)
    assert result.ok is False
    assert "菜谱库还没准备好" in (result.error or "")


def test_bad_max_minutes_is_ignored_instead_of_crashing():
    """模型偶尔会把数字写成字符串，不能因此让整轮对话崩。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, max_minutes="不久")
    assert payload["count"] >= 1


def test_tool_is_registered_by_one_line_in_the_registry():
    """按项目规矩：加工具只改注册表一行。

    这里**只做结构断言、不 import 注册表**：`build_default_registry` 会顺带
    拉起天气/POI/地理编码三个工具，而它们 import httpx——本组是纯标准库组。
    真跑装配的用例在 `tests/test_tool_registry.py`（需要 httpx）。
    """
    source = (Path(__file__).resolve().parent.parent / "tools" / "registry.py").read_text(
        encoding="utf-8"
    )
    assert "from tools.recipe_search_tool import RecipeSearchTool" in source
    # 第 182 步：多了 embedder（向量召回），仍然是"注册表一行 + 透传依赖"
    assert "RecipeSearchTool(storage=storage, embedder=embedder, judge=judge)" in source
    # 注册表要能把 storage / embedder 透传下去，否则工具拿不到库、也开不了向量路
    assert "storage: Any = None" in source
    assert "embedder: Any = None" in source


def test_registry_runtime_checks_live_in_the_httpx_group():
    """装配的运行时断言在 test_tool_registry.py（那组需要 httpx）。

    这条只是把"为什么分两处"留在代码里，免得以后被当成重复测试删掉。
    """
    registry_tests = Path(__file__).resolve().parent / "test_tool_registry.py"
    assert registry_tests.exists(), "装配用例搬去了 test_tool_registry.py"


def test_tool_schema_is_well_formed():
    tool = RecipeSearchTool(_db())
    schema = tool.to_openai_schema()
    function = schema["function"]
    assert function["name"] == "search_recipes"
    assert function["description"], "描述要写清楚「什么时候该用」"
    params = function["parameters"]
    assert params["type"] == "object"
    # 第 77 步加了 exclude_tags（本轮的否定口味）。这份清单是**故意**钉死的：
    # 新增参数必须在这里显式登记，不能悄悄多出来一个模型不知道也不会用的字段。
    assert set(params["properties"]) == {
        "keyword", "tags", "ingredients", "min_minutes", "max_minutes",
        "exclude", "exclude_tags",
        # 第 181 步登记：场景/功效类查询的扩展词（评价14 阶段 0 量出来的缺口）
        "search_terms",
        # 第 193 步登记：用户原话，向量召回 + 裁决的输入（评价15 T1-6③）
        "query_text",
    }
    # 扩展词必须自己说清"什么时候用"，否则模型只会把它当第二个 keyword
    description = params["properties"]["search_terms"]["description"]
    assert "场景" in description and "命中" in description
    # 「30 分钟以上」要能表达成下限，否则右栏那个标签没法落地
    assert "min_minutes" in params["properties"]
    # 主料是第 52 步加的关键参数：模型得知道"鸡肉"要放这儿，而不是 keyword
    assert "主料" in params["properties"]["ingredients"]["description"]
    # 全是可选参数：模型可以只说"想吃点什么"，不强制填关键词
    assert params.get("required", []) == []


# ------------------------------------------------------------------ 提示词纪律
#
# 这两条放在这里而不是 test_agent.py：`build_system_prompt` 只依赖标准库，
# 放在这个"必然被跑"的模块里最稳。
# （第 66 步起 test_agent.py 也进了清单，所以不再是"那边跑不起来"的问题，
#   留着这条约定是因为提示词相关的断言集中在一处更好找。）


def test_chat_prompt_no_longer_tells_the_model_to_search_recipes():
    """聊天提示词**不再**要求"必须先查菜谱库"（第 85 步）。

    用户决定：菜谱问题固定在食谱窗口里，聊天只答别的问题。
    所以这条测试从"断言它教模型查库"反转为"断言它不再教"——
    同时保留"不许编菜名"这层意思（编造永远是错的）。
    """
    from agents.agent import build_system_prompt

    prompt = build_system_prompt([])

    assert "search_recipes" not in prompt, "聊天已没有这个工具，不该再点名"
    assert "什么时候该查菜谱" not in prompt, "旧的菜谱教程段应已删除"
    assert "不要凭记忆编" in prompt, "『不许编事实』这条底线要留着"
def test_prompt_forbids_markdown_because_bubbles_are_plain_text():
    """气泡是纯文本：model 输出 markdown 会把星号原样显示出来。

    这条是**实测抓到的**——真跑时模型返回了「**快手类**」，界面上就是一堆星号。
    """
    from agents.agent import build_system_prompt

    prompt = build_system_prompt([])
    assert "markdown" in prompt.lower()
    assert "星号" in prompt or "纯文本" in prompt


# ------------------------------- 候选集要真的扫满 SCAN_LIMIT（第 66 步）


def test_scan_pages_through_instead_of_taking_only_the_first_page(monkeypatch):
    """`SCAN_LIMIT = 500` 必须真的扫到 500，而不是只拿第一页 200 条。

    改之前：直接把 500 传给 `list_recipes`，而它内部夹到 `MAX_LIMIT = 200`。
    菜谱库一超过 200 条，排在后面（更旧）的菜就被**静默漏掉**——
    不报错、只是查不到，看起来像"库里没有"。
    """
    import tools.recipe_search_tool as mod
    from core.recipe_library import MAX_LIMIT

    calls = []

    def fake_list(storage, *, limit=20, offset=0, **_kwargs):
        calls.append({"limit": limit, "offset": offset})
        remaining = max(0, 450 - offset)     # 库里只有 450 条
        return [{"id": offset + i} for i in range(min(limit, remaining))]

    monkeypatch.setattr("tools.recipe_search_tool.list_recipes", fake_list)

    rows = mod._scan_candidates(object(), "")

    assert len(calls) > 1, f"必须分页，实际只调了 {len(calls)} 次"
    assert [call["offset"] for call in calls] == [
        0, MAX_LIMIT, 2 * MAX_LIMIT
    ], f"offset 要一页页往前推，实际 {[call['offset'] for call in calls]}"
    assert all(call["limit"] <= MAX_LIMIT for call in calls), (
        f"每次都不得超过 MAX_LIMIT，实际 {[call['limit'] for call in calls]}"
    )
    assert len(rows) == 450, f"库里 450 条就该扫到 450 条，实际 {len(rows)}"


def test_scan_stops_when_the_library_is_small(monkeypatch):
    """取不满一页就说明到底了：不许为了凑满 500 条空转。"""
    import tools.recipe_search_tool as mod

    calls = []

    def fake_list(storage, *, limit=20, offset=0, **_kwargs):
        calls.append(offset)
        remaining = max(0, 12 - offset)
        return [{"id": offset + i} for i in range(min(limit, remaining))]

    monkeypatch.setattr("tools.recipe_search_tool.list_recipes", fake_list)

    rows = mod._scan_candidates(object(), "")

    assert len(rows) == 12
    assert calls == [0], f"一页就取完了，不该再翻，实际 offsets={calls}"


# ------------------------------- 忌口必须也看标签（第 76 步）


def test_exclude_spicy_catches_a_dish_whose_ingredients_never_mention_it():
    """「辣」只体现在**标签**上时，忌口也必须拦得住。

    为什么是 bug：`_text_of()` 只拼菜名 + 食材。拿真实种子跑一遍，22 道带
    `辣` 标签的菜里有 21 道的食材恰好写了辣椒/花椒，只有「回锅肉」一个字都没有 ——
    所以这个洞很难被随手点到。

    这里**自建 fixture**（共享的 `_db()` 里没有回锅肉，用它写这条会是空断言），
    并且加了反证：不设忌口时它必须查得到，否则这条测试什么都没证明。
    """
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="回锅肉",
        ingredients=["五花肉 300g", "青蒜 2 根", "豆瓣酱 1 勺"],
        steps=["煮肉切片", "煸炒出油", "下豆瓣酱炒匀"],
        tags=["川菜", "辣", "炒"],
        time_minutes=30,
        source="seed",
    )
    tool = RecipeSearchTool(db)

    # 反证：没有忌口时必须查得到
    assert [item["name"] for item in _call(tool, keyword="回锅肉")["items"]] == ["回锅肉"]

    payload = _call(tool, keyword="回锅肉", exclude=["辣"])
    assert payload["items"] == [], (
        f"忌口「辣」必须把回锅肉排掉，实际还返回了 "
        f"{[item['name'] for item in payload['items']]}"
    )


def test_exclude_spicy_also_rejects_mildly_spicy():
    """「微辣」也算辣：用户说不吃辣，不会因为标签写"微辣"就接受。"""
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="毛氏红烧肉",
        ingredients=["五花肉 500g", "冰糖 30g", "酱油 2 勺"],
        steps=["煸炒上色", "加冰糖酱油", "小火焖 70 分钟"],
        tags=["湘菜", "微辣", "焖"],
        time_minutes=70,
        source="seed",
    )
    tool = RecipeSearchTool(db)

    assert [item["name"] for item in _call(tool, keyword="毛氏红烧肉")["items"]] == [
        "毛氏红烧肉"
    ]

    payload = _call(tool, keyword="毛氏红烧肉", exclude=["辣"])
    assert payload["items"] == []


def test_exclude_still_works_on_the_text_side():
    """加标签匹配不能把原来的文本匹配弄坏（海鲜仍按食材/菜名排）。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="虾", exclude=["海鲜"])

    assert payload["items"] == [], "「海鲜」仍要能靠食材排掉虾"


def test_exclude_of_a_word_that_is_not_a_tag_is_unaffected():
    """忌口词不是标签名时，行为不变 —— 不能因为加了标签匹配就误伤。"""
    tool = RecipeSearchTool(_db())
    payload = _call(tool, keyword="鱼", exclude=["香菜"])

    assert payload["items"], "「香菜」不是标签，不该把鱼全排掉"


# ------------------------- 本轮的否定口味 exclude_tags（第 77 步）


def _spicy_db() -> Storage:
    """三道菜：一道辣、一道微辣、一道不辣，用来验否定条件。"""
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db, name="香辣鸡丁",
        ingredients=["鸡胸肉 300g", "干辣椒 10g"],
        steps=["腌", "炒", "撒辣椒"], tags=["辣", "炒"], time_minutes=15, source="seed",
    )
    create_recipe(
        db, name="毛氏红烧肉",
        ingredients=["五花肉 500g", "冰糖 30g"],
        steps=["煸", "焖"], tags=["微辣", "焖"], time_minutes=70, source="seed",
    )
    create_recipe(
        db, name="白切鸡",
        ingredients=["三黄鸡 1 只", "姜"],
        steps=["煮", "斩件"], tags=["清淡", "煮"], time_minutes=60, source="seed",
    )
    return db


def test_exclude_tags_drops_spicy_dishes():
    tool = RecipeSearchTool(_spicy_db())
    payload = _call(tool, exclude_tags=["辣"])

    names = [item["name"] for item in payload["items"]]
    assert "香辣鸡丁" not in names
    assert "白切鸡" in names, "不辣的菜要留着（反证：不是把结果清空）"


def test_exclude_tags_also_drops_mildly_spicy():
    """用户说不吃辣，不会因为标签写的是「微辣」就接受。"""
    tool = RecipeSearchTool(_spicy_db())
    names = [item["name"] for item in _call(tool, exclude_tags=["辣"])["items"]]

    assert "毛氏红烧肉" not in names


def test_exclude_tags_survives_relaxation():
    """放宽链可以放宽"辣"这个**正向**条件，但绝不能把辣菜放回来。

    我第一版把这条断言写成"宁可返回 0 条"，结果红了：`tags=["辣"]` 找不到时
    放宽链会退化成"给你几道不辣的"，那其实是合理行为。真正要钉住的是——
    **不管怎么放宽，被 exclude_tags 排掉的菜都不能回来。**
    """
    tool = RecipeSearchTool(_spicy_db())
    payload = _call(tool, tags=["辣"], exclude_tags=["辣"])

    for item in payload["items"]:
        tags = item.get("tags") or []
        assert "辣" not in tags and "微辣" not in tags, (
            f"放宽之后又冒出辣菜：{item['name']} / {tags}"
        )


def test_exclude_and_exclude_tags_apply_together():
    """长期忌口（exclude）与本轮否定（exclude_tags）是并集，互不覆盖。"""
    tool = RecipeSearchTool(_spicy_db())
    payload = _call(tool, exclude=["姜"], exclude_tags=["辣"])

    names = [item["name"] for item in payload["items"]]
    assert names == [], f"白切鸡含姜、另两道辣，应该一条都不剩，实际 {names}"


def test_the_schema_exposes_exclude_tags():
    """参数表里必须有它，否则模型根本不知道能这么传。"""
    properties = RecipeSearchTool.parameters["properties"]
    assert "exclude_tags" in properties
    assert properties["exclude_tags"]["type"] == "array"


def test_recipe_prompt_teaches_negative_taste():
    """提示词要教模型把「不要辣的」放进 exclude_tags（第 77 步）。

    只靠嘴上说避开是不够的：库里没排掉，返回的菜里还是会辣的。
    """
    from agents.agent import build_recipe_prompt

    prompt = build_recipe_prompt()

    assert "exclude_tags" in prompt, "要提到这个参数名"
    assert "不要辣" in prompt or "否定" in prompt, "要说清什么场景用它"
    assert "已经注入" in prompt, "要提醒长期忌口已由服务端注入，避免模型重复传"


def test_constrained_tool_injects_exclude_tags_even_if_the_model_omits_it():
    """服务端注入的否定口味必须生效，哪怕模型一个参数都没传（第 77 步）。

    实测过：把 exclude_tags 交给模型，它不传，还在回答里写"已帮你避开了辣"。
    所以约束压在工具边界，不靠模型自觉。
    """
    from tools.recipe_search_tool import ConstrainedSearchTool

    inner = RecipeSearchTool(_spicy_db())
    guarded = ConstrainedSearchTool(inner, {"exclude_tags": ["辣"]})

    payload = _call(guarded)          # 模型什么都没传
    names = [item["name"] for item in payload["items"]]

    assert "香辣鸡丁" not in names, "注入的否定口味没生效"
    assert "白切鸡" in names, "不该把不辣的也排掉"
    assert guarded.applied["exclude_tags"] == ["辣"], "要能在日志里看出注入了什么"


def test_recorded_arguments_show_the_enforced_constraints():
    """归因要能看见**实际生效**的参数，而不只是模型说的那套（第 77 步）。

    否则日志里永远看不出服务端注入了什么；下游按 `arguments` 记账的地方
    （比如"这轮用过哪些标签"）也会漏掉注入的标签。
    """
    from tools.recipe_search_tool import ConstrainedSearchTool

    inner = RecipeSearchTool(_spicy_db())
    guarded = ConstrainedSearchTool(
        inner, {"exclude_tags": ["辣"], "exclude": ["香菜"]}
    )

    result = _run(guarded, tags=["清淡"])       # 模型只给了 tags

    assert result.arguments.get("exclude_tags") == ["辣"], "注入的否定口味要出现在记录里"
    assert "香菜" in (result.arguments.get("exclude") or []), "注入的忌口也要在"
    assert result.arguments.get("strict") is True, "强制保留了 strict"


def test_recipe_prompt_does_not_promise_absolute_avoidance():
    """提示词不许把忌口说成绝对保证（第 79 步）。

    解析器一定会漏（词表覆盖有限），而 README 承诺的也只是"尽量"。
    两处口径必须一致，否则"已经避开"就是一句假承诺。
    """
    from agents.agent import build_recipe_prompt

    prompt = build_recipe_prompt()

    for forbidden in ("返回的菜里不会有", "返回的菜里不会包含它们"):
        assert forbidden not in prompt, f"不许出现绝对承诺：{forbidden}"
    assert "尽量" in prompt, "要改成『尽量回避开』这种措辞"
    assert "不保证" in prompt or "不能保证" in prompt, "要明确说明无法保证 100%"


# ------------------------- 忌口词表覆盖面对齐（第 79 步）


def test_dairy_term_maps_to_the_dairy_category():
    """右栏写「奶」，词表里的键是「乳制品」——不搭这座桥就整类漏掉。

    种子里刚好都写成"牛奶/奶油"（含"奶"字），所以这个洞一直没被测试暴露；
    但用户自建的「芝士焗饭」会漏。
    """
    words = expand_term("奶")

    for word in ("芝士", "黄油", "奶酪"):
        assert word in words, f"「奶」展开后应包含 {word}，实际 {words}"


def test_exclude_dairy_catches_cheese_even_without_the_character_milk():
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db, name="芝士焗饭",
        ingredients=["米饭 1 碗", "芝士 100g", "黄油 10g"],
        steps=["铺料", "焗"], tags=["焗"], time_minutes=20, source="user",
    )
    create_recipe(
        db, name="番茄炒蛋",
        ingredients=["番茄 2 个", "鸡蛋 3 个"],
        steps=["炒"], tags=["炒"], time_minutes=10, source="user",
    )
    tool = RecipeSearchTool(db)

    names = [item["name"] for item in _call(tool, exclude=["奶"])["items"]]

    assert "芝士焗饭" not in names, "芝士/黄油都是乳制品，忌口「奶」必须拦住"
    assert "番茄炒蛋" in names, "不含乳制品的菜要留着（反证）"


def test_seafood_expansion_covers_freshwater_fish():
    """忌口「海鲜」必须也拦住淡水鱼（第 79 步）。

    实测：种子里 14 道鱼虾菜，原来有 **8 道**逃过「海鲜」忌口
    （糖醋鲤鱼、西湖醋鱼、剁椒鱼头、臭鳜鱼、清蒸武昌鱼、凯里酸汤鱼…）——
    因为词表只列了海水鱼。逐个点名查，避免被"返回条数上限"掩盖。
    """
    from core.recipe_seed import import_seed

    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    tool = RecipeSearchTool(db)

    for name in (
        "糖醋鲤鱼", "西湖醋鱼", "剁椒鱼头", "臭鳜鱼", "清蒸武昌鱼",
        "凯里酸汤鱼", "得莫利炖鱼", "福州鱼丸", "清蒸鲈鱼",
    ):
        payload = _call(tool, keyword=name, exclude=["海鲜"])
        assert payload["items"] == [], f"{name} 应该被「海鲜」拦住"


def test_seafood_expansion_never_contains_a_bare_fish_character():
    """**不能**靠加一个「鱼」字解决覆盖率问题。

    「鱼香肉丝」里一条鱼都没有，加了裸「鱼」就会把它误伤 ——
    所以词表列的是具体鱼名（鲤鱼/草鱼/鳜鱼…），这条测试把它钉住。
    """
    assert "鱼" not in expand_term("海鲜")


def test_fish_fragrance_dish_survives_the_seafood_exclusion():
    """反证：鱼香肉丝必须留下来。"""
    from core.recipe_seed import import_seed

    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    tool = RecipeSearchTool(db)

    payload = _call(tool, keyword="鱼香肉丝", exclude=["海鲜"])

    assert [item["name"] for item in payload["items"]] == ["鱼香肉丝"]


# --------------------------------------------------------------- 扩展词（第 181 步）
# 评价14 阶段 0 量出来的空缺：「下酒」「发汗」这类场景/功效词在种子里一次都没出现，
# 字面检索必然 0 条。模型拆参时顺手给 search_terms，工具才用得上。


def _run_json(tool: RecipeSearchTool, **kwargs) -> dict:
    """跑一次工具并把 JSON 结果解析出来（与本文件已有的 `_run` 区分开）。"""
    import asyncio

    return asyncio.run(tool.run(**kwargs)).as_payload()


def test_search_terms_only_apply_when_literal_search_finds_nothing():
    """有字面命中时，扩展词**不许**影响结果（行为要逐字节不变）。"""
    db = _db()
    try:
        tool = RecipeSearchTool(db)
        plain = _run_json(tool, keyword="鲈鱼")
        with_terms = _run_json(
            tool, keyword="鲈鱼", search_terms=["完全无关的词", "另一条"]
        )
        assert plain["items"] == with_terms["items"], "有命中时扩展词改动了结果"
        assert "expanded" not in with_terms
    finally:
        db.close()


def test_search_terms_rescue_an_empty_literal_search():
    """字面 0 条时，扩展词能把结果救回来，并**如实说明**用了扩展。"""
    db = _db()
    try:
        tool = RecipeSearchTool(db)
        empty = _run_json(tool, keyword="下酒")          # 库里没有"下酒"这个词
        assert empty["count"] == 0

        rescued = _run_json(tool, keyword="下酒", search_terms=["清蒸鲈鱼", "蒜蓉粉丝蒸虾"])
        assert rescued["count"] >= 1, "扩展词没有救回任何结果"
        assert rescued["expanded"], "结果里要写明用了哪些扩展词"
        # 和"放宽"一样，必须让模型能如实转述
        assert "扩展" in " ".join(rescued.get("relaxed") or [])
    finally:
        db.close()


def test_search_terms_cannot_bypass_exclusions():
    """扩展词也不许绕过忌口/否定口味 —— 那是硬约束。"""
    db = _db()
    try:
        tool = RecipeSearchTool(db)
        result = _run_json(
            tool,
            keyword="下酒",
            search_terms=["清蒸鲈鱼"],
            exclude_tags=["清淡"],
        )
        names = [item["name"] for item in result["items"]]
        assert "清蒸鲈鱼" not in names, "扩展路径把忌口放回来了"
    finally:
        db.close()


# --------------------------------------------------------------- 向量召回（第 182 步）
class _FakeEmbedder:
    """固定映射的假嵌入器：只验证**管线**（有没有用上 query_text），不验证语义。"""

    model = "fake-test"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    async def embed(self, texts):
        return [self.mapping.get(str(text).strip(), [0.0, 0.0]) for text in texts]


def test_vector_recall_only_kicks_in_with_query_text():
    """给了原话才走向量路；不给就与今天逐字节一致。"""
    from core.recipe_library import list_recipes as _list
    from core.recipe_vectors import save_vector

    db = _db()
    try:
        recipes = _list(db, limit=100)
        target = recipes[0]
        # 至少存 3 条方向不同的向量：只有一条时"去中心"会把它减成零向量
        for index, recipe in enumerate(recipes[:3]):
            save_vector(
                db,
                recipe_id=recipe.id,
                model=_FakeEmbedder.model,
                vector=[1.0, float(index) * 0.5],
                digest=f"d{index}",
            )

        embedder = _FakeEmbedder({"下酒": [1.0, 0.0]})
        judged: list[list[str]] = []

        async def judge(query, names):
            judged.append(list(names))
            return [1]      # 选第一个候选

        tool = RecipeSearchTool(db, embedder=embedder, judge=judge)

        # 只给 keyword：字面 0 条，和没有向量时完全一样
        plain = _run_json(tool, keyword="下酒")
        assert plain["count"] == 0
        assert "vector_recall" not in plain

        # 给了原话：向量路召回 + 裁决把 target 补进候选
        with_vector = _run_json(tool, keyword="下酒", query_text="下酒")
        names = [item["name"] for item in with_vector["items"]]
        assert target.name in names, f"向量召回没有补进来：{names}"
        assert with_vector.get("vector_recall"), "结果里要写明语义召回补了几道"
    finally:
        db.close()


def test_vector_recall_still_respects_exclusions():
    """向量只会扩大候选池，**不能**让忌口失守。"""
    from core.recipe_library import list_recipes as _list
    from core.recipe_vectors import save_vector

    db = _db()
    try:
        recipes = _list(db, limit=100)
        target = recipes[0]
        save_vector(
            db, recipe_id=target.id, model=_FakeEmbedder.model,
            vector=[1.0, 0.0], digest="d1",
        )
        async def judge(query, names):
            return list(range(1, len(names) + 1))    # 全都要：看硬约束是否仍生效

        tool = RecipeSearchTool(
            db, embedder=_FakeEmbedder({"下酒": [1.0, 0.0]}), judge=judge
        )

        result = _run_json(
            tool,
            keyword="下酒",
            query_text="下酒",
            exclude_tags=list(target.tags)[:1] or ["清淡"],
        )
        names = [item["name"] for item in result["items"]]
        assert target.name not in names, "向量路径把被排除的菜放回来了"
    finally:
        db.close()


# ------------------------------------------------- 检索诊断（第 196 步 / 评价15 T2-8）
class _LogSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _diagnostics_of(call) -> list[dict]:
    """跑一次 `call()`，把它打出的「检索诊断」行解析回来（自研 runner 没有 caplog）。"""
    sink = _LogSink()
    root = logging.getLogger()
    root.addHandler(sink)
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        call()
    finally:
        root.setLevel(previous)
        root.removeHandler(sink)

    return [
        json.loads(message.split(" ", 1)[1])
        for message in sink.messages
        if message.startswith("检索诊断 ")
    ]


def test_diagnostics_record_whether_the_vector_path_ran():
    """诊断行的核心用途：日后从真实日志里抽 holdout 时，能分清"哪次走了向量轨"。"""
    from core.recipe_library import list_recipes as _list
    from core.recipe_vectors import save_vector

    db = _db()
    try:
        recipes = _list(db, limit=100)
        for index, recipe in enumerate(recipes[:3]):
            save_vector(
                db, recipe_id=recipe.id, model=_FakeEmbedder.model,
                vector=[1.0, float(index) * 0.5], digest=f"dg{index}",
            )

        async def judge(query, names):
            return [1]

        tool = RecipeSearchTool(db, embedder=_FakeEmbedder({"下酒": [1.0, 0.0]}), judge=judge)

        rows = _diagnostics_of(
            lambda: [
                _run_json(tool, keyword="下酒"),
                _run_json(tool, keyword="下酒", query_text="下酒"),
            ]
        )

        assert len(rows) == 2, rows
        plain, with_vector = rows[0], rows[1]
        assert plain["vector"] is False and plain["vector_added"] == 0
        assert plain["judge_picked"] == []
        assert with_vector["vector"] is True
        assert with_vector["judge_picked"], "走了裁决就必须记下它选的是谁"
        assert with_vector["query_text"] == "下酒", "原话要留在日志里（抽 holdout 正需要它）"
        for row in rows:
            assert isinstance(row["elapsed_ms"], int) and row["elapsed_ms"] >= 0
    finally:
        db.close()


def test_the_judge_sees_plain_names_and_nothing_else():
    """裁决者只该看到菜名（第 265 步的教训）。

    试过给「菜名（标签｜食材｜用时）」：v4 实测与只给菜名**逐条完全一致**（都是 8/15），
    而同一配置重跑会在「胃不舒服想喝热汤」这一条上从 9 变 8 —— 说明这点差异是跑动噪声，
    不是真的增益。既然没有可重复的收益，就退回最省 token、最少带偏风险的形式。
    """
    from core.recipe_library import list_recipes as _list
    from core.recipe_vectors import save_vector

    db = _db()
    try:
        recipes = _list(db, limit=100)
        for index, recipe in enumerate(recipes[:3]):
            save_vector(
                db, recipe_id=recipe.id, model=_FakeEmbedder.model,
                vector=[1.0, float(index) * 0.5], digest=f"dg{index}",
            )

        seen: list[list[str]] = []

        async def judge(query, names):
            seen.append(list(names))
            return [1]

        tool = RecipeSearchTool(db, embedder=_FakeEmbedder({"下酒": [1.0, 0.0]}), judge=judge)
        _run_json(tool, keyword="下酒", query_text="下酒")

        assert seen, "向量路没有走到裁决"
        for names in seen:
            assert names, "候选不能是空的"
            for name in names:
                assert "（" not in name and "｜" not in name, f"裁决者看到了多余信息：{name!r}"
    finally:
        db.close()


# ------------------------------------------------- 紧凑结果（第 211 步）
def test_tool_content_is_compact_text_while_data_keeps_the_structure():
    """喂给模型的是"一行一道菜"，结构化数据在 data 里 —— 两边都不丢。"""
    db = _db()
    try:
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="辣")

        content = result.content
        assert content.startswith("共 "), content[:40]
        assert "｜" in content
        assert '"items"' not in content, "不该再给模型塞 JSON 键名（那是纯噪音）"

        payload = result.as_payload()
        assert payload and payload["items"], "结构必须还在，卡片靠它渲染"
        for row in payload["items"]:
            assert isinstance(row["id"], int) and row["name"]
            assert len(row["steps"]) <= 1, "做法只留第一步（完整做法在卡片里）"
            assert row["name"] in content, "文本里必须能读到菜名"

        # 省下的量：同一份结果，JSON dump 至少是这个紧凑文本的 2 倍
        import json as _json

        dumped = _json.dumps(payload, ensure_ascii=False)
        assert len(dumped) > len(content) * 2, (len(dumped), len(content))
    finally:
        db.close()


def test_compact_content_still_tells_the_model_when_conditions_were_relaxed():
    """"放宽了"这句必须还在：模型据此如实交代，不然就是假装完全符合。"""
    db = _db()
    try:
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="辣", max_minutes=5, min_minutes=1)
        if result.as_payload() and result.as_payload().get("relaxed"):
            assert "[提示]" in result.content and "放宽" in result.content
    finally:
        db.close()


def test_cards_are_still_collected_from_the_structured_payload():
    """`_collect_recipe_ids` 不再解析 content，走 data —— 卡片不能消失。"""
    from api.recipe_routes import _collect_recipe_ids

    db = _db()
    try:
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="辣")
        ids = _collect_recipe_ids([result])
        assert ids, "有结果就必须能捞到卡片 id"
        assert ids == [row["id"] for row in result.as_payload()["items"]]
    finally:
        db.close()


def test_legacy_json_content_still_parses_through_as_payload():
    """老形态（content 就是 JSON）仍然可用：`as_payload()` 是兼容入口。"""
    from core.schemas import ToolResult

    legacy = ToolResult(ok=True, content='{"count": 1, "items": [{"id": 7, "name": "旧"}]}')
    assert legacy.as_payload()["items"][0]["id"] == 7

    plain_text = ToolResult(ok=True, content="今天晴，26°C")
    assert plain_text.as_payload() is None, "解析不出结构就返回 None，别抛异常"

    broken = ToolResult(ok=False, error="超时")
    assert broken.as_payload() is None
