"""场景词表：每一项的词都必须真能在种子库里命中，否则就是永远空转的分支。
"""

from __future__ import annotations

import asyncio

from core.recipe_library import ensure_schema, search_recipes
from core.recipe_scene_terms import MAX_SCENE_TERMS, SCENE_TERMS, expand_scene_terms
from core.recipe_seed import import_seed
from core.storage import Storage
from tools.recipe_search_tool import RecipeSearchTool


def _seeded() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    return db


def test_every_scene_term_hits_at_least_one_recipe():
    """表里的每个词都必须在库里命中 —— 这是这张表存在的意义。"""
    db = _seeded()
    try:
        missing: list[str] = []
        for phrases, terms in SCENE_TERMS.items():
            for term in terms:
                if not search_recipes(db, term, limit=1):
                    missing.append(f"{'/'.join(phrases)} → {term}")
        assert not missing, f"这些词在库里一条都搜不到：{missing}"
    finally:
        db.close()


def test_longest_phrase_wins():
    """「牙口不好想吃软的」同时含两个键，应命中最长的那个。"""
    assert expand_scene_terms("牙口不好，想吃点软的") == list(
        SCENE_TERMS[("牙口不好", "咬不动", "软的", "软一点", "软烂")]
    )
    # 完全不相干的话不该被扩展
    assert expand_scene_terms("今天天气怎么样") == []


def test_expansion_is_capped():
    """扩展词有上限，避免一次塞太多把候选池稀释。"""
    for terms in SCENE_TERMS.values():
        assert len(terms) <= MAX_SCENE_TERMS
    assert len(expand_scene_terms("下酒")) <= MAX_SCENE_TERMS


def test_tool_uses_the_scene_table_when_literal_search_misses():
    """字面 0 条 + 模型没给扩展词 → 用词表兜，并如实说明。"""
    db = _seeded()
    try:
        tool = RecipeSearchTool(db)
        result = asyncio.run(tool.run(keyword="有什么下酒的")).as_payload()

        assert result["count"] > 0, "词表没有兜住这条查询"
        assert result.get("expanded"), "结果里要写明用了哪些扩展词"
        assert any("场景词表" in note for note in result.get("relaxed") or [])

        names = [item["name"] for item in result["items"]]
        assert set(names) & {"盐水鸭", "夫妻肺片", "白切鸡", "大煮干丝"}, (
            f"扩展后应当出现下酒菜，实际 {names}"
        )
    finally:
        db.close()


def test_scene_expansion_cannot_bypass_exclusions():
    """词表也只是扩召回，硬约束照旧（这里是"不要辣"）。"""
    db = _seeded()
    try:
        tool = RecipeSearchTool(db)
        result = asyncio.run(tool.run(keyword="吃点能发汗的", exclude_tags=["辣"])).as_payload()
        for item in result["items"]:
            assert not any("辣" in tag for tag in item["tags"]), (
                f"被排除的辣味菜又出现了：{item['name']}"
            )
    finally:
        db.close()
