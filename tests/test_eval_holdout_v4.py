"""holdout v4 的守卫：15 条有答案 + 15 条真零命中，锁结构与 id 唯一，改期望值必须留下足迹。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

V4_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v4.json"


def _load() -> dict:
    return json.loads(V4_PATH.read_text(encoding="utf-8"))


def test_v4_has_thirty_cases_split_in_half() -> None:
    """30 条：15 条有答案 + 15 条真零命中（失败率的分母才够用）。"""
    data = _load()
    cases = data["cases"]
    zero = [case for case in cases if case.get("expect_zero")]
    semantic = [case for case in cases if not case.get("expect_zero")]

    assert data["split"] == "holdout_v4"
    assert len(cases) == 30, f"应当是 30 条，实际 {len(cases)}"
    assert len(semantic) == 15, f"有答案的应当是 15 条，实际 {len(semantic)}"
    assert len(zero) == 15, f"零命中的应当是 15 条，实际 {len(zero)}"


def test_v4_ids_are_unique_and_expects_are_consistent() -> None:
    cases = _load()["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "id 重复会让报告对不上号"

    for case in cases:
        if case.get("expect_zero"):
            assert case.get("expect") == [], f"{case['id']} 标了零命中却给了期望答案"
        else:
            assert case.get("expect"), f"{case['id']} 有答案却没有 expect"


def test_v4_every_case_carries_a_keyword() -> None:
    """每条都要有 `keyword`：空 keyword 会让工具把整库当候选（v3 踩过）。"""
    for case in _load()["cases"]:
        keyword = (case.get("args") or {}).get("keyword")
        assert isinstance(keyword, str) and keyword.strip(), f"{case['id']} 缺 keyword"


def test_v4_is_not_loaded_by_default() -> None:
    """默认评测不读 v4 —— 盲测集的答案不该出现在日常输出里。"""
    from scripts.eval_retrieval import _load_cases

    default_splits = {case["split"] for case in _load_cases()}
    assert "holdout_v4" not in default_splits

    with_v4 = {case["split"] for case in _load_cases(include_v4=True)}
    assert "holdout_v4" in with_v4
