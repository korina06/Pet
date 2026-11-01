"""嵌入客户端：响应要按 `index` 排回原序、按上限分批、没有 key 时返回 None 而不是抛异常。
"""

from __future__ import annotations

import json

import httpx
import pytest

from core.config import Settings
from core.embeddings import (
    EMBED_BATCH_SIZE,
    GlmEmbedder,
    HashEmbedder,
    build_embedder,
)


async def test_hash_embedder_is_deterministic_and_normalised():
    embedder = HashEmbedder(dim=32)
    first = (await embedder.embed(["番茄炒蛋"]))[0]
    second = (await embedder.embed(["番茄炒蛋"]))[0]

    assert first == second, "同样的文本必须给同样的向量（否则测试没法断言）"
    # 注意：项目的 check_frontend 用的是自带的极简 pytest 桩，**没有 approx**，
    # 所以这里手算范数（这也是这套测试能在两条路径下都跑的原因）。
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-6, f"向量没有归一化：范数 {norm}"


async def test_glm_embedder_orders_vectors_by_index():
    """接口返回顺序不可靠，必须按 `index` 还原。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen.append(payload)
        # 故意倒序返回
        data = [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
        return httpx.Response(200, json={"data": data})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = GlmEmbedder(
        api_key="test-key",
        base_url="https://example.invalid/v4",
        model="embedding-3",
        client=client,
    )
    vectors = await embedder.embed(["第一条", "第二条"])
    await client.aclose()

    assert vectors == [[1.0, 0.0], [0.0, 1.0]], "没有按 index 排回去"
    assert seen[0]["model"] == "embedding-3"
    assert seen[0]["input"] == ["第一条", "第二条"]


async def test_glm_embedder_batches_long_inputs():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        batch = payload["input"]
        calls.append(len(batch))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index), 1.0]}
                    for index in range(len(batch))
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = GlmEmbedder(
        api_key="k", base_url="https://example.invalid", model="m", client=client
    )
    count = EMBED_BATCH_SIZE * 2 + 1
    vectors = await embedder.embed([f"文本{index}" for index in range(count)])
    await client.aclose()

    assert len(vectors) == count
    assert calls == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 1], f"分批不对：{calls}"


async def test_glm_embedder_rejects_short_responses():
    """条数不符要报错，绝不能"凑合用"——否则向量和菜谱会错位。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = GlmEmbedder(
        api_key="k", base_url="https://example.invalid", model="m", client=client
    )
    with pytest.raises(ValueError):
        await embedder.embed(["甲", "乙"])
    await client.aclose()


def test_build_embedder_returns_none_without_key():
    """没有 key ≠ 报错：向量路自动关闭，字面检索照常。"""
    assert build_embedder(settings=Settings(_env_file=None, glm_api_key="")) is None


def test_build_embedder_respects_the_switch():
    conf = Settings(_env_file=None, glm_api_key="k", embedding_enabled=False)
    assert build_embedder(settings=conf) is None

    on = Settings(_env_file=None, glm_api_key="k", embedding_enabled=True)
    embedder = build_embedder(settings=on)
    assert embedder is not None and embedder.model == on.glm_embedding_model
