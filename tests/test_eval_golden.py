"""评测集本身可信：dev 与 holdout 没有重名、每条都有标注、P@5 的分母是实际给出的条数。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_PATH = ROOT / "tests" / "golden" / "retrieval_golden.json"
HOLDOUT_PATH = ROOT / "tests" / "golden" / "retrieval_holdout.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_holdout_and_dev_are_really_separate():
    """两份用例不能有重叠：重叠了就等于 holdout 被"看过"。"""
    dev = _load(DEV_PATH)
    hold = _load(HOLDOUT_PATH)

    assert hold.get("split") == "holdout"

    dev_ids = {case["id"] for case in dev["cases"]}
    hold_ids = {case["id"] for case in hold["cases"]}
    assert not (dev_ids & hold_ids), f"id 重叠：{dev_ids & hold_ids}"

    dev_queries = {case["query"] for case in dev["cases"]}
    hold_queries = {case["query"] for case in hold["cases"]}
    assert not (dev_queries & hold_queries), (
        f"同一句问法两边都有：{dev_queries & hold_queries}"
    )


def test_every_holdout_case_has_human_labels_and_correct_args():
    """holdout 的每一条都必须有：人工 expect、正确的工具参数、可判定的约束。"""
    hold = _load(HOLDOUT_PATH)
    cases = hold["cases"]

    assert len(cases) >= 6, f"holdout 只有 {len(cases)} 条，样本太少没有意义"

    for case in cases:
        for key in ("id", "query", "args", "expect", "must", "expect_all"):
            assert key in case, f"{case.get('id', '?')} 缺少字段 {key}"
        assert case["expect"], f"{case['id']} 的 expect 不能为空（那还怎么算命中）"
        assert case["args"], f"{case['id']} 的 args 不能为空（那是「正确调用」的答案）"
        assert isinstance(case["expect_all"], bool)


def test_precision_denominator_is_the_returned_results_not_the_label_count():
    """P@5 的分母必须是**实际给出的条数**（这是第 75 步的自我修正）。

    写成 `min(5, len(expect))` 时：`expect` 只有 1 道菜的用例，
    命中 1 条就算 P@5 = 1.00 —— 看起来完美，其实另外 4 条全不合格。
    改成 `len(top)` 之后，那两条"负向口味"用例当场掉到 0.20 / 0.40。
    """

    source = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")

    # 只看**代码行**：源码注释里正是引用了那句错误写法做解释，
    # 全文查会把"说明以前为什么错"也判成违规（这个坑我在第 71 步踩过一次）。
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert "len(hits) / len(top)" in code, "分母应该是实际返回条数"
    assert "min(5, len(expect))" not in code, (
        "不许再用标注条数当分母（会把不合格的结果藏起来）"
    )


def test_holdout_is_reported_without_a_gate():
    """holdout 只报告、不设门槛 —— 首次跑出来的数字就是基线。

    设了门槛，下一次难看时最自然的反应就是"调检索直到变绿"，
    而那样它立刻就不再是 holdout 了。
    """

    source = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")

    assert "_report_holdout" in source
    # holdout 段里不能出现阈值判断
    start = source.index("def _report_holdout")
    end = source.index("def _report(", start)
    segment = source[start:end]
    assert "THRESHOLDS" not in segment, "holdout 不该有任何门槛"
    assert "return 1" not in segment, "holdout 不该让脚本失败"


def test_eval_reports_a_current_behavior_column_for_the_holdout():
    """holdout 必须同时给"冻结基线"和"当前行为"两列（第 82 步）。

    评价2 指出：第 77 步加了 exclude_tags 之后，冻结集的 args 仍不含它，
    所以 `h-sichuan-mild` 的 0.20 是**旧能力基线**，读者会以为现在还是这么差。
    解法不是去改冻结集（那就毁了它），而是**另跑一列**当前行为。
    """
    source = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")

    assert "extract_negative_taste" in source, "当前行为那列要和服务端用同一个抽取器"
    assert 'extra={"exclude_tags": injected}' in source, "要把抽到的否定口味注入再跑一次"
    assert "当前 P@5" in source, "报告里得真的印出来"
    assert 'print("⚠️ 这一节的 recall/MRR 分母由 `must` 机械算出' in source, (
        "dev 那一节要标明是自洽性下界，不能被当成质量结论引用"
    )


def test_args_comparison_is_field_wise_not_exact_equality():
    """模型参数评测按**字段**判，不是整份精确相等（第 86 步 / B2）。

    以前 list 里的顺序不同就算错、`None` 和 `[]` 不等价、多传一个参数也罚，
    于是那个"参数正确率"只能当噪声看。
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "eval_retrieval", Path(__file__).resolve().parent.parent / "scripts" / "eval_retrieval.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    match = module._args_match

    # 顺序无关
    assert match({"tags": ["辣", "下饭"]}, {"tags": ["下饭", "辣"]})[0] is True
    # 缺失当空集
    assert match({"tags": ["辣"]}, {})[0] is False, "少了标签要算错"
    assert match({"tags": []}, {})[0] is True, "期望空集、实际没传 —— 等价"
    # 多余字段不罚
    assert match({"tags": ["辣"]}, {"tags": ["辣"], "keyword": "随便"})[0] is True
    # 数值字段
    assert match({"max_minutes": 20}, {"max_minutes": 20})[0] is True
    assert match({"max_minutes": 20}, {"max_minutes": 15})[0] is False
    assert match({"max_minutes": 20}, {})[0] is False
    # 文本字段去空白
    assert match({"keyword": "鱼"}, {"keyword": " 鱼 "})[0] is True
    # 根本没调工具
    assert match({"tags": ["辣"]}, None)[0] is False
    assert "没有调用" in match({"tags": ["辣"]}, None)[1]


V2_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v2.json"


def test_holdout_v2_exists_and_is_separate_from_v1():
    """v2 与 v1/dev 都不能有重叠（第 87 步 / B1）。

    v2 是**另开的一份**：它允许用后来才有的能力（exclude_tags）、也允许修正
    v1 标错的地方；v1 保持原样，因为它记录的是"还没有那个能力时"的基线。
    """
    dev = _load(DEV_PATH)
    v1 = _load(HOLDOUT_PATH)
    v2 = _load(V2_PATH)

    assert v2.get("split") == "holdout_v2"

    v2_ids = {case["id"] for case in v2["cases"]}
    for other, label in ((dev, "dev"), (v1, "v1")):
        overlap = v2_ids & {case["id"] for case in other["cases"]}
        assert not overlap, f"v2 与 {label} 的 id 重叠：{overlap}"


def test_holdout_v2_exercises_the_new_capability():
    """v2 里必须有用例用到 exclude_tags —— 否则它就衡不到第 77 步那件事。"""
    v2 = _load(V2_PATH)

    users = [case["id"] for case in v2["cases"] if "exclude_tags" in case["args"]]
    assert users, "v2 至少要有一条用 exclude_tags 的用例"


def test_holdout_v2_documents_cases_whose_labels_cannot_be_complete():
    """完整性拿不准的用例必须 `expect_all: false`，只算 hit@5。

    「想吃鸡」这种问法会让「鸡蛋」「鸡汤底」的菜也算命中，而用户通常指的是鸡肉 ——
    这时候标 `expect_all: true` 就是在声称一个不存在的"全部应得答案"。
    """
    v2 = _load(V2_PATH)

    partial = [case for case in v2["cases"] if case["expect_all"] is False]
    assert partial, "至少要有用例演示「标注只能举例」这种情况"
    for case in partial:
        assert "举例" in case.get("note", "") or "完整" in case.get("note", ""), (
            f"{case['id']} 标了 expect_all=false，note 里要说明为什么"
        )


def test_eval_loads_three_splits_and_warns_about_v2():
    """评测脚本要读三份，并且对 v2 明确写"不能当泛化证据"。

    v2 是看过实现之后写的，它的满分只说明能力覆盖到位 —— 报告里不写这句，
    读者一定会把它当成绩。
    """
    source = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")

    assert "HOLDOUT_V2_PATH" in source
    assert '"holdout_v2"' in source
    assert "不能当泛化证据" in source


def test_production_wiring_builds_all_three_feature_registries():
    """`main.py` 必须真的建三套 —— 否则线上会静默退化成共用一个工具面。

    回退分支（`feature_registry_for` 找不到就退回 `registry`）是为测试留的，
    它一出声就说明有人删了装配。
    """
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "build_feature_registry" in source
    for allow_list in ("CHAT_TOOLS", "TRAVEL_TOOLS", "RECIPE_TOOLS"):
        assert allow_list in source, f"main.py 没有用 {allow_list} 装配"
    for attr in ("app.state.registry", "app.state.travel_registry", "app.state.recipe_registry"):
        assert attr in source, f"main.py 没有挂 {attr}"


# 注意：`test_the_fallback_helper_prefers_the_feature_registry` 已挪到
# `tests/test_tool_registry.py`（第 137 步）。它 import `tools.registry` →
# `core.config` → pydantic_settings，而本模块在 check_frontend 的"纯标准库"组里，
# 放在这里会让没有 pydantic 的解释器整组报 ModuleNotFoundError。
