"""检查 `.env.example` 是否覆盖了所有配置项，避免新增设置后样板文件悄悄过期。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _parsed():
    from dotenv import dotenv_values

    return dotenv_values(ENV_EXAMPLE)


def test_every_key_is_a_legal_env_name():
    values = _parsed()
    assert values, "解析出 0 项，模板文件可能坏了"

    illegal = [key for key in values if key is not None and not _KEY.match(key)]
    assert not illegal, f"出现非法 key（多半是没写成注释的说明文字）：{illegal}"


def test_every_non_comment_line_is_a_key_value_pair():
    """逐行判：注释随便写（可以在注释里解释这段历史），但**非注释行**必须是 KEY=VALUE。

    之前想用"全文不含三引号"粗暴一点，结果连注释里的说明都会被判违规 ——
    规则写得太宽会把好代码也拦下来，所以这里按行判。
    """
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert '"""' not in line, f"非注释行里出现了三引号：{line!r}"
        assert "=" in stripped, f"这一行既不是注释也不是 KEY=VALUE：{line!r}"


def test_the_important_keys_are_present():
    values = _parsed()
    for key in ("PET_ADDRESS", "DEEPSEEK_API_KEY", "GAODE_API_KEY", "HEFENG_API_KEY"):
        assert key in values, f"模板里缺少 {key}"
