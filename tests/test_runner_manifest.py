"""守住 `scripts/check_frontend.py` 的模块清单：写进去的模块必须存在、可选模块不许悄悄变成必跑、
活文档不许写死测试条数。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "scripts" / "check_frontend.py"


def _load_runner():
    """按路径加载 runner 模块（它不依赖 fastapi，纯标准库）。"""
    spec = importlib.util.spec_from_file_location("_cf_for_format_check", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: 会被当成「现在的事实」读的文档（README / 规模叙事）。
#: 历史记录（DECISIONS / FAILURES / PERF）里**可以**有当时的数字，因为它们标了步骤；
#: 这几份不行——每次改动都会让数字过期，而读者不会知道它过期了。
#: 第 270 步：`docs/INTERVIEW.md` 是私人文档，不进仓库；文件不在时自动从清单里去掉，
#: 以免「一份不存在的文档」把这条守卫变成永远读不到的假绿。
LIVING_DOCS = tuple(
    name
    for name in ("README.md", "docs/SCALE.md", "docs/INTERVIEW.md")
    if (PROJECT_ROOT / name).is_file()
)


def test_living_docs_do_not_freeze_the_test_count():
    """活文档里不许写死「NNNN 条 / NNNN 通过」。

    第 257 步的教训：README 写 1442、SCALE 写 1452、INTERVIEW 写 1442，
    而当时实际已经 1464 —— 三处数字各自是「写那一刻的快照」，读者却当成现在。
    要么改成不带数字的说法，要么把数字写进带步骤号的历史记录里。
    """
    import re

    pattern = re.compile(r"\b\d{4}\s*(?:条|通过)|(?:通过|全量)[^\n]{0,8}\b\d{4}\b")
    offenders: list[str] = []
    for name in LIVING_DOCS:
        path = PROJECT_ROOT / name
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{name}:{number}: {line.strip()}")

    assert offenders == [], "活文档里写死了会过期的测试数：\n" + "\n".join(offenders)


def test_the_module_lists_have_the_right_shapes():
    runner = _load_runner()

    modules = list(getattr(runner, "MODULES", []))
    optional = list(getattr(runner, "OPTIONAL_MODULES", []))

    for entry in modules:
        assert isinstance(entry, str), (
            f"MODULES 里只许放字符串，这条是 {type(entry).__name__}：{entry!r}"
        )

    for entry in optional:
        assert isinstance(entry, tuple) and len(entry) == 2, (
            f"OPTIONAL_MODULES 的每条必须是 (依赖元组, 名字)，这条是：{entry!r}"
        )
        libs, name = entry
        assert isinstance(libs, tuple), f"依赖要写成元组：{entry!r}"
        assert isinstance(name, str), f"名字要写成字符串：{entry!r}"


def test_no_module_is_listed_twice():
    runner = _load_runner()
    names = list(getattr(runner, "MODULES", [])) + [
        entry[1] for entry in getattr(runner, "OPTIONAL_MODULES", [])
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"这些模块被登记了两次：{duplicates}"
