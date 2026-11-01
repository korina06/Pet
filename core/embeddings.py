"""文本嵌入：把一段文本变成一个向量，怎么用不在这里。

不引 SDK（走 OpenAI 兼容的 `/embeddings`）、失败不吞不重试（降级交给上层）、
`HashEmbedder` 只给测试用且没有语义。
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, Sequence

import httpx

from core.config import Settings, get_settings

#: 一次请求最多送多少条文本。GLM 的嵌入接口有单次条数上限（不同模型不同），
#: 取一个保守值：宁可多请求几次，也不要被上游 400 打断整批。
EMBED_BATCH_SIZE = 16

#: 嵌入请求的超时（秒）。比对话短得多：这是**检索路径上的**一次调用，
#: 慢了宁可放弃走字面，也不要让用户等。
EMBED_TIMEOUT_SECONDS = 8.0


class Embedder(Protocol):
    """能把文本变成向量。`model` 用于给不同模型的向量做隔离。"""

    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与 `texts` **一一对应**的向量列表。失败抛异常。"""


class GlmEmbedder:
    """智谱 GLM 的嵌入模型（默认 `embedding-3`）。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [str(text or "").strip() for text in texts]
        if not cleaned:
            return []

        payload: dict[str, Any] = {"model": self.model, "input": cleaned}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        vectors: list[list[float]] = []
        for start in range(0, len(cleaned), EMBED_BATCH_SIZE):
            batch = cleaned[start:start + EMBED_BATCH_SIZE]
            payload["input"] = batch
            if self._client is not None:
                response = await self._client.post(
                    f"{self.base_url}/embeddings",
                    json=payload,
                    headers=headers,
                    timeout=EMBED_TIMEOUT_SECONDS,
                )
            else:
                async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        f"{self.base_url}/embeddings", json=payload, headers=headers
                    )
            response.raise_for_status()
            body = response.json()
            data = body.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError(
                    f"嵌入接口返回的条数不对：要 {len(batch)} 条，拿到 "
                    f"{len(data) if isinstance(data, list) else data!r}"
                )
            # 接口不保证顺序，但每条都带 index —— 按它排回去
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            for item in ordered:
                vector = item.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ValueError("嵌入接口返回了空向量")
                vectors.append([float(value) for value in vector])
        return vectors


class HashEmbedder:
    """**只给测试用**的确定性"嵌入"：字符 n-gram 哈希，没有任何语义。

    它存在的理由：单元测试不能依赖网络，也不能依赖真模型的输出。
    用它测的是**管线**（存、取、融合、降级），不是**语义**。
    """

    def __init__(self, *, model: str = "hash-test", dim: int = 64) -> None:
        self.model = model
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(str(text or "")) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        cleaned = text.strip()
        for index in range(len(cleaned)):
            gram = cleaned[index:index + 2]
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:2], "big") % self.dim
            vector[slot] += 1.0
        # 归一化，让余弦相似度不受文本长度影响
        norm = sum(value * value for value in vector) ** 0.5
        return [value / norm for value in vector] if norm else vector


def build_embedder(
    *, client: httpx.AsyncClient | None = None, settings: Settings | None = None
) -> Embedder | None:
    """按配置造一个可用的嵌入器；**没有 key 就返回 None**（检索层据此降级）。

    为什么返回 None 而不是抛：没 key 是**正常状态**（这个功能是可选的），
    不该让"打开应用"因此失败。
    """
    conf = settings or get_settings()
    if not conf.embedding_enabled:
        return None
    key = (conf.glm_api_key or "").strip()
    if not key:
        return None
    return GlmEmbedder(
        api_key=key,
        base_url=conf.glm_base_url,
        model=conf.glm_embedding_model,
        client=client,
    )
