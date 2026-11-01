"""检索质量回归：golden set 不达标就红 —— 这是唯一能发现「改着改着检索变差」的机制。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import eval_retrieval  # noqa: E402


def test_golden_file_is_well_formed():
    """用例本身要合法：id 唯一、args 是对象、must 非空（零命中用例除外）。"""
    payload = json.loads(eval_retrieval.GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert len(cases) >= 20, f"用例太少（{len(cases)}），覆盖不住主要场景"
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "用例 id 有重复"

    for case in cases:
        assert case.get("query"), f"{case['id']} 缺 query"
        assert isinstance(case.get("args"), dict), f"{case['id']} 的 args 不是对象"
        if not case.get("expect_zero"):
            assert case.get("must"), f"{case['id']} 既不是零命中又没有 must"


def test_golden_covers_the_five_failure_modes():
    """覆盖度：历史踩过的坑必须都在用例里。

    - 主料别名（鸡肉→三黄鸡）
    - 假朋友（鸡蛋≠鸡、鱼香≠鱼）
    - 跨维度「与」（辣 + 粤菜）
    - 时间上下限
    - 零命中 / 放宽
    """
    payload = json.loads(eval_retrieval.GOLDEN_PATH.read_text(encoding="utf-8"))
    ids = {case["id"] for case in payload["cases"]}
    for required in (
        "spicy-chicken",      # 口味 + 主料组合
        "chicken-not-egg",    # 假朋友：鸡蛋≠鸡
        "light-fish",         # 假朋友：鱼香≠鱼
        "relax-spicy-cantonese",  # 跨维度「与」+ 放宽说明
        "under-30",
        "over-30-slow",
        "zero-truffle",
    ):
        assert required in ids, f"缺少用例：{required}"


def test_retrieval_quality_meets_the_thresholds():
    """跑一遍 golden set：正确性三条必须 100%，recall/MRR 要过门槛。"""
    code = eval_retrieval.evaluate(verbose=False)
    assert code == 0, "检索质量未达标（详见 scripts/eval_retrieval.py 的输出）"
