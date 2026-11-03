"""跑 pytest，并把失败用例变成 GitHub Actions 的 `::error` annotation。

**为什么需要这个壳**（第 277 步）：CI 的 run/job 日志要仓库 admin 权限才读得到
（第 274 步已经踩过一次，见 docs/DECISIONS.md）。`check_frontend.py` 现在会自己打
`::error`，pytest 那一步不会——一旦它红了，Actions 页面上只剩一句
"Process completed with exit code 1."，又得靠猜。

做法：用 `--junitxml` 留一份机器可读的结果，跑完以后把每个 failure/error 翻成一行
`::error file=…,line=…::<用例> <消息>`。只在 `GITHUB_ACTIONS=true` 时打印，
本机跑出来的输出保持不变。

退出码永远是 pytest 自己的退出码：这个壳不许把一个红的套件变成绿。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_NAME = "pytest-report.xml"

# 消息压成单行是有原因的：annotation 的 message 里换行会让 Actions 页面上只剩第一行。
MESSAGE_LIMIT = 300


def report_path() -> Path:
    """报告放哪儿：CI 放 RUNNER_TEMP（不进工作树），本机放系统临时目录。"""
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / REPORT_NAME


def parse_failures(xml_text: str) -> list[str]:
    """从 junit xml 里挑出失败的用例，拼成 `文件:行::用例 消息`。

    单独拆出来是为了能在本机验证：这台机器上 pytest 跑不起来（删不掉的临时目录，
    WinError 5），字符串进、列表出这种纯函数才测得到。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found: list[str] = []
    for case in root.iter("testcase"):
        bad = None
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                bad = node
                break
        if bad is None:
            continue
        path = case.get("file") or "?"
        line = case.get("line") or "0"
        name = "::".join(part for part in (case.get("classname"), case.get("name")) if part)
        message = " ".join((bad.get("message") or "").split())[:MESSAGE_LIMIT]
        found.append(f"{path}:{line}::{name} {message}".strip())
    return found


def main(argv: list[str]) -> int:
    report = report_path()
    command = [
        sys.executable, "-m", "pytest", "-q", "--tb=short",
        f"--junitxml={report}", *argv,
    ]
    code = subprocess.run(command, cwd=ROOT).returncode

    if os.environ.get("GITHUB_ACTIONS") == "true" and report.exists():
        failures = parse_failures(report.read_text(encoding="utf-8", errors="replace"))
        for item in failures:
            print(f"::error title=pytest::{item}")
        if failures:
            print(f"::error title=pytest::共 {len(failures)} 条失败（pytest 退出码 {code}）")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
