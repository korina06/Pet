"""CI 配置本身的回归保护：漏跑或写错是静默的，只能靠测试盯住关键约定。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "checks.yml"
REPORTER = ROOT / "scripts" / "pytest_report.py"


def _workflow() -> str:
    assert WORKFLOW.exists(), "CI 配置文件不见了"
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_runs_both_verification_paths():
    """两条验证路都必须跑：项目自己的 runner **和** pytest。

    只跑前者会漏掉带 fixture 的文件（例如 test_deepseek_llm 的 17 条流式解析）；
    只跑后者会让"不装任何第三方库也能验证"这个能力失去守护。
    """
    text = _workflow()

    assert "check_frontend.py" in text, "CI 必须跑项目自己的验证脚本"
    assert "pytest" in text, "CI 必须跑完整套件"


def test_ci_installs_the_pinned_dependencies():
    """CI 必须从 requirements.txt 装依赖，而不是随手装最新的。

    否则"CI 绿"只说明"某天的最新版能过"，本地按 requirements 装出来的
    组合仍然可能不一样。
    """
    text = _workflow()

    assert "requirements.txt" in text
    assert "pip install" in text


def test_pytest_and_its_asyncio_plugin_are_pinned():
    """测试工具本身不能是变量（第 80 步钉死）。

    之前 `pytest>=8.3` / `pytest-asyncio>=0.24` 一直没装过，
    等真装上时依赖解析出来的组合是未知的。
    """
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    # 只看**非注释行**。我第一版写的是「整段不许出现 >=」，结果自己红了：
    # 注释里正在解释"以前一直写 >= "。这类坑这个项目已经踩到第三次
    # （前两次是「线程池」和 min(5, len(expect))）——**别对含注释的文本做子串否定**。
    # 第 156 步：requirements.txt 现在**纯 ASCII、一行一条**，没有中文分段标记了
    # （pip 按本地代码页解码，中文注释会让 `pip install -r` 直接报 UnicodeDecodeError）。
    # 所以这里不再按 "# --- 测试 ---" 切段，而是对整份文件的非注释行做断言。
    deps = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert deps, "requirements.txt 里没有任何依赖（格式变了？）"
    assert all("==" in dep for dep in deps), f"依赖要钉版本，实际：{deps}"
    for name in ("pytest==", "pytest-asyncio=="):
        assert any(dep.startswith(name) for dep in deps), f"缺少钉死的 {name}"


def test_pytest_step_goes_through_the_annotation_wrapper():
    """pytest 那一步必须走壳，不能直接调 `python -m pytest`（第 277 步）。

    理由不是洁癖：CI 的 run/job 日志要仓库 admin 权限才读得到（第 274 步，
    连红 16 次都只能猜）。check_frontend.py 会自己打 `::error`，
    pytest 不会——所以套一层，把失败的用例名送到 Actions 页面和 annotations 接口上。
    """
    text = _workflow()

    assert "pytest_report.py" in text, "pytest 那一步要走 scripts/pytest_report.py"


def _parse(xml_text: str):
    spec = importlib.util.spec_from_file_location("_pytest_report_for_test", REPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_failures(xml_text)


def test_junit_failures_become_annotations():
    """纯函数进 xml、出 `文件:行::用例 消息`。

    只测这个函数、不真跑 pytest：这台开发机上 pytest 起不来
    （临时目录删不掉，WinError 5），字符串进出才验得到。
    """
    xml_text = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="1">
  <testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3" time="0.01"/>
  <testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="9" time="0.02">
    <failure message="assert 1 == 2&#10;more detail">traceback…</failure>
  </testcase>
  <testcase classname="tests.test_b" name="test_boom" file="tests/test_b.py" line="17" time="0.0">
    <error message="RuntimeError: boom">traceback…</error>
  </testcase>
</testsuite></testsuites>"""

    failures = _parse(xml_text)

    assert len(failures) == 2, f"只该报失败的，实际：{failures}"
    assert failures[0].startswith("tests/test_a.py:9::tests.test_a::test_bad "), failures[0]
    assert "assert 1 == 2 more detail" in failures[0], "消息要压成单行（换行会截断 annotation）"
    assert failures[1].startswith("tests/test_b.py:17::tests.test_b::test_boom "), failures[1]


def test_broken_report_is_not_fatal():
    """报告残缺时返回空列表，不能让壳自己崩掉（退出码必须是 pytest 的）。"""
    assert _parse("这不是 xml") == []

