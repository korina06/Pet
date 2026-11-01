"""向量索引的三方法接口：增删查语义，以及默认实现对现有 `search_vectors` 行为不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_vectors import ensure_vector_schema, save_vector, search_vectors  # noqa: E402
from core.storage import Storage  # noqa: E402
from core.vector_index import (  # noqa: E402
    InMemoryVectorIndex,
    VectorIndex,
    build_vector_index,
)

MODEL = "test-embedding"


def test_index_returns_ids_sorted_by_cosine_and_applies_threshold() -> None:
    index = InMemoryVectorIndex({1: [1.0, 0.0], 2: [0.6, 0.8], 3: [0.0, 1.0]})

    assert isinstance(index, VectorIndex)
    hits = index.search([1.0, 0.0], limit=10, min_score=0.0)
    assert [recipe_id for recipe_id, _score in hits] == [1, 2, 3]
    assert hits[0][1] == 1.0

    above = index.search([1.0, 0.0], limit=10, min_score=0.5)
    assert [recipe_id for recipe_id, _score in above] == [1, 2], "低于门槛的不能出现"


def test_index_respects_limit_but_never_returns_zero_items() -> None:
    index = InMemoryVectorIndex({1: [1.0, 0.0], 2: [0.6, 0.8]})

    assert len(index.search([1.0, 0.0], limit=1)) == 1
    # 与 search_vectors 一致：limit 有下限 1，避免调用方传 0 就悄悄拿不到结果
    assert len(index.search([1.0, 0.0], limit=0)) == 1


def test_add_overwrites_and_delete_is_idempotent() -> None:
    index = InMemoryVectorIndex()
    index.add(7, [1.0, 0.0])
    assert index.search([1.0, 0.0], limit=5)[0][0] == 7

    index.add(7, [0.0, 1.0])          # 覆盖：旧向量不该再命中
    assert index.search([1.0, 0.0], limit=5, min_score=0.9) == []
    assert index.search([0.0, 1.0], limit=5)[0][0] == 7

    index.delete(7)
    index.delete(7)                    # 幂等
    assert len(index) == 0


def test_build_index_reads_the_storage_and_matches_search_vectors() -> None:
    storage = Storage.memory()
    try:
        ensure_vector_schema(storage)
        save_vector(storage, recipe_id=1, model=MODEL, vector=[1.0, 0.0], digest="a")
        save_vector(storage, recipe_id=2, model=MODEL, vector=[0.6, 0.8], digest="b")
        save_vector(storage, recipe_id=3, model="other", vector=[0.0, 1.0], digest="c")

        index = build_vector_index(storage, MODEL)
        assert len(index) == 2, "只装当前模型的向量"

        query = [1.0, 0.0]
        from_index = [recipe_id for recipe_id, _ in index.search(query, limit=5)]
        from_old = search_vectors(query, [(1, [1.0, 0.0]), (2, [0.6, 0.8])], limit=5)
        assert from_index == from_old == [1, 2]
    finally:
        storage.close()
