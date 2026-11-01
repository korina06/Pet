"""会话记忆的测试。"""

from __future__ import annotations

from core.schemas import Message
from core.session import SessionStore


async def test_get_history_returns_copy_not_internal_list():
    """返回的必须是副本：外部修改不能污染存储内部状态。"""
    store = SessionStore()
    await store.append("s1", Message(role="user", content="你好"))

    history = await store.get_history("s1")
    history.clear()

    assert len(await store.get_history("s1")) == 1


async def test_unknown_session_returns_empty_history():
    store = SessionStore()
    assert await store.get_history("never-seen") == []


async def test_messages_are_isolated_between_sessions():
    store = SessionStore()
    await store.append("a", Message(role="user", content="我是 A"))
    await store.append("b", Message(role="user", content="我是 B"))

    history_a = await store.get_history("a")
    history_b = await store.get_history("b")

    assert [m.content for m in history_a] == ["我是 A"]
    assert [m.content for m in history_b] == ["我是 B"]


async def test_history_is_truncated_to_max_messages():
    """历史超长时必须只保留最近的 N 条，防止上下文无限增长。"""
    store = SessionStore(max_messages=4)
    for i in range(10):
        await store.append("s1", Message(role="user", content=f"第{i}条"))

    history = await store.get_history("s1")

    assert len(history) == 4
    # 保留的是最近的 4 条：第6~9条
    assert [m.content for m in history] == ["第6条", "第7条", "第8条", "第9条"]


async def test_reset_clears_session():
    store = SessionStore()
    await store.append("s1", Message(role="user", content="你好"))
    await store.reset("s1")

    assert await store.get_history("s1") == []
