"""负载测试脚本：能跑、报告写明客户端与服务同机且模型是替身、给出事件循环延迟与据此的判断。
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_the_load_test_runs_and_explains_its_limits():
    import load_test

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = load_test.main(["--quick"])
    output = buffer.getvalue()

    assert code == 0, output
    assert "客户端与服务都在本机" in output, "必须写明测的是什么"
    assert "替身" in output, "必须写明模型/外部 API 是替身"
    for expected in ("RPS", "p50", "p95", "loop lag"):
        assert expected in output, f"报告里缺少 {expected}"
    assert "事件循环延迟" in output
    assert "asyncio.to_thread" in output, "要给出'要不要改'的判断依据"


def test_the_load_test_reports_json():
    import json

    import load_test

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        load_test.main(["--quick", "--json"])
    rows = json.loads(buffer.getvalue())

    assert len(rows) == 6, f"3 个场景 × 2 档 = 6 行，实际 {len(rows)}"
    for row in rows:
        assert {"scenario", "concurrency", "requests", "failures", "rps", "p50", "p95"} <= set(row)
        assert {"p50", "p95", "max"} <= set(row["loop_lag"])
        assert row["failures"] == 0, row


def test_the_scenarios_cover_the_three_hot_paths():
    import load_test

    names = [scenario["name"] for scenario in load_test._scenarios()]
    assert len(names) == 3, names
    joined = " ".join(names)
    assert "读接口" in joined
    assert "聊天" in joined, "聊天 SSE 是主路径，必须测"
    assert "出行" in joined
