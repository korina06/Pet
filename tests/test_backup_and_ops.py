"""备份与运维：备份包能打开、清单里的 sha256 与包内字节一致、库缺失时跳过而不是报错。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backup_data import backup  # noqa: E402


def _make_db(path: Path, rows: tuple[str, ...] = ("清蒸鲈鱼",)) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE dishes (name TEXT)")
        conn.executemany("INSERT INTO dishes VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def test_backup_produces_an_openable_copy_with_matching_hashes(tmp_path: Path) -> None:
    db = tmp_path / "pet.db"
    _make_db(db, ("清蒸鲈鱼", "番茄炒蛋"))
    images = tmp_path / "recipe_images.db"      # 故意不建：模拟"还没有图片库"

    archive = backup(tmp_path / "out", db_path=db, image_path=images)

    assert archive.exists(), "备份包没生成"
    with zipfile.ZipFile(archive) as bundle:
        assert "pet.db" in bundle.namelist()
        assert "manifest.json" in bundle.namelist()
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        payload = bundle.read("pet.db")

    # 1) 清单里的哈希必须和包里的字节一致
    entry = next(e for e in manifest["files"] if e["name"] == "pet.db")
    assert hashlib.sha256(payload).hexdigest() == entry["sha256"], "清单哈希对不上"

    # 2) 备份出来的库要**能打开、数据还在**（不是只有个文件）
    restored = tmp_path / "restored.db"
    restored.write_bytes(payload)
    conn = sqlite3.connect(restored)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM dishes")]
    finally:
        conn.close()
    assert names == ["清蒸鲈鱼", "番茄炒蛋"]

    # 3) 不存在的库要**跳过并在清单里标出来**，而不是让整个备份失败
    missing = next(e for e in manifest["files"] if e["name"] == "recipe_images.db")
    assert missing.get("missing") is True


def test_backup_does_not_touch_the_source_database(tmp_path: Path) -> None:
    """备份是**只读**的：源库的字节与 mtime 都不该被改。"""
    db = tmp_path / "pet.db"
    _make_db(db)
    before = (db.read_bytes(), db.stat().st_mtime_ns)

    backup(tmp_path / "out", db_path=db, image_path=tmp_path / "none.db")

    after = (db.read_bytes(), db.stat().st_mtime_ns)
    assert before == after, "备份不该改动源库"


def test_start_script_exists_and_mentions_the_three_things_people_forget() -> None:
    """启动脚本必须覆盖三件最容易忘的事（venv / 依赖 / .env）。"""
    script = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert "venv" in script
    assert "pip install" in script
    assert ".env.example" in script
    assert "uvicorn" in script


def test_the_app_writes_a_log_file_under_data() -> None:
    """日志要落到 data/logs/app.log —— 关掉窗口后还能查"昨天为什么失败"。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "RotatingFileHandler" in source
    assert "data" in source and "logs" in source
    assert "app.log" in source
    # 打不开也不能崩（和 storage 那层的取舍一致）
    assert "except OSError" in source


def test_backup_hashes_match_even_if_the_database_changes_midway(
    tmp_path: Path, monkeypatch
) -> None:
    """备份期间库被写过，清单里的哈希也必须和包里字节一致（第 179 步，评价13 N-1）。

    真事：脚本以前 `_snapshot()` 调**两次** —— 第一次算清单 sha256，第二次写进 zip。
    两次之间只要库有写入（跑着的服务、WAL checkpoint），"清单说 A、包里是 B"，
    而这个清单的全部意义就是"备份坏了要能发现"。

    复现手法：包一层 `_snapshot`，在**第一次**调用之后往库里写一行 —— 真实场景里
    就是"备份正在进行时服务还在写"。修好之后只会 snapshot 一次，所以写入不会影响一致性。
    """
    import sqlite3

    from scripts import backup_data

    db_path = tmp_path / "pet.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE t (v INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()

    original = backup_data._snapshot
    calls = {"n": 0}

    def counting_snapshot(path):
        payload = original(path)
        if Path(path).name != "pet.db":
            return payload          # 另一个目标是 recipe_images.db，不参与这条断言
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次快照之后写入：旧代码的第二次快照会拿到不同字节
            writer = sqlite3.connect(db_path)
            writer.execute("INSERT INTO t VALUES (2)")
            writer.commit()
            writer.close()
        return payload

    monkeypatch.setattr(backup_data, "_snapshot", counting_snapshot)

    archive = backup_data.backup(out_dir=tmp_path / "backups", db_path=db_path)

    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        payload = bundle.read("pet.db")
    entry = next(e for e in manifest["files"] if e["name"] == "pet.db")

    assert hashlib.sha256(payload).hexdigest() == entry["sha256"], (
        "清单里的 sha256 和包里的字节不一致 —— 又回到了二次快照的老问题"
    )
    assert calls["n"] == 1, f"_snapshot 被调了 {calls['n']} 次，应当只有 1 次"


def test_start_script_is_saved_as_utf8_with_bom():
    """启动脚本必须带 UTF-8 BOM（第 179 步踩过的坑）。

    Windows PowerShell 5.1 **没有 BOM 就按 ANSI（中文系统是 GBK）读** ——
    脚本里的中文注释会被拆成乱码，曾经因此报出
    「字符串缺少终止符 / 语句块缺少右 }」这种莫名其妙的语法错误。
    """
    raw = (ROOT / "scripts" / "start.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        "start.ps1 必须以 UTF-8 BOM 开头，否则 PowerShell 5.1 会把中文读成乱码"
    )
    text = raw.decode("utf-8-sig")
    # -Prod：日常开发带 --reload，长期运行不带
    assert "$Prod" in text
    assert "--reload" in text
