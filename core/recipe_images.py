"""菜谱图片库的只读访问。

按菜名归一化后的键关联（id 会随重导入变化），meta 与 BLOB 分开取，库不存在就返回 None。
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_DB_NAME = "recipe_images.db"
#: 环境变量覆盖（测试用，避免碰到真库）
IMAGE_DB_ENV = "PET_IMAGE_DB"


def resolve_image_db_path(explicit: str | Path | None = None) -> Path:
    """图片库位置：显式参数 > 环境变量 > `<项目>/data/recipe_images.db`。"""
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(IMAGE_DB_ENV)
    if from_env:
        return Path(from_env)
    return Path(__file__).resolve().parent.parent / "data" / IMAGE_DB_NAME


class ImageStore:
    """一小层只读封装：按菜名取图片与元信息。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(
            f"file:{self._path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row

    @property
    def path(self) -> Path:
        return self._path

    def count(self) -> int:
        return int(self._conn.execute("SELECT count(*) FROM image_meta").fetchone()[0])

    def meta(self, recipe_name: str) -> dict[str, Any] | None:
        """这张菜有没有图（**不读 BLOB**）。"""
        from core.recipe_library import normalize_name_key

        row = self._conn.execute(
            "SELECT name_key, name, format, mime, width, height, bytes "
            "FROM image_meta WHERE name_key = ?",
            (normalize_name_key(recipe_name),),
        ).fetchone()
        return dict(row) if row else None

    def get(self, recipe_name: str) -> dict[str, Any] | None:
        """取图片本体 + 元信息；没有就返回 None。"""
        meta = self.meta(recipe_name)
        if meta is None:
            return None
        row = self._conn.execute(
            "SELECT data FROM image_blob WHERE name_key = ?", (meta["name_key"],)
        ).fetchone()
        if row is None or not row["data"]:
            return None
        data = bytes(row["data"])
        return {
            **meta,
            "data": data,
            # 内容哈希做 ETag：图不变，浏览器就不会重复下载
            "etag": hashlib.md5(data).hexdigest(),
        }

    def close(self) -> None:
        self._conn.close()


def open_image_store(path: str | Path | None = None) -> ImageStore | None:
    """打开图片库；**文件不存在或打不开时返回 None**（不抛异常）。

    少一套资产不该让整个应用起不来——详情页只是不显示图而已。
    """
    target = resolve_image_db_path(path)
    if not target.exists():
        logger.info("没有菜谱图片库（%s），详情页将不显示图片", target)
        return None
    try:
        return ImageStore(target)
    except Exception as exc:  # noqa: BLE001 —— 库坏了也不该拖垮启动
        logger.warning("打开菜谱图片库失败（%s）：%s", target, exc)
        return None
