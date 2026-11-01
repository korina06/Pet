"""存储基建：迁移逐版本到位且幂等、数据真落盘、打开失败降级、并发写不崩、WAL 与 FTS5 探测是真的。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import core.storage as storage_module  # noqa: E402
from core.storage import (  # noqa: E402
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    Storage,
    display_path,
    open_storage,
    resolve_db_path,
)

#: 测试用的两版迁移：第二版加一列，用来验"逐版本升级"真的生效
SAMPLE_MIGRATIONS = (
    ("CREATE TABLE notes(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)",),
    ("ALTER TABLE notes ADD COLUMN mood TEXT NOT NULL DEFAULT ''",),
)


# ------------------------------------------------------------------ 迁移


def test_migrations_upgrade_version_by_version(tmp_path):
    db = Storage(tmp_path / "pet.db")
    try:
        assert db.user_version() == 0
        assert db.apply_migrations(SAMPLE_MIGRATIONS[:1]) == 1
        columns = {row["name"] for row in db.query("PRAGMA table_info(notes)")}
        assert columns == {"id", "text"}          # v2 的列还不该在
        assert db.apply_migrations(SAMPLE_MIGRATIONS) == 2
        columns = {row["name"] for row in db.query("PRAGMA table_info(notes)")}
        assert columns == {"id", "text", "mood"}
    finally:
        db.close()


def test_migrations_are_idempotent(tmp_path):
    db = Storage(tmp_path / "pet.db")
    try:
        db.apply_migrations(SAMPLE_MIGRATIONS)
        db.execute("INSERT INTO notes(text, mood) VALUES (?, ?)", ("第一条", "开心"))
        # 重复应用：不报错、不重跑、不动已有数据
        assert db.apply_migrations(SAMPLE_MIGRATIONS) == 2
        assert db.apply_migrations(SAMPLE_MIGRATIONS) == 2
        assert [row["text"] for row in db.query("SELECT text FROM notes")] == ["第一条"]
    finally:
        db.close()


def test_failed_migration_rolls_back_the_whole_version(tmp_path):
    """一版里的语句要么全成，要么全败：半套 schema 比没升级更危险。"""
    db = Storage(tmp_path / "pet.db")
    try:
        broken = (("CREATE TABLE good(a)", "CREATE TABLE bad("),)  # 第二条语法错
        with pytest.raises(sqlite3.Error):
            db.apply_migrations(broken)
        assert db.user_version() == 0
        names = {row["name"] for row in db.query("SELECT name FROM sqlite_master")}
        assert "good" not in names                # 同一版里先成功的表也要回滚
    finally:
        db.close()


# ------------------------------------------------------------------ 落盘与降级


def test_data_survives_close_and_reopen(tmp_path):
    path = tmp_path / "pet.db"
    db = Storage(path)
    db.apply_migrations(SAMPLE_MIGRATIONS)
    db.execute("INSERT INTO notes(text, mood) VALUES (?, ?)", ("重启也要在", "平静"))
    db.close()

    again = Storage(path)
    try:
        assert again.user_version() == 2
        rows = again.query("SELECT text FROM notes")
        assert [row["text"] for row in rows] == ["重启也要在"]
        assert again.apply_migrations(SAMPLE_MIGRATIONS) == 2   # 已升过级 = 空操作
    finally:
        again.close()


def test_open_storage_degrades_to_memory_on_bad_path(tmp_path):
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    bad_path = blocker / "sub" / "pet.db"       # 父级是文件 → 建目录必失败

    db, reason = open_storage(bad_path)
    try:
        assert reason, "降级必须把原因带回来"
        assert "降级" in reason and "pet.db" in reason
        assert db.path == ":memory:"
        # 降级库照常可用：建表、写入、读回
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES (?)", ("仍在",))
        assert db.query_one("SELECT x FROM t")["x"] == "仍在"
    finally:
        db.close()


def test_transaction_commits_and_rolls_back(tmp_path):
    db = Storage(tmp_path / "pet.db")
    db.apply_migrations(SAMPLE_MIGRATIONS)
    try:
        with db.transaction() as conn:
            conn.execute("INSERT INTO notes(text) VALUES ('ok')")
        assert db.query_one("SELECT count(*) AS n FROM notes")["n"] == 1

        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO notes(text) VALUES ('boom')")
                raise RuntimeError("业务失败")
        assert db.query_one("SELECT count(*) AS n FROM notes")["n"] == 1

        # 嵌套事务：明确拒绝（而不是悄悄共享外层事务）
        with db.transaction() as conn:
            conn.execute("INSERT INTO notes(text) VALUES ('outer')")
            with pytest.raises(RuntimeError):
                with db.transaction():
                    pass
        assert db.query_one("SELECT count(*) AS n FROM notes")["n"] == 2
    finally:
        db.close()


def test_concurrent_writes_do_not_crash(tmp_path):
    """多线程同时写同一张连接：不崩、不丢（锁的作用）。"""
    db = Storage(tmp_path / "pet.db")
    db.apply_migrations(SAMPLE_MIGRATIONS)
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for i in range(25):
                db.execute(
                    "INSERT INTO notes(text) VALUES (?)", (f"w{worker_id}-{i:02d}",)
                )
        except BaseException as exc:  # noqa: BLE001 —— 收集起来给主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert db.query_one("SELECT count(*) AS n FROM notes")["n"] == 100
    finally:
        db.close()


# ------------------------------------------------------------------ 环境真实性


def test_wal_is_enabled_for_file_databases(tmp_path):
    db = Storage(tmp_path / "pet.db")
    try:
        mode = db.query_one("PRAGMA journal_mode")[0]
        assert mode.lower() == "wal"
    finally:
        db.close()


def test_fts5_probe_reports_true_on_this_environment():
    """本机两个解释器都带 FTS5（第 47 步开工前的侦察结论）。

    哪天这条红了：说明环境换了，**不是**测试坏了——
    那是提醒把 recipe_library 的 LIKE 回退路径当成一等公民来对待。
    """
    db = Storage.memory()
    try:
        assert db.fts_available is True
    finally:
        db.close()


def test_fts5_probe_can_be_forced_unavailable(monkeypatch, tmp_path):
    """注入"没有 FTS5"的环境：探测失败只记开关，不能崩。"""
    monkeypatch.setattr(storage_module, "_detect_fts5", lambda conn: False)
    db = Storage(tmp_path / "pet.db")
    try:
        assert db.fts_available is False
        # 没有 FTS5 也不影响普通读写
        db.apply_migrations(SAMPLE_MIGRATIONS)
        db.execute("INSERT INTO notes(text) VALUES ('无 FTS 也能写')")
        assert db.query_one("SELECT count(*) AS n FROM notes")["n"] == 1
    finally:
        db.close()


# ------------------------------------------------------------------ 路径规则


def test_db_path_prefers_argument_then_env_then_default(monkeypatch):
    monkeypatch.delenv("PET_DB_PATH", raising=False)
    assert resolve_db_path(None) == DEFAULT_DB_PATH
    assert DEFAULT_DB_PATH == PROJECT_ROOT / "data" / "pet.db"

    from_env = PROJECT_ROOT / "somewhere" / "from_env.db"
    monkeypatch.setenv("PET_DB_PATH", str(from_env))
    assert resolve_db_path(None) == from_env

    explicit = PROJECT_ROOT / "somewhere" / "explicit.db"
    assert resolve_db_path(explicit) == explicit      # 显式参数最大


def test_display_path_relative_inside_absolute_outside():
    assert display_path(DEFAULT_DB_PATH) == str(Path("data") / "pet.db")
    beyond = PROJECT_ROOT.parent / "somewhere-else.db"
    assert display_path(beyond) == str(beyond.resolve())
    assert display_path(":memory:") == "内存库"

def test_storage_docstring_states_the_real_threading_model():
    """说清真相（第 71 步）：接口**全是 `async def`**，跑在事件循环上。

    以前这段注释写的是"接口跑在 FastAPI 的线程池里"——**恰好说反了**。
    FastAPI 只把 `def` 端点丢线程池；`async def` 直接在循环上跑，
    所以这里的同步 sqlite3 调用是真的会阻塞循环的。

    这条测试不是咬文嚼字：注释错了，后来的人就会基于错的模型做决定
    （比如以为"反正在线程池里，慢一点无所谓"）。取舍可以保留，理由必须是真的。

    注意：这里**不**断言"线程池"三个字不出现 —— 注释里正是引用了那句错话做对照，
    机械地查关键词会把"解释为什么以前错了"也判成违规。查的是**真相有没有写出来**。
    """
    import inspect

    import core.storage as storage

    doc = inspect.getdoc(storage) or ""

    assert "事件循环" in doc, "要写明同步调用发生在事件循环上"
    assert "阻塞" in doc, "要承认它会阻塞循环"
    assert "to_thread" in doc, "要给出真到那一天的正解"
    assert "已知取舍" in doc, "要明确这是**选择**，不是疏忽"
