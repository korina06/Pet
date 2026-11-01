"""holdout v3 的守卫：锁结构与零命中条数，并确认它默认不参与常规评测（否则答案会打在屏幕上）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

V3_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v3.json"


def _load() -> dict:
    return json.loads(V3_PATH.read_text(encoding="utf-8"))


def test_v3_has_both_halves_and_enough_zero_cases() -> None:
    """有答案的 ≥10 条、真零命中 ≥35 条（评价15 要求「专门放 35 条真零命中」）。"""
    data = _load()
    cases = data["cases"]
    zero = [case for case in cases if case.get("expect_zero")]
    semantic = [case for case in cases if not case.get("expect_zero")]

    assert data["split"] == "holdout_v3"
    assert len(semantic) >= 10, f"有答案的用例只有 {len(semantic)} 条"
    assert len(zero) >= 35, f"真零命中的用例只有 {len(zero)} 条"


def test_v3_ids_are_unique_and_zero_cases_have_empty_expect() -> None:
    cases = _load()["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "id 重复会让报告对不上号"

    for case in cases:
        if case.get("expect_zero"):
            assert case.get("expect") == [], f"{case['id']} 标了零命中却给了期望答案"


def test_v3_every_case_carries_a_keyword() -> None:
    """每条都要有 `keyword`：空 keyword 会让工具把整库当候选（第 189 步踩过）。

    第一版 v3 的 args 全是 {}，结果 37 条零命中用例返回同一批 6 道菜 ——
    那不是检索能力问题，是评测没按真实调用方式喂参数。
    """
    for case in _load()["cases"]:
        keyword = (case.get("args") or {}).get("keyword")
        assert isinstance(keyword, str) and keyword.strip(), f"{case['id']} 缺 keyword"


def test_v3_is_not_loaded_by_default() -> None:
    """默认评测不读 v3 —— 盲测集的答案不该出现在日常输出里。"""
    from scripts.eval_retrieval import _load_cases

    default_splits = {case["split"] for case in _load_cases()}
    assert "holdout_v3" not in default_splits

    with_v3 = {case["split"] for case in _load_cases(include_v3=True)}
    assert "holdout_v3" in with_v3
