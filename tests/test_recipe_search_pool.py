"""检索候选池的三条边界：零命中必须回空、改写词能召回、空关键词不许把整库当候选。
"""

from __future__ import annotations

import asyncio

from core.recipe_library import ensure_schema, list_recipes
from core.recipe_seed import import_seed
from core.storage import Storage
from tools.recipe_search_tool import RecipeSearchTool


def _seeded() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    return db


def _run(tool: RecipeSearchTool, **kwargs) -> dict:
    return asyncio.run(tool.run(**kwargs)).as_payload()


def test_true_zero_hit_returns_nothing() -> None:
    """库里确实没有的菜 → 空结果，且**不硬凑**（不许回退成"整库随便挑"）。

    「寿司」在 holdout v3 里是零命中用例之一（第 189 步实测返回过兰州牛肉面）。
    """
    db = _seeded()
    try:
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="寿司和刺身")

        assert result["count"] == 0, f"硬凑了 {result['count']} 条：{result['items'][:5]}"
        assert result["items"] == []
        assert not result.get("expanded"), "没搜到的时候不该谎称用了扩展词"
    finally:
        db.close()


def test_rewritten_terms_still_recall() -> None:
    """模型把「热乎乎的辣汤」改写成 `search_terms=["发汗"]` → 照样召回。

    这就是"改写"这一路存在的理由：用户原话里未必有库里的词。
    """
    db = _seeded()
    try:
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="发汗", search_terms=["酸辣", "胡辣汤"])

        assert result["count"] > 0, "改写词一条都没召回"
        assert result.get("expanded"), "结果里要写清用了哪些扩展词"
        assert result["count"] <= 6, "候选池不该因为扩展就无限膨胀"
    finally:
        db.close()


def test_empty_keyword_falls_back_to_whole_library() -> None:
    """空 `keyword` = 整库候选（现状，钉死，防回归）。

    这个行为本身**不是 bug**（真实模型总会给 keyword 或 tags），但它是
    第 189 步那次评测事故的根因，所以必须由测试显式记录下来：
    空 keyword 时 `count` 应当等于库里的菜数，而不是 0。
    """
    db = _seeded()
    try:
        total = len(list_recipes(db))
        tool = RecipeSearchTool(db)
        result = _run(tool, keyword="")

        assert total >= 50, f"种子库太小了（{total} 道），这条测试失去意义"
        assert result["count"] == min(total, 6), (
            f"空 keyword 应当把整库当候选（工具默认只回 6 条），实际 {result['count']}"
        )
    finally:
        db.close()


def test_schema_exposes_query_text() -> None:
    """`query_text` 必须出现在**给模型看的 schema** 里（第 193 步 / 评价15 T1-6③）。

    第 188 步建了向量召回，但 `query_text` 只活在 `run()` 签名里、schema 里没有 ——
    模型根本传不上来，于是那条路在真实对话里一次都没被触发过，只有测试在直接调它。
    真模型实测（同一批 5 句）：schema 补上之前 **0/5** 带原话，补上之后 **5/5** 带。

    这条测试同时钉住两件事：schema 里有它（模型才看得到），签名里有它（传了才不丢）。
    """
    import inspect

    properties = RecipeSearchTool.parameters["properties"]
    assert "query_text" in properties, "schema 缺 query_text → 模型无从传原话，向量路等于死路"
    assert "原话" in properties["query_text"]["description"], "描述里要明确「照抄原话」"

    signature = inspect.signature(RecipeSearchTool.run).parameters
    assert "query_text" in signature, "签名缺 query_text → 模型传了也会被丢掉"


def test_every_schema_property_is_accepted_by_run() -> None:
    """schema 里的每个参数名，`run()` 都必须能接（第 193 步）。

    防的是反过来的错：schema 里写了、签名里没有 —— 模型传了直接 TypeError 或静默丢弃。
    这条是上一条的推广：以后加参数忘了改签名，这里立刻红。
    """
    import inspect

    signature = set(inspect.signature(RecipeSearchTool.run).parameters)
    schema_keys = set(RecipeSearchTool.parameters["properties"])
    missing = schema_keys - signature
    assert not missing, f"schema 里有、run() 里没有：{sorted(missing)}"
