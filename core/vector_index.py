"""向量索引的最小接口 `add / search / delete` 与默认的内存暴力实现。

换存储实现时只替换这一层，召回之后的门槛、裁决与融合不跟着换。
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from core.recipe_vectors import cosine, load_vectors


@runtime_checkable
class VectorIndex(Protocol):
    """向量索引的最小接口。"""

    def add(self, recipe_id: int, vector: Sequence[float]) -> None:
        """加入/覆盖一条向量（同一个 id 覆盖旧值）。"""
        ...

    def search(
        self, query: Sequence[float], *, limit: int = 20, min_score: float = 0.0
    ) -> list[tuple[int, float]]:
        """返回 ``(recipe_id, score)``，按分数从高到低；低于 `min_score` 的不返回。"""
        ...

    def delete(self, recipe_id: int) -> None:
        """删除一条；不存在时静默（幂等）。"""
        ...


class InMemoryVectorIndex:
    """默认实现：dict + 纯 Python 余弦，O(N·D)。

    与 `search_vectors` 的排序规则、`limit` 下限、门槛语义保持一致，
    这样换实现时调用方不需要改判断。
    """

    def __init__(self, vectors: Mapping[int, Sequence[float]] | None = None) -> None:
        self._items: dict[int, list[float]] = {
            int(recipe_id): [float(value) for value in vector]
            for recipe_id, vector in (vectors or {}).items()
        }

    def __len__(self) -> int:
        return len(self._items)

    def add(self, recipe_id: int, vector: Sequence[float]) -> None:
        self._items[int(recipe_id)] = [float(value) for value in vector]

    def delete(self, recipe_id: int) -> None:
        self._items.pop(int(recipe_id), None)

    def search(
        self, query: Sequence[float], *, limit: int = 20, min_score: float = 0.0
    ) -> list[tuple[int, float]]:
        scored = [
            (cosine(query, vector), recipe_id)
            for recipe_id, vector in self._items.items()
        ]
        scored = [pair for pair in scored if pair[0] >= min_score]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [(recipe_id, score) for score, recipe_id in scored[: max(1, limit)]]


def build_vector_index(storage: object, model: str) -> InMemoryVectorIndex:
    """从库里的 `recipe_vectors` 装一个内存索引。

    换库时改这里（按 `docs/SCALE.md` 的判据选实现），调用方不用动。
    """
    return InMemoryVectorIndex(dict(load_vectors(storage, model)))
