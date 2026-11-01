"""仓库卫生：用户自己的文件（评价稿、素材、计划书）不该被 git 跟踪。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 用户的文件：存在与否都行，但不该被跟踪
USER_FILES = (
    "1.png",
    "recipe_rework_plan.md",
    "set/",
    "评价1.md",
    "评价2.md",
    "评价3.md",
    "评价4.md",
    "评价5.md",
    "评价6.md",
    "评价7.md",
    "错误1.md",
)

GIT = shutil.which("git")


def _tracked(path: str) -> bool:
    """这个路径在 git 里有没有被跟踪（目录用 ls-files 前缀匹配）。"""
    completed = subprocess.run(
        [GIT, "ls-files", "--", path],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return bool((completed.stdout or "").strip())


def test_user_files_are_not_tracked():
    if GIT is None:
        print("    没有 git，跳过")
        return

    offenders = [path for path in USER_FILES if _tracked(path)]
    assert not offenders, (
        "这些是用户的未跟踪文件，不该进代码提交："
        f"{offenders}\n"
        "（第 121 步踩过：一次 `git add -A` 把它们带进去了。"
        "要刻意提交某一份，就把 tests/test_repo_hygiene.py 里对应的行删掉。）"
    )


def test_generated_dirs_are_ignored():
    """运行期产物不该出现在 `git status` 里（.testtmp / backups / .ptmp*）。"""
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in (".testtmp", "backups", ".ptmp"):
        assert pattern in ignored, f".gitignore 没有忽略 {pattern}"
