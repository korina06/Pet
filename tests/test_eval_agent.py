"""评测器自测：所有场景都过、覆盖五类失败、场景文件格式正确。

评测脚本自己也要被验，否则它能在场景全挂时静默退出 0。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_FILE = PROJECT_ROOT / "tests" / "golden" / "agent_scenarios.json"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def _scenarios() -> list[dict]:
    return json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))["cases"]


def test_every_scenario_passes():
    """一条命令跑完所有场景，退出码必须是 0。

    刻意不用 `capsys`：项目自研的 runner 没有这个 fixture，
    用了就会变成"只被 pytest 覆盖"（而它恰恰是 CI 里最该跑的一条）。
    """
    import io
    from contextlib import redirect_stdout

    import eval_agent

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = eval_agent.main([])
    output = buffer.getvalue()

    assert code == 0, "有场景没过：\n" + output
    # 第 200 步（评价15 T3-13）：场景从 10 条扩到 32 条，这里不再写死分母 ——
    # 写死就得每加一条改一次测试，改到最后没人愿意加场景（守卫反而劝退）。
    # 改成动态：成功率必须是 100%，且总数不能低于下限。
    total = len(_scenarios())
    assert f"任务成功率 {total}/{total}（100%）" in output, output
    assert total >= 30, f"场景只有 {total} 条，低于评价15 要求的 30+"


def test_scenarios_cover_the_five_categories_from_the_review():
    """评价7 点名的五类必须在场景集里：成功、工具失败、模型失败、步数耗尽、参数错误。"""
    ids = {case["id"] for case in _scenarios()}

    assert any("no-tool" in case_id or "one-tool" in case_id for case_id in ids), "缺少成功路径"
    assert any("tool-error" in case_id for case_id in ids), "缺少工具失败"
    assert any("model-failure" in case_id for case_id in ids), "缺少模型失败"
    assert any("step-exhaustion" in case_id for case_id in ids), "缺少步数耗尽"
    assert any("bad-args" in case_id for case_id in ids), "缺少参数错误"


def test_scenario_file_is_well_formed():
    cases = _scenarios()

    assert len(cases) >= 30, f"场景太少（{len(cases)} 条），覆盖率对不上评价15 的 30+ 目标"
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), f"id 有重复：{ids}"
    for case in cases:
        assert case.get("expect"), f"{case['id']} 没有任何断言 —— 那它永远不会失败"
        assert case.get("script"), f"{case['id']} 没有模型脚本"
        assert case.get("note"), f"{case['id']} 没写它想验证什么"


def test_scenarios_cover_the_dimensions_added_in_review15():
    """评价15 T3-13 扩的那几个维度，每一条都得留在场景集里 —— 少一条就等于悄悄丢了守卫。

    为什么单独列一张表：`test_every_scenario_passes` 只能证明"现有的都过"，
    证明不了"该测的还在"。少了场景它照样绿。
    """
    cases = {case["id"]: case for case in _scenarios()}

    def expect_of(case_id: str) -> dict:
        assert case_id in cases, f"场景丢了：{case_id}"
        return cases[case_id]["expect"]

    # ① 并发执行，但回灌顺序按模型给的顺序
    parallel = expect_of("k-parallel-two-tools-keep-model-order")
    assert parallel["tools"] == ["weather", "lookup"], "两个工具的顺序不许被并发打乱"
    assert "observe" in parallel["states"], "每个工具都要有独立的 observe 事件"

    # ② 并发的真实收益：耗时上限钉住（串行会超）
    assert expect_of("k-slow-tools-run_concurrently")["max_ms"] <= 400, "并发退化时这条必须红"

    # ③ 多轮历史：第二轮调用必须带着第一轮的问答
    assert expect_of("k-two-turns-history")["messages_in_second_call_min"] >= 3

    # ④ 工具失败后能换路继续（不是终点）
    assert expect_of("k-tool-error-then-recover")["second_call_sees_failure"] is True

    # ⑤ 模型幻觉出的工具名要回灌「不存在」并继续
    assert expect_of("k-unknown-tool-then-valid")["second_call_sees_failure"] is True

    # ⑥ 预算烧超要在"下一步之前"拦住，且不走到最终回答
    budget = expect_of("k-budget-exhausted-after-tool-round")
    assert budget["model_calls"] == 1 and "token 预算" in budget["final_contains"]

    # ⑦ 最终回答不许泄漏异常细节
    assert "RuntimeError" in expect_of("k-final-answer-does-not-leak-exception")["final_not_contains"]

@pytest.mark.skipif(
    not (PROJECT_ROOT / "docs" / "EVAL.md").is_file(),
    reason="docs/EVAL.md 是私人笔记，第 270 步起不进仓库（本机没有就跳过）",
)
def test_the_three_eval_layers_are_documented_where_people_look():
    """文档不是装饰：`docs/EVAL.md` 必须说清三层各管什么，否则「100%」照样会被读歪。"""
    doc = (PROJECT_ROOT / "docs" / "EVAL.md")
    assert doc.is_file(), "没有 docs/EVAL.md —— 评测怎么读就只存在于聊天记录里"
    text = doc.read_text(encoding="utf-8")
    for name in ("eval_agent.py", "eval_retrieval.py", "e2e_real.py"):
        assert name in text, f"文档没提 {name}"
    assert "契约" in text and "人工抽检" in text
    assert "不代表" in text or "不是" in text, "必须写明契约测试不等于模型质量"
