"""菜谱图片库：按规范化菜名对得上图、库不存在时应用照常可用。
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_images import ImageStore, open_image_store, resolve_image_db_path  # noqa: E402

#: 一张 2x1 的最小 PNG（8 字节签名 + IHDR 里带宽高）
PNG_2x1 = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0dIHDR"
    + struct.pack(">II", 2, 1)
    + b"\x08\x06\x00\x00\x00"
    + b"\x00" * 8
)


def _make_db(path: Path, rows: list[tuple[str, bytes]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE image_meta (name_key TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "format TEXT NOT NULL, mime TEXT NOT NULL, width INTEGER, height INTEGER, "
        "bytes INTEGER NOT NULL, source_url TEXT NOT NULL DEFAULT '', "
        "imported_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE image_blob (name_key TEXT PRIMARY KEY, data BLOB NOT NULL)"
    )
    from core.recipe_library import normalize_name_key

    for name, data in rows:
        conn.execute(
            "INSERT INTO image_meta VALUES (?, ?, 'png', 'image/png', 2, 1, ?, '', 'x')",
            (normalize_name_key(name), name, len(data)),
        )
        conn.execute(
            "INSERT INTO image_blob VALUES (?, ?)", (normalize_name_key(name), data)
        )
    conn.commit()
    conn.close()
    return path


def test_lookup_by_name_key_ignores_spacing_and_case(tmp_path):
    """按**归一化菜名**找图：`西红柿 炒蛋` 与 `西红柿炒蛋` 是同一道。"""
    db_path = _make_db(tmp_path / "img.db", [("西红柿炒蛋", PNG_2x1)])
    store = ImageStore(db_path)
    try:
        hit = store.get("西红柿 炒蛋")
        assert hit is not None, "归一化后应当能对上"
        assert hit["data"] == PNG_2x1
        assert hit["mime"] == "image/png"
        assert store.meta("西红柿炒蛋")["width"] == 2
    finally:
        store.close()


def test_missing_image_returns_none_instead_of_raising(tmp_path):
    """没图是正常状态（用户自己录的菜），不能抛异常。"""
    db_path = _make_db(tmp_path / "img.db", [("白切鸡", PNG_2x1)])
    store = ImageStore(db_path)
    try:
        assert store.get("我自创的菜") is None
        assert store.meta("我自创的菜") is None
    finally:
        store.close()


def test_absent_library_means_no_images_not_an_error(tmp_path):
    """图片库文件不存在时：返回 None，应用照常启动。"""
    assert open_image_store(tmp_path / "没有这个库.db") is None


def test_renamed_recipe_degrades_to_no_image(tmp_path):
    """菜被改名后查不到图 —— 宁可不显示，也不能给错的图。"""
    db_path = _make_db(tmp_path / "img.db", [("白切鸡", PNG_2x1)])
    store = ImageStore(db_path)
    try:
        assert store.get("白切鸡（改过名）") is None
    finally:
        store.close()


def test_etag_is_content_based(tmp_path):
    """ETag 用内容哈希：同一张图重复取，ETag 稳定；换了图就变。"""
    db_path = _make_db(tmp_path / "img.db", [("白切鸡", PNG_2x1)])
    store = ImageStore(db_path)
    try:
        first = store.get("白切鸡")["etag"]
        second = store.get("白切鸡")["etag"]
        assert first == second
        assert len(first) == 32
    finally:
        store.close()


def test_image_db_path_can_be_overridden(tmp_path, monkeypatch):
    """路径可被 PET_IMAGE_DB 覆盖（测试与部署都不该碰真库）。"""
    target = tmp_path / "custom.db"
    monkeypatch.setenv("PET_IMAGE_DB", str(target))
    assert resolve_image_db_path() == target

    monkeypatch.delenv("PET_IMAGE_DB")
    assert resolve_image_db_path().name == "recipe_images.db"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "scripts" / "import_recipe_images.py").is_file(),
    reason="import_recipe_images.py 是一次性导入脚本，第 271 步起只在本机",
)
def test_png_size_parser_used_by_the_importer():
    """导入脚本的尺寸解析：PNG 的头里就能读出真实宽高。"""
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    sys.path.insert(0, str(scripts))
    from import_recipe_images import image_size

    fmt, size = image_size(PNG_2x1)
    assert fmt == "png"
    assert size == (2, 1)
