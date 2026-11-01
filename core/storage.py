"""通用 SQLite 存储层：连接与锁、WAL、外键、版本化迁移、FTS5 探测与打开失败降级。

接口都是 `async def`，而这里的 sqlite3 调用是同步的，会短暂阻塞事件循环（查询都很短，
是单机自用的已知取舍）；要多用户或大库时在调用点用 `asyncio.to_thread`。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 默认库文件位置；PET_DB_PATH 环境变量可覆盖（见 resolve_db_path）
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "pet.db"

#: 记忆库路径的显示名（日志/错误信息里用）
MEMORY_LABEL = "内存库"

#: 迁移登记表：每个版本一项（一组 SQL 语句）。
#: 领域模块在 import 时用 register_migration() 登记自己的 schema，
#: 版本号按登记顺序从 1 递增；应用时机由调用方显式决定
#: （main.py 启动时通过 recipe_library.ensure_schema 应用一次）。
#: 版本号 -> 该版本的 SQL 语句。**键是显式版本号，不是列表下标**。
#:
#: 为什么改（第 91 步）：原来用 `append` + `enumerate(start=1)`，
#: 版本号等于"第几个 import 的模块"——`core/history` 只要比 `core.recipe_library`
#: 先被导入，它的 v3 就会排到第 1 位，整个映射错位。
#: 实测表现：一个"手动退回 user_version=2"的测试重跑迁移时，把 v2 当成了 v3 又执行一遍，
#: 报"table recipe_aversions already exists"。**导入顺序不该决定库结构。**
#: 每一项可以是 SQL 字符串，也可以是 `f(conn)` 函数 —— 后者用于
#: "必须看库结构才能决定做什么"的迁移（比如加列：SQLite 没有 ADD COLUMN IF NOT EXISTS）。
#: 没有这个口子的话，第 123 步只能把 `ALTER TABLE` 硬写进 SQL，
#: 于是"把 user_version 退回再重跑"的场景会撞 `duplicate column name`（实测踩了）。
MIGRATIONS: dict[int, tuple[Any, ...]] = {}


def register_migration(statements: Sequence[Any], *, version: int) -> int:
    """登记一个新版本（一组按顺序执行的语句）；返回版本号（从 1 起）。

    注意：这里只登记、不执行。测试与领域模块可以在自己的库里
    显式调 `Storage.apply_migrations(...)`，互不污染全局表。
    """
    if version < 1:
        raise ValueError(f"迁移版本号必须从 1 开始，收到 {version}")
    if version in MIGRATIONS:
        raise ValueError(f"迁移版本 {version} 已被登记过")
    MIGRATIONS[version] = tuple(statements)
    return version


def resolve_db_path(explicit: str | Path | None = None) -> Path:
    """决定用哪个库文件：显式参数 > PET_DB_PATH 环境变量 > 默认 data/pet.db。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PET_DB_PATH", "").strip()
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def display_path(path: str | Path) -> str:
    """日志用：项目内的文件显示相对路径，项目外的显示绝对路径。"""
    if str(path) == ":memory:":
        return MEMORY_LABEL
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def _detect_fts5(conn: sqlite3.Connection) -> bool:
    """试建一张临时虚拟表来探测 FTS5 是否可用；失败不抛，只记 False。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts_probe")
    except sqlite3.Error:
        return False
    return True


class Storage:
    """一个带锁的 SQLite 连接 + 版本化迁移机制。

    `path` 传 `":memory:"` 就是内存库（主程序降级时用 `Storage.memory()`）。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._closed = False
        self._in_transaction = False

        if self.path != ":memory:":
            # 目录不存在就建（父目录自动创建）；建不出来会抛 OSError，
            # 由 open_storage 接住并降级。
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # 自动提交：写操作立即可见，事务由 transaction() 显式开
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self.fts_available = _detect_fts5(conn)
        except BaseException:
            conn.close()  # 构造失败不留半开连接
            raise
        self._conn = conn

    # ------------------------------------------------------------- 打开与关闭

    @classmethod
    def memory(cls) -> Storage:
        """内存库：进程内有效，重启即清空（降级路径专用）。"""
        return cls(":memory:")

    def close(self) -> None:
        """关闭连接；重复调用无副作用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _guard(self) -> None:
        if self._closed:
            raise RuntimeError("Storage 已关闭，不能再执行语句")

    # ------------------------------------------------------------- 执行与查询

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """执行一条写/读语句并立即提交（自动提交模式）。"""
        with self._lock:
            self._guard()
            return self._conn.execute(sql, params)

    def executemany(
        self, sql: str, seq: Sequence[Sequence[Any]] = ()
    ) -> sqlite3.Cursor:
        with self._lock:
            self._guard()
            return self._conn.executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._guard()
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            self._guard()
            return self._conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------- 事务与迁移

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：块内语句要么一起提交，要么一起回滚。

        `BEGIN IMMEDIATE` 马上拿写锁，避免"先读后写"的多进程升级死锁。
        不支持嵌套（领域层目前没有这个需求；真需要时再引入 SAVEPOINT）。
        """
        with self._lock:
            self._guard()
            if self._in_transaction:
                raise RuntimeError("不支持嵌套事务")
            self._in_transaction = True
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._in_transaction = False

    def user_version(self) -> int:
        """当前 schema 版本（PRAGMA user_version）。"""
        with self._lock:
            self._guard()
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def apply_migrations(
        self, migrations: Sequence[Sequence[str]] | None = None
    ) -> int:
        """把库逐版本升到最新；返回升级后的版本号。幂等。

        `migrations` 缺省用模块级登记表（应用启动路径）；
        测试和领域代码可以传自己的列表，不碰全局状态。
        """
        # 缺省用登记表（按**显式版本号**升序）；传入序列时按"下标 = 版本"
        # 处理（测试传自己的列表，那条语义不变）。
        if migrations is None:
            plan = [(version, MIGRATIONS[version]) for version in sorted(MIGRATIONS)]
        else:
            plan = list(enumerate(migrations, start=1))
        current = self.user_version()
        for version, statements in plan:
            if version <= current:
                continue
            with self.transaction() as conn:
                for statement in statements:
                    if callable(statement):
                        statement(conn)
                    else:
                        conn.execute(statement)
                # PRAGMA 不能带参数占位符；version 是内部生成的整数，安全
                conn.execute(f"PRAGMA user_version = {version}")
        return self.user_version()


def open_storage(path: str | Path | None = None) -> tuple[Storage, str | None]:
    """打开库（失败降级内存库）。

    返回 `(storage, 原因)`：成功时原因为 None；失败时是给日志用的一句话。
    任何打开失败都降级（权限/路径/损坏/磁盘满……），桌宠不因此起不来。
    """
    resolved = resolve_db_path(path)
    try:
        return Storage(resolved), None
    except Exception as exc:  # noqa: BLE001 —— 打开失败的兜底就在这里
        reason = (
            f"打开数据库失败（{display_path(resolved)}，{type(exc).__name__}: {exc}），"
            f"已降级为{MEMORY_LABEL}，数据重启即清空"
        )
        return Storage.memory(), reason
