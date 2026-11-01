"""向量存储与融合：余弦、RRF、指纹、存取。全是纯函数，断言可以写得很硬。
"""

from __future__ import annotations

from core.recipe_library import ensure_schema
from core.recipe_vectors import (
    content_hash,
    cosine,
    ensure_vector_schema,
    load_vectors,
    rrf_fuse,
    save_vector,
    search_vectors,
)
from core.storage import Storage


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 1.0]) > 0.7
    # 维度不一致不许猜：直接 0（补零会编出一个假相似度）
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    assert cosine([], []) == 0.0


def test_search_vectors_orders_by_similarity_then_id():
    vectors = [(7, [1.0, 0.0]), (3, [0.9, 0.1]), (5, [0.0, 1.0])]
    assert search_vectors([1.0, 0.0], vectors, limit=2) == [7, 3]
    # 同分时按 id 升序，保证结果可复现
    same = [(9, [1.0, 0.0]), (4, [1.0, 0.0])]
    assert search_vectors([1.0, 0.0], same, limit=2) == [4, 9]


def test_rrf_puts_the_item_found_by_both_paths_first():
    """两路都召回的应该排第一 —— 这正是融合的意义。"""
    literal = [10, 20, 30]
    vector = [40, 20, 50]

    fused = rrf_fuse([literal, vector])
    assert fused[0] == 20, f"两路都命中的 20 应当排第一，实际 {fused}"

    # 只被一路召回的顺序保持：字面在前（同分时按 RRF 分数，其次 id）
    assert set(fused) == {10, 20, 30, 40, 50}


def test_vectors_round_trip_through_sqlite():
    storage = Storage(":memory:")
    try:
        ensure_schema(storage)      # 建表（含向量表）
        ensure_vector_schema(storage)   # 幂等，重复调用无害

        save_vector(storage, recipe_id=1, model="m", vector=[0.25, 0.5], digest="d1")
        save_vector(storage, recipe_id=2, model="other", vector=[1.0], digest="d2")
        # 同一个 (recipe, model) 再写一次应当覆盖，而不是插两行
        save_vector(storage, recipe_id=1, model="m", vector=[0.5, 0.5], digest="d3")

        loaded = load_vectors(storage, "m")
        assert [recipe_id for recipe_id, _vec in loaded] == [1]
        assert loaded[0][1] == [0.5, 0.5], "同主键写入应当覆盖"

        # 不同模型的向量互不可见（换模型后旧向量自动不参与）
        assert load_vectors(storage, "other")[0][0] == 2
    finally:
        storage.close()


def test_content_hash_is_stable_and_sensitive():
    digest = content_hash("番茄炒蛋 清淡")
    assert digest == content_hash("番茄炒蛋 清淡")
    assert digest != content_hash("番茄炒蛋 微辣")
    assert len(digest) == 32
