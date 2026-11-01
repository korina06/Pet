"""`docs/PERF.md` 的守卫：免责声明必须在，重现命令必须真是仓库里存在的参数。

那份数字是替身模型跑出来的，最容易被当成真模型性能引用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 第 270 步：用户要求仓库只留项目本体，私人文档（含这份）只在本机。
#: 本机没有就跳过，而不是假装通过 —— 「没测」和「测过且对」必须分得开。
NEEDS_DOC = pytest.mark.skipif(
    not (ROOT / "docs/PERF.md").is_file(),
    reason="docs/PERF.md 是私人文档，第 270 步起不进仓库（本机没有就跳过）",
)


def _perf() -> str:
    return (ROOT / "docs/PERF.md").read_text(encoding="utf-8")


def test_perf_doc_exists_and_is_linked_from_readme():
    """README 里必须指到性能数字所在的地方（哪怕那份文档只在本机）。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/PERF.md" in readme, "写了文档但 README 里没有入口，等于没写"


@NEEDS_DOC
def test_perf_doc_keeps_the_fake_model_disclaimer():
    text = _perf()
    assert "替身" in text and "我们的代码" in text, "必须写明这些数字量的是我们的代码"
    assert "DeepSeek 能扛多少" in text, "必须写明清「不回答上游能扛多少」"


@NEEDS_DOC
def test_perf_doc_commands_use_arguments_the_script_really_has():
    """文档里的命令要真能敲（第 206 步给 `load_test.py` 加了 --heavy / --scenarios）。"""
    text = _perf()
    source = (ROOT / "scripts/load_test.py").read_text(encoding="utf-8")

    for flag in ("--heavy", "--scenarios", "--concurrency", "--json"):
        assert flag in text, f"文档里没有 {flag} 的重现命令"
        assert f'"{flag}"' in source, f"{flag} 在脚本里不存在，文档就骗人"


@NEEDS_DOC
def test_perf_doc_has_the_break_point_and_the_cost_ceiling():
    """两个结论必须留在文档里：先崩在哪、以及「扛得住但钱包扛不住」。"""
    text = _perf()
    assert "400" in text and "86.5ms" in text, "崩点（400 并发、loop lag 越过 50ms）要留着"
    assert "钱包" in text, "成本那一节是这份文档存在的另一半理由"
