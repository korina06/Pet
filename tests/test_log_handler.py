"""`app.log` 的 handler 只挂一次；轮转失败也只记一行，不许刷成一大片日志错误。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_main(name: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "main.py")
    assert spec is not None and spec.loader is not None, "找不到 main.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_log_handlers():
    return [
        handler
        for handler in logging.getLogger().handlers
        if str(getattr(handler, "baseFilename", "")).endswith("app.log")
    ]


def test_importing_main_twice_does_not_duplicate_the_file_handler():
    before = len(_app_log_handlers())

    _load_main("pet_desktop_main_for_log_once_a")
    after_first = len(_app_log_handlers())
    _load_main("pet_desktop_main_for_log_once_b")
    after_second = len(_app_log_handlers())

    assert after_first - before <= 1, "第一次导入最多挂一个"
    assert after_second == after_first, (
        f"第二次导入又挂了一个 handler（{after_first} → {after_second}），"
        "轮转时会互相抢文件"
    )


def test_rotation_failure_is_swallowed(tmp_path: Path):
    """文件被另一个句柄占着时，轮转失败**不能**抛出（也不许刷 traceback）。"""
    module = _load_main("pet_desktop_main_for_rotation")
    handler_class = module._TolerantRotatingFileHandler

    log_path = tmp_path / "app.log"
    log_path.write_text("x" * 10, encoding="utf-8")

    handler = handler_class(log_path, maxBytes=16, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("pet_desktop_rotation_probe")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False

    try:
        # 另一个句柄开着同一个文件：Windows 上 rename 会 WinError 32
        with log_path.open("r", encoding="utf-8") as busy:
            assert busy.read() == "x" * 10
            for index in range(6):
                logger.info("第 %s 行，够把 maxBytes 顶穿了", index)
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert log_path.exists(), "轮转失败也要保留原文件"
