"""菜谱向量的存取与融合：按模型分别存进 SQLite，查询只与同模型向量比较。

不引向量库，纯 Python 暴力余弦；召回结果用 RRF 与字面检索融合。
"""

from __future__ import annotations

import array
import hashlib
import json
import math
from collections.abc import Sequence

from core.storage import Storage

#: RRF 的平滑常数（论文里的 60）。它把"排名"压成温和的分数，避免个别极值主导。
RRF_K = 60

#: 一条菜谱的嵌入文本用哪些字段、各截多少字。
#: 为什么带上做法前若干字：用户可能问"祛湿""发汗"这类**只在做法里**出现的语义。
EMBED_STEPS_CHARS = 120


def ensure_vector_schema(storage: Storage) -> None:
    """建向量表。与 `recipes_fts` 同款：不存在就建，已存在就跳过。"""
    storage.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_vectors (
            recipe_id    INTEGER NOT NULL,
            model        TEXT    NOT NULL,
            dim          INTEGER NOT NULL,
            vec          BLOB    NOT NULL,
            content_hash TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (recipe_id, model)
        )
        """
    )


def embed_text(recipe: object) -> str:
    """一条菜谱 → 用于嵌入的文本。

    顺序有讲究：**菜名 + 标签 + 主料**在前（它们最概括），做法正文截前 120 字垫后。
    不切 chunk：一道菜 <512 token，切开只会把"番茄炒蛋的步骤二"检索成孤儿。
    """
    name = str(getattr(recipe, "name", "") or "")
    tags = " ".join(getattr(recipe, "tags", []) or [])
    try:
        ingredients = json.loads(getattr(recipe, "ingredients_json", "") or "[]")
    except (TypeError, ValueError):
        ingredients = []
    if not isinstance(ingredients, list):
        ingredients = []
    ingredient_words = []
    for item in ingredients:
        text = str(item or "").strip()
        if not text:
            continue
        # "鲈鱼 1 条（约 500g）" → 取前面的名词部分，去掉数量
        ingredient_words.append(text.split(" ")[0] if " " in text else text)
    steps = getattr(recipe, "steps", None) or []
    step_text = " ".join(str(line) for line in steps)[:EMBED_STEPS_CHARS]
    parts = [name, tags, " ".join(ingredient_words), step_text]
    return " ".join(part for part in parts if part).strip()


def content_hash(text: str) -> str:
    """嵌入文本的指纹：文本没变就不重算向量（批建脚本靠它做幂等）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _pack(vector: Sequence[float]) -> bytes:
    return array.array("f", [float(value) for value in vector]).tobytes()


def _unpack(blob: bytes) -> list[float]:
    packed = array.array("f")
    packed.frombytes(blob)
    return list(packed)


def save_vector(
    storage: Storage,
    *,
    recipe_id: int,
    model: str,
    vector: Sequence[float],
    digest: str,
) -> None:
    storage.execute(
        """
        INSERT INTO recipe_vectors (recipe_id, model, dim, vec, content_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(recipe_id, model) DO UPDATE SET
            dim = excluded.dim,
            vec = excluded.vec,
            content_hash = excluded.content_hash,
            updated_at = excluded.updated_at
        """,
        (int(recipe_id), model, len(vector), _pack(vector), digest),
    )


def load_vectors(storage: Storage, model: str) -> list[tuple[int, list[float]]]:
    """读某个模型的全部向量（`(recipe_id, vector)`）。库小，整批进内存最快。"""
    rows = storage.query(
        "SELECT recipe_id, vec FROM recipe_vectors WHERE model = ?", (model,)
    )
    return [(int(row["recipe_id"]), _unpack(row["vec"])) for row in rows]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度。维度不一致返回 0（不猜、不补齐）。"""
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def search_vectors(
    query: Sequence[float],
    vectors: Sequence[tuple[int, Sequence[float]]],
    *,
    limit: int = 20,
    min_score: float = 0.0,
) -> list[int]:
    """按余弦相似度取前 `limit` 个菜 id（高到低）。

    `min_score` **必须给**（第 182 步的教训）：不给门槛时，任何查询都会捞出
    前 20 道菜 —— 连"应该查不到"的查询也变成"有几道"，评测里的
    `zero_hit_accuracy` 直接掉到 0。语义召回只能**加分**，不能把空结果变成有结果。

    第 253 步：实现委托给 `InMemoryVectorIndex`（`core/vector_index.py`），
    这样「排序 / 门槛 / limit 下限」只有一份定义；换索引实现时两边不会漂移。
    局部 import 是为了避开 `recipe_vectors ↔ vector_index` 的循环依赖。
    """
    from core.vector_index import InMemoryVectorIndex

    index = InMemoryVectorIndex({recipe_id: vector for recipe_id, vector in vectors})
    return [
        recipe_id
        for recipe_id, _score in index.search(
            query, limit=limit, min_score=min_score
        )
    ]


def rrf_fuse(rankings: Sequence[Sequence[int]], *, k: int = RRF_K) -> list[int]:
    """Reciprocal Rank Fusion：`score = Σ 1/(k + rank)`，rank 从 1 开始。

    为什么用它而不是"加权求和"：两路的分数不可比（BM25 是任意尺度的正数，
    余弦在 [-1,1]），而排名是可比的。RRF 不需要调权重、单测好写。
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, recipe_id in enumerate(ranking, start=1):
            scores[int(recipe_id)] = scores.get(int(recipe_id), 0.0) + 1.0 / (
                k + position
            )
    return [
        recipe_id
        for recipe_id, _score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def center_vectors(
    vectors: Sequence[Sequence[float]],
    query: Sequence[float],
) -> tuple[list[list[float]], list[float]]:
    """减去**库的均值向量**再比较（第 188 步）。

    为什么需要：句向量有各向异性 —— 所有菜谱向量都挤在同一个方向上，
    余弦相似度于是全落在很窄的区间里（实测 0.38~0.57），连"库里没有"都分不开。
    减去均值能把这个"公共方向"去掉，实测可分离度从 0.071 提到 0.133，
    "库里没有"的最高分从 0.45 掉到 0.20。

    库为空时原样返回（不制造一个不存在的中心）。
    """
    if len(vectors) < 4:
        # 样本太少时均值没有代表性：把 1~3 条向量减掉它们的均值，
        # 结果几乎是 0 向量 —— 余弦全变成 0，召回直接失效。
        return [list(vector) for vector in vectors], list(query)

    dim = len(vectors[0])
    total = [0.0] * dim
    for vector in vectors:
        for index, value in enumerate(vector):
            if index < dim:
                total[index] += value
    count = len(vectors)
    mean = [value / count for value in total]
    return (
        [[value - mean[index] for index, value in enumerate(vector)] for vector in vectors],
        [value - mean[index] for index, value in enumerate(query)],
    )
