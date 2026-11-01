"""把 `data/` 下的数据库打成一个带 sha256 清单的 zip。

用 SQLite 的 `backup()` 取快照而不是拷文件；不删旧备份、不做轮转，删除永远交给人。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "backups"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(db_path: Path) -> bytes | None:
    """用 SQLite 的 backup API 拿一份**一致性**快照；库不存在返回 None。

    为什么不用 `read_bytes()`：数据库可能正被写入（WAL 里还有没落盘的页），
    直接复制会得到一个"看起来是 .db、打开却不一致"的文件。
    """
    if not db_path.exists():
        return None
    # 先落到内存库，再序列化 —— 这样不需要临时文件，也不会碰原库
    memory = sqlite3.connect(":memory:")
    try:
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            source.backup(memory)
        finally:
            source.close()
        return memory.serialize()
    finally:
        memory.close()


def _targets(db_path: Path | None, image_path: Path | None) -> list[tuple[str, Path]]:
    """要备份的东西：(zip 内名字, 源路径)。

    只列**数据**：`.env` 里有 API key，不进备份包（它的位置在 README 里写着）。
    """
    if db_path is None or image_path is None:
        from core.recipe_images import resolve_image_db_path
        from core.storage import resolve_db_path

        db_path = db_path or resolve_db_path()
        image_path = image_path or resolve_image_db_path()

    return [
        ("pet.db", Path(db_path)),
        ("recipe_images.db", Path(image_path)),
    ]


def backup(
    out_dir: Path | str = DEFAULT_OUT,
    *,
    db_path: Path | str | None = None,
    image_path: Path | str | None = None,
) -> Path:
    """做一次备份，返回 zip 路径。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = out / f"backup-{stamp}.zip"

    entries: list[dict[str, Any]] = []
    #: 名字 -> 快照字节。**只 snapshot 一次**（第 179 步）：
    #: 以前清单里的哈希取自第一次、写进 zip 的取自第二次；两次之间只要库被写过
    #: （WAL checkpoint、跑着的服务），"清单说 A、包里是 B" —— 而这个清单的全部意义
    #: 就是"备份坏了要能发现"。顺带省掉一次整库进内存的序列化。
    payloads: dict[str, bytes] = {}
    for name, source in _targets(
        Path(db_path) if db_path else None,
        Path(image_path) if image_path else None,
    ):
        payload = _snapshot(source)
        if payload is None:
            entries.append({"name": name, "source": str(source), "missing": True})
            continue
        payloads[name] = payload
        entries.append(
            {
                "name": name,
                "source": str(source),
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "由 scripts/backup_data.py 生成；不含 .env（里面有 API key）",
        "files": entries,
    }

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for entry in entries:
            if entry.get("missing"):
                continue
            # 直接用上面那份快照：和清单里的 sha256 是**同一串字节**
            payload = payloads.get(entry["name"])
            if payload is not None:
                bundle.writestr(entry["name"], payload)
        bundle.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )

    return archive


def describe(archive: Path) -> str:
    """人话报告这次备份包了什么。"""
    lines = [f"备份包：{archive}"]
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        for entry in manifest["files"]:
            if entry.get("missing"):
                lines.append(f"  {entry['name']}: **不存在**，已跳过（{entry['source']}）")
            else:
                lines.append(
                    f"  {entry['name']}: {entry['bytes']:,} 字节  sha256={entry['sha256'][:12]}…"
                )
    lines.append(f"  清单：manifest.json（创建于 {manifest['created_at']}）")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份 data/ 下的 SQLite 数据")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--db", default="", help="覆盖 pet.db 路径")
    parser.add_argument("--images", default="", help="覆盖 recipe_images.db 路径")
    args = parser.parse_args(list(argv) if argv is not None else None)

    archive = backup(
        args.out,
        db_path=args.db or None,
        image_path=args.images or None,
    )
    print(describe(archive))
    return 0


if __name__ == "__main__":
    # 第 103 步：不再替换 sys.stdout（会破坏调用方的捕获流）；原地改编码
    from core.console import force_utf8

    force_utf8()
    raise SystemExit(main())
