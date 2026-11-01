"""用量报告命令行：能跑通、表里有内容、`--prompt` 过滤生效。

补的是"只测了库、没测入口"留下的空档。
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

TEMP_ROOT = PROJECT_ROOT / ".testtmp"


def _db_with_rows(name: str):
    """在 .testtmp 下造一个小库并塞两条账（不同提示词版本）。"""
    from core import usage
    from core.storage import Storage

    TEMP_ROOT.mkdir(exist_ok=True)
    path = TEMP_ROOT / f"usage-cli-{name}.db"
    if path.exists():
        path.unlink()
    storage = Storage(path)
    storage.apply_migrations()

    usage.record(
        storage, provider="deepseek", model="deepseek-chat", feature="chat",
        session_id="s1", turn_id="t1", step=1, stream=False,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        prompt_version="aaaaaaaa",
    )
    usage.record(
        storage, provider="deepseek", model="deepseek-chat", feature="recipe",
        session_id="s2", turn_id="t2", step=1, stream=True,
        usage={"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
        prompt_version="bbbbbbbb",
    )
    return storage, path


def _run(argv: list[str], db_path: Path) -> tuple[int, str]:
    import usage_report

    previous = os.environ.get("PET_DB_PATH")
    os.environ["PET_DB_PATH"] = str(db_path)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = usage_report.main(argv)
    finally:
        if previous is None:
            os.environ.pop("PET_DB_PATH", None)
        else:
            os.environ["PET_DB_PATH"] = previous
    return code, buffer.getvalue()


def test_the_cli_runs_and_reports_totals():
    """这条如果红了，说明"账本有数据但命令跑不出来"——第 114 步就是这样。"""
    storage, path = _db_with_rows("basic")
    try:
        code, output = _run(["--days", "3"], path)
    finally:
        storage.close()

    assert code == 0, output
    assert "chat" in output and "recipe" in output, output
    assert "合计" in output, output
    assert "120" in output or "460" in output, f"数字没印出来：\n{output}"


def test_the_cli_can_filter_one_prompt_version():
    storage, path = _db_with_rows("prompt")
    try:
        code, output = _run(["--days", "7", "--prompt", "aaaaaaaa"], path)
    finally:
        storage.close()

    assert code == 0, output
    assert "chat" in output, output
    assert "recipe" not in output, f"--prompt 没起作用：\n{output}"


def test_the_cli_says_so_when_the_db_is_missing():
    missing = TEMP_ROOT / "definitely-missing.db"
    if missing.exists():
        missing.unlink()
    code, output = _run([], missing)
    assert code == 1
    assert "库不存在" in output, output


def test_stats_prints_distributions_and_suggests_thresholds():
    """第 210 步：`--stats` 要把分布和阈值建议印出来，且样本少时必须明说。"""
    storage, path = _db_with_rows("stats")
    try:
        code, output = _run(["--days", "7", "--stats"], path)
    finally:
        storage.close()

    assert code == 0, output
    assert "分布" in output, output
    assert "TURN_TOKEN_BUDGET" in output, output
    assert "DAILY_TOKEN_WARN" in output, output
    assert "P95" in output, output
    # 两个样本不算依据 —— 这句话本身也得被钉住，否则以后又会拿三个 probe 调用定预算
    assert "样本太少" in output, output


def test_stats_works_on_an_empty_window():
    """窗口里一条都没有时，不该崩，也不该编出阈值。"""
    storage, path = _db_with_rows("stats-empty")
    try:
        code, output = _run(["--days", "1", "--feature", "nope", "--stats"], path)
    finally:
        storage.close()

    assert code == 0, output
    assert "没有记录" in output, output
