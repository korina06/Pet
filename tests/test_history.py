"""消息落库：迁移后老数据不少、新建实例能读回、两个 scope 不串、显示上限与上下文上限分开。
"""

from __future__ import annotations

import asyncio

from core import history
from core.recipe_library import ensure_schema
from core.recipes import RecipeSessionStore
from core.schemas import Message
from core.session import SessionStore
from core.storage import Storage


def _db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    return db


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


# --------------------------------------------------------------- 迁移与表


def test_migration_reaches_the_latest_version_and_keeps_old_tables() -> None:
    """升到最新版本只**加**表，原有菜谱/忌口表不受影响。

    这里刻意不写死版本号：以后每加一版迁移都要改测试，那就变成"测试在管版本号"
    而不是"版本号被测试守着"。断言改成"跟登记表里最大的那个一致"。
    """
    from core.storage import MIGRATIONS

    db = _db()

    assert db.user_version() == max(MIGRATIONS), f"登记表：{sorted(MIGRATIONS)}"
    tables = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "chat_messages" in tables, "v3 的对话表"
    assert "app_settings" in tables, "v4 的应用小配置表（当前选中的模型）"
    assert {"recipes", "recipe_aversions", "ingredient_aliases"} <= tables

    columns = {
        row["name"] for row in db.query("PRAGMA table_info(chat_messages)")
    }
    assert columns == {"id", "scope", "session_id", "role", "content", "created_at"}


# --------------------------------------------------------------- 基本读写


def test_append_and_load_roundtrip_keeps_order() -> None:
    db = _db()
    history.append_messages(
        db,
        history.SCOPE_CHAT,
        "s1",
        [_msg("user", "你好"), _msg("assistant", "主人好")],
    )

    rows = history.load_messages(db, history.SCOPE_CHAT, "s1", 10)

    assert [m.role for m in rows] == ["user", "assistant"]
    assert [m.content for m in rows] == ["你好", "主人好"], "必须按时间正序"


def test_scopes_do_not_leak_into_each_other() -> None:
    db = _db()
    history.append_messages(db, history.SCOPE_CHAT, "s1", [_msg("user", "聊天")])
    history.append_messages(db, history.SCOPE_RECIPE, "s1", [_msg("user", "菜谱")])

    chat = history.load_messages(db, history.SCOPE_CHAT, "s1", 10)
    recipe = history.load_messages(db, history.SCOPE_RECIPE, "s1", 10)

    assert [m.content for m in chat] == ["聊天"]
    assert [m.content for m in recipe] == ["菜谱"], "同名 session 也要按 scope 分开"


def test_load_returns_the_most_recent_n_in_order() -> None:
    db = _db()
    history.append_messages(
        db, history.SCOPE_CHAT, "s1", [_msg("user", f"第{i}条") for i in range(10)]
    )

    rows = history.load_messages(db, history.SCOPE_CHAT, "s1", 3)

    assert [m.content for m in rows] == ["第7条", "第8条", "第9条"]


def test_rows_are_trimmed_to_the_display_cap(monkeypatch) -> None:
    """每个会话在库里最多留 `MAX_ROWS_PER_SESSION` 行 —— 否则库会一直长。"""
    monkeypatch.setattr(history, "MAX_ROWS_PER_SESSION", 5)
    db = _db()

    history.append_messages(
        db, history.SCOPE_CHAT, "s1", [_msg("user", f"第{i}条") for i in range(9)]
    )

    assert history.count_messages(db, history.SCOPE_CHAT, "s1") == 5
    rows = history.load_messages(db, history.SCOPE_CHAT, "s1", 10)
    assert [m.content for m in rows] == ["第4条", "第5条", "第6条", "第7条", "第8条"]


def test_clear_reports_how_many_rows_were_removed() -> None:
    db = _db()
    history.append_messages(
        db, history.SCOPE_CHAT, "s1", [_msg("user", "a"), _msg("user", "b")]
    )

    assert history.clear_messages(db, history.SCOPE_CHAT, "s1") == 2
    assert history.load_messages(db, history.SCOPE_CHAT, "s1", 10) == []
    assert history.clear_messages(db, history.SCOPE_CHAT, "s1") == 0, "再删一次是 0"


# ------------------------------------------- store 层：等价于"重启进程"


def test_chat_history_survives_a_new_store_instance() -> None:
    """新实例 = 进程重启：库在，历史就在。"""
    db = _db()

    async def first_run() -> None:
        store = SessionStore(max_messages=40, storage=db)
        await store.append("s1", _msg("user", "我叫小明"), _msg("assistant", "记住了"))

    asyncio.run(first_run())

    async def second_run() -> list[Message]:
        store = SessionStore(max_messages=40, storage=db)    # 全新实例
        return await store.get_history("s1")

    rows = asyncio.run(second_run())
    assert [m.content for m in rows] == ["我叫小明", "记住了"]


def test_recipe_history_survives_a_new_store_instance() -> None:
    db = _db()

    store = RecipeSessionStore(storage=db)
    store.append("r1", _msg("user", "想吃鱼"), _msg("assistant", "清蒸鲈鱼"))

    fresh = RecipeSessionStore(storage=db)                    # 模拟重启
    session = fresh.ensure("r1")

    assert [m.content for m in session.messages] == ["想吃鱼", "清蒸鲈鱼"]


def test_recipe_reset_also_deletes_from_the_database() -> None:
    """「新建会话」之后历史不能又冒出来 —— 所以 reset 必须连库一起删。"""
    db = _db()
    store = RecipeSessionStore(storage=db)
    store.append("r1", _msg("user", "想吃鱼"))

    store.reset("r1")

    assert history.count_messages(db, history.SCOPE_RECIPE, "r1") == 0
    assert RecipeSessionStore(storage=db).ensure("r1").messages == []


def test_chat_reset_deletes_from_the_database() -> None:
    db = _db()

    async def run() -> list[Message]:
        store = SessionStore(max_messages=40, storage=db)
        await store.append("s1", _msg("user", "你好"))
        await store.reset("s1")
        return await store.get_history("s1")

    rows = asyncio.run(run())
    assert rows == []
    assert history.count_messages(db, history.SCOPE_CHAT, "s1") == 0


# ------------------------------------- 显示上限 vs 上下文上限（关键）


def test_context_stays_40_while_display_can_reach_200() -> None:
    """**两个上限不是一回事**：喂模型 40 条，界面能翻 200 条。

    混用会让上下文随历史无限增长 —— 而 token 是真金白银。
    """
    db = _db()
    history.append_messages(
        db, history.SCOPE_CHAT, "s1", [_msg("user", f"第{i}条") for i in range(120)]
    )

    async def run() -> tuple[list[Message], list[Message]]:
        store = SessionStore(max_messages=40, storage=db)
        return (
            await store.get_history("s1"),
            await store.history_for_display("s1"),
        )

    context, display = asyncio.run(run())

    assert len(context) == 40, f"模型上下文应当只有 40 条，实际 {len(context)}"
    assert len(display) == 120, f"界面应当能翻到更多，实际 {len(display)}"
    assert context[-1].content == display[-1].content, "两边的最新一条必须一致"


# ------------------------------------------------- 没有 storage 时不能坏


def test_without_storage_the_store_still_works_in_memory() -> None:
    """测试与"数据库降级成内存库"的极端情况：不传 storage 也要照常工作。"""
    store = SessionStore(max_messages=4)

    async def run() -> list[Message]:
        for i in range(6):
            await store.append("s1", _msg("user", f"第{i}条"))
        return await store.get_history("s1")

    rows = asyncio.run(run())
    assert [m.content for m in rows] == ["第2条", "第3条", "第4条", "第5条"]


def test_migration_versions_are_explicit_not_import_order() -> None:
    """版本号必须由 `version=` 明确指定，**不能等于导入顺序**（第 91 步）。

    踩过的坑：`core/history` 一旦比 `core.recipe_library` 先被导入，
    它登记的 v3 就会排到第 1 位（原来用 `append` + `enumerate`）。
    后果很隐蔽：一个"把 user_version 退回 2 再跑迁移"的测试，
    会把 v2 当成 v3 又执行一遍，报「table recipe_aversions already exists」。
    **导入顺序不该决定库结构。**
    """
    from core.storage import MIGRATIONS

    assert sorted(MIGRATIONS) == [1, 2, 3, 4, 5, 6, 7, 8], f"版本号应当连续且显式：{sorted(MIGRATIONS)}"
    assert "recipes" in MIGRATIONS[1][0], "v1 应当是菜谱主表"
    assert "recipe_aversions" in MIGRATIONS[2][0], "v2 应当是忌口表"
    assert "chat_messages" in MIGRATIONS[3][0], "v3 应当是对话消息表"
    assert "app_settings" in MIGRATIONS[4][0], "v4 应当是应用小配置表"
    assert "recipe_session_meta" in MIGRATIONS[5][0], "v5 应当是食谱会话的小状态表"
    assert "llm_usage" in MIGRATIONS[6][0], "v6 应当是模型调用用量账本"
    # v7 是**函数**迁移（加列要先看有没有这列）：SQLite 没有 ADD COLUMN IF NOT EXISTS
    assert callable(MIGRATIONS[7][0]), "v7 应当是带判断的加列迁移"
    assert "checkin_days" in MIGRATIONS[8][0], "v8 应当是签到记录表"
    assert "checkin_cards" in MIGRATIONS[8][1], "v8 还应建补签卡表"


def test_registering_the_same_version_twice_is_an_error() -> None:
    """重复登记同一个版本号要**报错**，不能悄悄覆盖。"""
    import pytest

    from core.storage import MIGRATIONS, register_migration

    with pytest.raises(ValueError) as excinfo:
        register_migration(("SELECT 1",), version=3)

    assert "3" in str(excinfo.value)
    assert 3 in MIGRATIONS


def test_ensure_schema_registers_migrations_by_itself() -> None:
    """即使调用方**没 import 过** `core.history`，`ensure_schema` 也要把 v3 装上。

    实测踩过：迁移表靠 `import` 时登记，所以"注册表里有没有 v3"取决于谁先 import。
    只导入 `core.recipe_library` 的场景下，真库停在 v2、`chat_messages` 没建出来，
    写消息直接 `no such table` —— 而启动日志里一句异常都没有。
    """
    from core import recipe_library
    from core.storage import MIGRATIONS

    saved_v3 = MIGRATIONS.pop(3, None)
    try:
        db = Storage(":memory:")

        recipe_library.ensure_schema(db)

        assert db.user_version() == max(MIGRATIONS), (
            "ensure_schema 应当自己把缺的迁移登记并应用"
        )
        assert db.query_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_messages'"
        ) is not None
    finally:
        if saved_v3 is not None:
            MIGRATIONS[3] = saved_v3


def test_recipe_get_loads_from_the_database_after_a_restart() -> None:
    """`get()` 在"内存没有、库里有"时必须懒加载（第 92 步）。

    `/recipe/session` 用的就是 `get()`：只看内存的话，重启后打开食材窗口永远是空的，
    等于这层持久化白做。
    """
    db = _db()
    store = RecipeSessionStore(storage=db)
    store.append("r1", _msg("user", "想吃鱼"), _msg("assistant", "清蒸鲈鱼"))

    fresh = RecipeSessionStore(storage=db)          # 模拟重启
    session = fresh.get("r1")

    assert session is not None, "库里有历史，get() 不该返回 None"
    assert [m.content for m in session.messages] == ["想吃鱼", "清蒸鲈鱼"]


def test_recipe_get_returns_none_for_an_unknown_session() -> None:
    """库里也没有的会话：返回 None，**不要凭空造一个空会话**。"""
    db = _db()
    store = RecipeSessionStore(storage=db)

    assert store.get("never-seen") is None
    assert store.get("never-seen") is None, "重复调用也不该把空会话塞进内存"


def test_recipe_session_meta_survives_a_restart() -> None:
    """`recent_tags` / 忌口快照也要落库（第 102 步，评价4 P1-1）。

    只恢复消息不恢复 recent_tags，"上一轮用过的标签不该再参照"这条约束在重启后就失效了 ——
    **数据回来了、规则没回来**，是最难查的一种不一致。
    """
    db = _db()
    store = RecipeSessionStore(storage=db)
    store.append("r1", _msg("user", "想吃清淡的"))
    session = store.ensure("r1")
    session.recent_tags = ["清淡", "蒸"]
    session.aversions_snapshot = ["香菜"]
    store.save_meta(session)

    restored = RecipeSessionStore(storage=db).ensure("r1")     # 模拟重启

    assert [m.content for m in restored.messages] == ["想吃清淡的"]
    assert restored.recent_tags == ["清淡", "蒸"]
    assert restored.aversions_snapshot == ["香菜"]


def test_recipe_reset_clears_the_meta_too() -> None:
    db = _db()
    store = RecipeSessionStore(storage=db)
    store.append("r1", _msg("user", "想吃清淡的"))
    session = store.ensure("r1")
    session.recent_tags = ["清淡"]
    store.save_meta(session)

    store.reset("r1")

    assert RecipeSessionStore(storage=db).ensure("r1").recent_tags == []


def test_meta_without_storage_is_a_no_op() -> None:
    """不传 storage 时（测试 / 降级）不能炸，也不能假装存下来了。"""
    store = RecipeSessionStore()
    session = store.ensure("r1")
    session.recent_tags = ["清淡"]

    store.save_meta(session)          # 不抛异常即可

    assert store.ensure("r1").recent_tags == ["清淡"]
