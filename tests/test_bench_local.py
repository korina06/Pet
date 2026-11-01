"""本地基准的验收：能跑、报告可信、数字是机器可读的。不验具体毫秒数——换台机器就会变。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_benchmark_runs_quickly_and_reports_percentiles():
    """基准脚本要能在 CI 里几秒跑完，且报告里带 p50/p95 与"mock"声明（第 120 步）。

    没有"mock 声明"的话，README 里那张表会被误读成真实延迟 —— 这是最容易犯的错。
    """
    import io
    from contextlib import redirect_stdout

    import bench_local

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = bench_local.main(["--quick"])
    output = buffer.getvalue()

    assert code == 0, output
    for expected in ("p50", "p95", "mock", "不含真实网络延迟"):
        assert expected in output, f"报告里缺少 {expected!r}：\n{output}"
    for scenario in ("出行流水线", "菜谱检索", "地理编码缓存", "Agent 循环"):
        assert scenario in output, f"缺少场景 {scenario}：\n{output}"


def test_json_output_is_machine_readable():
    import io
    import json
    from contextlib import redirect_stdout

    import bench_local

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        bench_local.main(["--quick", "--json"])
    rows = json.loads(buffer.getvalue())

    assert len(rows) == 4
    for row in rows:
        assert {"scenario", "n", "p50", "p95", "max"} <= set(row)
        assert row["n"] >= 1
