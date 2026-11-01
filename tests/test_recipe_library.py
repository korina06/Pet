"""本地菜谱知识库：增删改查往返、撞名去重、过滤分页、FTS 与 LIKE 两条路径、别名命中。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core.recipe_library import (  # noqa: E402
    MAX_ITEM_LENGTH,
    MAX_ITEMS,
    SOURCES,
    DuplicateRecipe,
    count_recipes,
    count_search,
    add_aversions,
    create_recipe,
    delete_recipe,
    ensure_schema,
    expand_terms,
    get_recipe,
    list_aversions,
    list_recipes,
    set_aversions,
    normalize_name_key,
    search_recipes,
    update_recipe,
)
from core.storage import Storage  # noqa: E402


def _storage(*, fts: bool = True) -> Storage:
    """内存库 + schema 就位；`fts=False` 模拟"环境没有 FTS5"。"""
    db = Storage(":memory:")
    if not fts:
        db.fts_available = False
    ensure_schema(db)
    return db


# ------------------------------------------------------------------ 往返与清洗


def test_create_and_get_roundtrip_with_chinese_and_json():
    """中文、换行、引号、emoji 进 JSON 出的往返保真。"""
    db = _storage()
    recipe = create_recipe(
        db,
        name="番茄牛腩",
        ingredients=["牛腩 500g", "西红柿 2 个", "番茄酱 1 勺"],
        steps=["牛腩冷水下锅焯水\n撇去浮沫", '加入「番茄」慢炖 40 分钟', "撒上香菜 🍅"],
        tags=["炖菜", "周末"],
        time_minutes=90,
        source="user",
        note="奶奶的做法",
    )
    assert recipe.id > 0

    again = get_recipe(db, recipe.id)
    assert again == recipe                                   # 全字段往返
    assert again.ingredients[1] == "西红柿 2 个"
    assert "\n" in again.steps[0]
    assert again.tags == ("炖菜", "周末")

    payload = recipe.to_dict()
    assert payload["ingredients"] == ["牛腩 500g", "西红柿 2 个", "番茄酱 1 勺"]
    assert isinstance(payload["tags"], list)                 # 接口层可直接 json 序列化


def test_array_fields_are_cleaned_and_clamped():
    db = _storage()
    recipe = create_recipe(
        db,
        name="大杂烩",
        ingredients=["  牛肉  ", "", "   ", "牛肉", "牛" * 400],
        steps=[],
        tags=None,
    )
    assert recipe.ingredients == ("牛肉", "牛" * MAX_ITEM_LENGTH)   # 去空/去重/截断
    assert recipe.steps == ()
    assert recipe.tags == ()

    many = create_recipe(db, name="很多料", ingredients=[f"料{i}" for i in range(80)])
    assert len(many.ingredients) == MAX_ITEMS

    # 裸字符串会被逐字拆开，是个安静的灾难：直接拒绝（接口层负责拆行）
    with pytest.raises(TypeError):
        create_recipe(db, name="错误传参", steps="第一步\n第二步")


def test_normalize_name_key_collapses_space_width_and_case():
    assert normalize_name_key(" 番茄  炒蛋 ") == "番茄炒蛋"
    assert normalize_name_key("Ｃａｅｓａｒ Ｓａｌａｄ") == "caesarsalad"
    assert normalize_name_key("T bone") == normalize_name_key("tbone")


def test_invalid_input_raises_clear_errors():
    db = _storage()
    with pytest.raises(ValueError):
        create_recipe(db, name="   ")
    with pytest.raises(ValueError):
        create_recipe(db, name="x", source="magic")
    with pytest.raises(ValueError):
        create_recipe(db, name="y", time_minutes=0)
    with pytest.raises(ValueError):
        create_recipe(db, name="z", time_minutes="很久")
    # 来源词表：这里**不写死整份元组**（第 48 步加 "seed" 时它红了一次，
    # 而那次改动本身是对的）。改为钉住"每个来源都能入库、瞎写的会被拒"，
    # 这样将来再加一个合法来源不必来改这条断言。
    assert {"user", "parsed", "generated", "web", "seed"} <= set(SOURCES)
    for index, source in enumerate(SOURCES):
        created = create_recipe(db, name=f"来源测试{index}", source=source)
        assert created.source == source
    with pytest.raises(ValueError):
        create_recipe(db, name="来源测试-非法", source="magic")


# ------------------------------------------------------------------ 去重


def test_duplicate_names_are_rejected_across_write_variants():
    db = _storage()
    first = create_recipe(db, name="西红柿炒蛋")

    with pytest.raises(DuplicateRecipe) as excinfo:
        create_recipe(db, name=" 西红柿 炒蛋 ")              # 只差空白 = 同一道菜
    assert excinfo.value.existing_id == first.id
    assert count_recipes(db) == 1


def test_rename_onto_existing_name_raises_duplicate():
    db = _storage()
    create_recipe(db, name="红烧肉")
    other = create_recipe(db, name="糖醋排骨")

    with pytest.raises(DuplicateRecipe) as excinfo:
        update_recipe(db, other.id, name="红烧肉")
    assert excinfo.value.existing_id is not None
    assert get_recipe(db, other.id).name == "糖醋排骨"       # 冲突失败后原记录不动


# ------------------------------------------------------------------ 更新与删除


def test_update_bumps_updated_at_and_preserves_untouched_fields():
    db = _storage()
    recipe = create_recipe(db, name="清炒时蔬", ingredients=["青菜"], source="parsed")

    updated = update_recipe(db, recipe.id, ingredients=["青菜", "蒜"], tags=["下饭"])
    assert updated.created_at == recipe.created_at
    assert updated.updated_at != recipe.updated_at            # 写入会推新时间戳
    assert updated.name == "清炒时蔬"                          # 没传的字段保持原样
    assert updated.source == "parsed"
    assert updated.ingredients == ("青菜", "蒜")
    assert updated.tags == ("下饭",)


def test_update_can_clear_time_and_rename():
    db = _storage()
    recipe = create_recipe(db, name="蒸蛋", time_minutes=10)

    renamed = update_recipe(db, recipe.id, name="水蒸蛋", time_minutes=None)
    assert renamed.name == "水蒸蛋"
    assert renamed.time_minutes is None                       # None = 清掉，不是"没传"
    assert get_recipe(db, recipe.id).name == "水蒸蛋"


def test_update_and_delete_missing_are_quiet():
    db = _storage()
    assert update_recipe(db, 999, name="x") is None
    assert delete_recipe(db, 999) is False

    recipe = create_recipe(db, name="临时菜")
    assert delete_recipe(db, recipe.id) is True
    assert get_recipe(db, recipe.id) is None


# ------------------------------------------------------------------ 列表与分页


def test_list_filters_pagination_and_count():
    db = _storage()
    for i in range(5):
        create_recipe(
            db,
            name=f"菜{i}",
            tags=["家常"] if i < 3 else ["宴客"],
            source="user" if i < 2 else "parsed",
        )
    assert count_recipes(db) == 5
    assert count_recipes(db, source="user") == 2
    assert count_recipes(db, tag="家常") == 3

    page1 = list_recipes(db, limit=2)
    page2 = list_recipes(db, limit=2, offset=2)
    assert [r.name for r in page1] == ["菜4", "菜3"]          # 最近更新在前
    assert [r.name for r in page2] == ["菜2", "菜1"]          # 分页不重不漏
    assert {r.name for r in list_recipes(db, tag="宴客")} == {"菜3", "菜4"}
    assert {r.name for r in list_recipes(db, source="user")} == {"菜0", "菜1"}


# ------------------------------------------------------------------ 检索


def test_search_hits_name_ingredients_and_tags():
    db = _storage()
    create_recipe(db, name="蒜蓉西兰花", ingredients=["西兰花", "蒜"], tags=["素食"])
    create_recipe(db, name="糖醋排骨", ingredients=["排骨"], tags=["硬菜"])

    assert [r.name for r in search_recipes(db, "西兰花")] == ["蒜蓉西兰花"]
    assert [r.name for r in search_recipes(db, "排骨")] == ["糖醋排骨"]
    assert [r.name for r in search_recipes(db, "素食")] == ["蒜蓉西兰花"]
    assert search_recipes(db, "") == []
    assert search_recipes(db, "不存在的菜") == []


def test_alias_expansion_finds_variant_ingredient():
    db = _storage()
    create_recipe(db, name="西红柿炒蛋", ingredients=["西红柿", "鸡蛋"])
    create_recipe(db, name="土豆炖牛肉")

    # 搜"番茄"要命中用"西红柿"写的菜——这就是别名表的意义
    assert [r.name for r in search_recipes(db, "番茄")] == ["西红柿炒蛋"]
    # 反向（主名 → 别名）也成立
    assert [r.name for r in search_recipes(db, "西红柿")] == ["西红柿炒蛋"]
    # 多词 = AND：两个词都要在
    assert [r.name for r in search_recipes(db, "番茄 鸡蛋")] == ["西红柿炒蛋"]
    assert search_recipes(db, "番茄 土豆") == []


def test_expand_terms_groups_alias_and_canonical():
    db = _storage()
    assert set(expand_terms(db, "番茄")) == {"番茄", "西红柿"}
    assert set(expand_terms(db, "土豆")) == {"土豆", "马铃薯", "洋芋"}
    assert expand_terms(db, "从没登记过的词") == ("从没登记过的词",)


def test_search_still_works_without_fts5():
    """没有 FTS5 的环境：写入照常，检索退回 LIKE，别名展开照样命中。"""
    db = _storage(fts=False)
    assert db.fts_available is False
    create_recipe(db, name="西红柿炒蛋", ingredients=["西红柿", "鸡蛋"])

    assert [r.name for r in search_recipes(db, "番茄")] == ["西红柿炒蛋"]
    assert [r.name for r in search_recipes(db, "鸡蛋")] == ["西红柿炒蛋"]

    create_recipe(db, name="清炒菠菜")
    assert count_recipes(db) == 2                             # 没有影子表也写得进


def test_search_finds_words_in_the_middle_of_a_cjk_name():
    """回归（第 48 步实测到的真实缺陷）："词在中间"也必须搜得到。

    unicode61 把连续 CJK 当成**一个词**，`紫菜蛋花汤`就是一个 token，
    而 FTS 前缀短语只能匹配"以该词开头"，于是旧实现里：

        搜「紫菜」→ 命中（是 token 前缀）
        搜「蛋花」→ **不命中**（词在中间）
        搜「蛋」  → **不命中**

    对搜索框来说"搜不到"就等于坏了。更麻烦的是这条表现**看环境**：
    没有 FTS5 时走 LIKE（`%蛋花%`，没有位置限制）反而能搜到。
    所以现在两条路合并，这里把"中间也能搜到"钉死。
    """
    db = _storage()
    create_recipe(db, name="紫菜蛋花汤", ingredients=["紫菜 5g", "鸡蛋 1 个"])
    create_recipe(db, name="蛋炒饭", ingredients=["米饭 1 碗", "鸡蛋 2 个"])
    create_recipe(db, name="西红柿炒蛋", ingredients=["西红柿 2 个"])

    assert [r.name for r in search_recipes(db, "蛋花")] == ["紫菜蛋花汤"]
    assert "紫菜蛋花汤" in {r.name for r in search_recipes(db, "蛋")}
    assert count_search(db, "蛋花") == 1
    assert count_search(db, "蛋") >= 2


def test_name_matches_rank_before_ingredient_matches():
    """回归（第 50 步换库后实测到的真实缺陷）：**名字命中要排在配料命中前面**。

    换成《中国菜谱大全》后搜「豆腐」，前 14 位是这样的：

        过桥米线（配料有"豆腐皮"）… 第 14 位才是麻婆豆腐

    原因：FTS 前缀命中排在前，而"词在名字中间"的（麻婆豆腐）只能由 LIKE
    补在**尾部**。对"想吃豆腐"的人来说这是错的——他要的是豆腐菜，
    不是配料里有豆腐皮的面。

    修法是在合并两条路之后按"名字命中 > 其它命中"稳定重排。
    """
    db = _storage()
    create_recipe(db, name="过桥米线", ingredients=["豆腐皮 适量", "米线 200g"])
    create_recipe(db, name="麻婆豆腐", ingredients=["嫩豆腐 400g", "豆瓣 1 勺"])
    create_recipe(db, name="长沙臭豆腐", ingredients=["臭豆腐 8 块"])

    names = [r.name for r in search_recipes(db, "豆腐")]
    # 名字里就有"豆腐"的两道必须排在"配料里有豆腐皮"的前面。
    # 同层内部的先后不钉死（那是"最近更新优先"的既有口径，与本次修复无关）。
    assert set(names[:2]) == {"麻婆豆腐", "长沙臭豆腐"}, names
    assert names[2:] == ["过桥米线"], names


def test_fts_and_like_paths_agree_on_recall():
    """两条路的**召回必须一致**（顺序可以不同）：换台机器不该换结果。

    这是上一条缺陷的另一半——旧实现里 FTS 与 LIKE 的召回不同，
    同一个库在"有 FTS5 / 没 FTS5"的机器上会给出不同答案，最难排查。
    """
    def build(fts: bool):
        db = _storage(fts=fts)
        create_recipe(db, name="紫菜蛋花汤", ingredients=["紫菜 5g"])
        create_recipe(db, name="蛋炒饭", ingredients=["米饭 1 碗"])
        create_recipe(db, name="红烧肉", ingredients=["五花肉 500g"])
        return db

    with_fts, without = build(True), build(False)
    for keyword in ("蛋", "蛋花", "紫菜", "汤", "米饭", "五花肉", "不存在"):
        left = {r.name for r in search_recipes(with_fts, keyword)}
        right = {r.name for r in search_recipes(without, keyword)}
        assert left == right, f"搜「{keyword}」两条路召回不同：FTS={left} LIKE={right}"
        assert count_search(with_fts, keyword) == count_search(without, keyword)


def test_search_pagination_is_stable_and_deduped():
    """合并两条路之后，分页不能出现重复或漏项。"""
    db = _storage()
    for index in range(6):
        create_recipe(db, name=f"蛋菜{index}", ingredients=[f"鸡蛋 {index + 1} 个"])

    seen: list[str] = []
    for offset in range(0, 7, 2):
        seen.extend(r.name for r in search_recipes(db, "蛋", limit=2, offset=offset))
    assert len(seen) == len(set(seen)), f"分页出现重复：{seen}"
    assert len(seen) == 6
    assert count_search(db, "蛋") == 6


def test_enabling_fts_later_rebuilds_index_for_existing_rows():
    """环境先无 FTS5 后有了：已有行必须被补进索引（rebuild），不能搜不到。"""
    db = _storage(fts=False)
    recipe = create_recipe(db, name="西红柿炒蛋", ingredients=["西红柿"])

    db.fts_available = True
    ensure_schema(db)                                         # 相当于下次启动环境恢复

    rows = db.query(
        "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH ?", ('"西红柿"*',)
    )
    assert [row["rowid"] for row in rows] == [recipe.id]      # 老行已进索引
    assert [r.name for r in search_recipes(db, "番茄")] == ["西红柿炒蛋"]


# ------------------------------------------------- 忌口的追加与删除（第 63 步）


def test_add_aversions_keeps_existing_and_orders_new_ones():
    """追加**不许**动已有点亮的忌口——这是 set_aversions 做不到的地方。"""
    db = _storage()
    set_aversions(db, ["香菜", "海鲜"])

    merged = add_aversions(db, ["花生", "芒果"])

    assert merged == ["香菜", "海鲜", "花生", "芒果"], "原有顺序保留，新词追加在后"
    assert list_aversions(db) == merged, "真落库了"


def test_add_aversions_is_idempotent():
    db = _storage()
    set_aversions(db, ["花生"])

    merged = add_aversions(db, ["花生", "花生"])

    assert merged == ["花生"], "重复追加不该产生两条"


def test_add_aversions_cleans_and_drops_empty_terms():
    db = _storage()
    merged = add_aversions(db, ["  花生过敏  ", "", "  "])
    assert merged == ["花生过敏"], "剥空白、丢空项"


def test_set_aversions_still_deletes_everything():
    """删除语义只走 set_aversions（右栏 PUT 整份替换）——这条不能被 add 影响。"""
    db = _storage()
    add_aversions(db, ["香菜", "花生"])

    assert set_aversions(db, ["香菜"]) == ["香菜"], "取消勾选后那条真的没了"
    assert list_aversions(db) == ["香菜"]


def test_add_then_uncheck_in_sidebar_deletes_a_stated_allergen():
    """第 63 步要的完整语义：口语说出来 → 进库 → 右栏取消 → 删掉。"""
    db = _storage()
    # 1) 用户在对话里说"我对花生过敏" → 走 add_aversions
    assert "花生" in add_aversions(db, ["花生"])
    # 2) 他在右栏点亮"香菜"，此时整份提交必须**不丢**花生
    assert set_aversions(db, list_aversions(db) + ["香菜"]) == ["花生", "香菜"]
    # 3) 他取消"花生"（右栏提交的列表里没有它）→ 从长期删除
    assert set_aversions(db, ["香菜"]) == ["香菜"]
    assert "花生" not in list_aversions(db)
