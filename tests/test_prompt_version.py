"""提示词版本化：版本号是提示词哈希、Agent 每轮写进日志上下文、账本带上它、迁移幂等。
"""

from __future__ import annotations

import asyncio

import pytest

from agents.agent import Agent, build_system_prompt, prompt_version
from core.storage import Storage


def test_the_version_is_a_short_hash_of_the_prompt():
    assert len(prompt_version("你好")) == 8
    assert all(char in "0123456789abcdef" for char in prompt_version("你好"))


def test_the_same_prompt_always_gets_the_same_version():
    text = build_system_prompt(["search_recipes"])

    assert prompt_version(text) == prompt_version(text)


def test_changing_one_character_changes_the_version():
    base = "你是桌宠九九。"
    assert prompt_version(base) != prompt_version(base + "！")


def test_the_empty_prompt_still_has_a_stable_version():
    """空提示也要有个确定的值，别变成空字符串混进账本。"""
    assert prompt_version("") == prompt_version("")
    assert prompt_version("") != ""


def test_the_agent_puts_its_prompt_version_into_the_ledger(tmp_path):
    """跑一轮真实循环，账本那一行必须带上提示词版本。"""
    from core import log_context, usage
    from core.recipe_library import ensure_schema
    from core.schemas import Message, ToolResult
    from tools.base import BaseTool
    from tools.registry import ToolRegistry

    class _Echo(BaseTool):
        name = "echo"
        description = "回声"
        show_in_menu = False
        parameters = {"type": "object", "properties": {}}

        async def run(self, **arguments) -> ToolResult:
            return ToolResult(tool_name=self.name, ok=True, content="ok")

    class _Response:
        """流式响应替身：一行 delta + 一行带 usage 的收尾 + [DONE]。"""

        is_success = True

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"好"}}]}'
            yield (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}'
            )
            yield "data: [DONE]"

        async def aread(self) -> bytes:
            return b""

    class _Stream:
        async def __aenter__(self) -> "_Response":
            return _Response()

        async def __aexit__(self, *_exc) -> bool:
            return False

    class _Client:
        """假 HTTP 客户端：Agent 走的是流式路径（run() 内部用 run_stream）。"""

        def stream(self, *_args, **_kwargs) -> "_Stream":
            return _Stream()

        async def aclose(self) -> None:
            return None

    from agents.deepseek_llm import DeepSeekLLM
    from core.config import Settings

    db = Storage(":memory:")
    ensure_schema(db)

    registry = ToolRegistry()
    registry.register(_Echo())
    system_prompt = build_system_prompt(["echo"])
    llm = DeepSeekLLM(
        settings=Settings(deepseek_api_key="test-key"), storage=db
    )
    llm._client = _Client()             # 换掉真客户端，避免联网
    agent = Agent(llm=llm, registry=registry, max_steps=2, system_prompt=system_prompt)

    log_context.set_context("chat", "sess-pv", "turn-pv")
    asyncio.run(agent.run([Message(role="user", content="嗨")]))

    rows = usage.recent(db, 5)
    assert rows, "账本应该有记录"
    assert rows[0]["prompt_version"] == prompt_version(system_prompt), rows[0]


def test_the_v7_migration_is_idempotent():
    """v7 加列要先看有没有这列：SQLite 没有 ADD COLUMN IF NOT EXISTS。

    这个场景真的会发生：`test_old_v2_table_shape_still_works` 会把 user_version
    退回 2 再跑迁移，那时 `llm_usage` 里已经有 `prompt_version` 了。
    """
    db = Storage(":memory:")
    db.apply_migrations()
    columns = {row[1] for row in db.execute("PRAGMA table_info(llm_usage)")}
    assert "prompt_version" in columns

    # 模拟"版本号回退但结构已是新的"：再跑一次必须不炸
    db.execute("PRAGMA user_version = 6")
    db.apply_migrations()
    from core.storage import MIGRATIONS

    # 断言"追平到最新"而不是写死 7：v8（签到表）加进来之后，这条只关心
    # "回退重跑能把版本补回来、且 v7 的加列不炸"。
    assert db.user_version() == max(MIGRATIONS)
    assert "prompt_version" in {row[1] for row in db.execute("PRAGMA table_info(llm_usage)")}


@pytest.mark.parametrize("feature", ["chat", "recipe"])
def test_summary_can_group_by_prompt_version(feature):
    from core import usage

    db = Storage(":memory:")
    db.apply_migrations()
    usage.record(
        db, provider="deepseek", model="m", feature=feature, session_id="s",
        turn_id="t", step=1, stream=False,
        usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        prompt_version="12345678",
    )
    usage.record(
        db, provider="deepseek", model="m", feature=feature, session_id="s",
        turn_id="t", step=2, stream=False,
        usage={"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        prompt_version="87654321",
    )

    picked = usage.summary(db, prompt_version="12345678")
    assert picked["total"]["calls"] == 1, picked
    assert picked["total"]["total"] == 6, picked
