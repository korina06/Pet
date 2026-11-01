"""从 app.js 抽出 `clampToBounds` 纯函数，用 Node 真跑：拖动不许把窗口拖出屏幕。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import shutil

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PROJECT_ROOT / "static" / "app.js"
#: 临时脚本落在工作区内，避免系统临时目录的权限坑
SCRIPT_PATH = PROJECT_ROOT / ".clamp_check.js"

#: Node 可执行文件
# GitHub 的 runner 上没有本机那个 node 路径 —— **动态查找**（第 104 步）。
# 找不到时由 pytest.mark.skipif 跳过，绝不写死绝对路径。
NODE = shutil.which("node")


def _extract_function(name: str) -> str:
    """从 app.js 里抠出一个函数声明的源码（按大括号配平）。"""
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(rf"^[ \t]*function {re.escape(name)}\(", source, re.MULTILINE)
    assert match, f"app.js 里找不到函数 {name}"

    start = match.start()
    brace = source.index("{", match.end() - 1)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"函数 {name} 的大括号不配平")


def _extract_const(name: str) -> str:
    """抠出一个 `const NAME = 123;` 的声明，连同它一起执行。

    clampToBounds 依赖 PET_FALLBACK_W/H，只抽函数本身会 ReferenceError。
    这里把常量也提取出来，而不是为了测试去改函数签名——
    实现不该为测试让步。
    """
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(rf"^[ \t]*const {re.escape(name)} = .*?;$", source, re.MULTILINE)
    assert match, f"app.js 里找不到常量 {name}"
    return match.group(0).strip()


@pytest.mark.skipif(NODE is None, reason="需要 Node 才能跑纯函数验证")
def test_clamp_to_bounds_keeps_pet_inside_viewport():
    """clampToBounds 必须保证宠物整体落在视口内。

    这是"不超出屏幕"这条要求的行为验证：把提取出的函数放进 Node 执行，
    用大量越界坐标试探，断言结果的中心点始终让宠物完整可见。
    """
    func_src = _extract_function("clampToBounds")
    const_src = "\n".join(
        [_extract_const("PET_FALLBACK_W"), _extract_const("PET_FALLBACK_H")]
    )

    script = f"""
{const_src}
{func_src}

const W = 120, H = 80;
const VW = 1280, VH = 800;
const cases = [];
// 四角与四周大幅越界
for (const x of [-9999, -1, 0, VW, VW + 9999]) {{
  for (const y of [-9999, -1, 0, VH, VH + 9999]) {{
    cases.push([x, y]);
  }}
}}
// 中间的正常位置
cases.push([W / 2, H / 2], [VW - W / 2, VH - H / 2], [640, 400], [1, 1]);

const results = cases.map(([x, y]) => {{
  const r = clampToBounds(x, y, W, H, VW, VH);
  return {{
    input: [x, y],
    output: [r.x, r.y],
    left: r.x - W / 2,
    right: r.x + W / 2,
    top: r.y - H / 2,
    bottom: r.y + H / 2,
  }};
}});

// 故意传入非法尺寸，验证兜底逻辑（结果不能是 NaN）
const degenerate = [
  clampToBounds(500, 400, 0, 0, VW, VH),
  clampToBounds(500, 400, NaN, NaN, VW, VH),
  clampToBounds(500, 400, undefined, undefined, VW, VH),
];

console.log(JSON.stringify({{ vw: VW, vh: VH, w: W, h: H, results, degenerate }}));
"""

    SCRIPT_PATH.write_text(script, encoding="utf-8")
    try:
        completed = subprocess.run(
            [NODE, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    finally:
        SCRIPT_PATH.unlink(missing_ok=True)

    assert completed.returncode == 0, f"Node 执行失败：{completed.stderr[:500]}"
    data = json.loads(completed.stdout)

    vw, vh = data["vw"], data["vh"]
    w, h = data["w"], data["h"]

    # 1) 所有越界输入都被拉回视口内
    for row in data["results"]:
        assert row["left"] >= -0.001, f"左侧越界：{row}"
        assert row["top"] >= -0.001, f"上方越界：{row}"
        assert row["right"] <= vw + 0.001, f"右侧越界：{row}"
        assert row["bottom"] <= vh + 0.001, f"下方越界：{row}"

    # 2) 极端输入应贴到边界，而不是被拉到中间（否则"拖到角落"会失效）
    by_input = {tuple(r["input"]): r for r in data["results"]}
    assert by_input[(-9999, -9999)]["output"] == [w / 2, h / 2], "左上极端应贴左上"
    assert by_input[(vw + 9999, vh + 9999)]["output"] == [vw - w / 2, vh - h / 2], (
        "右下极端应贴右下"
    )

    # 3) 视口内的正常坐标不应被改动
    assert by_input[(640, 400)]["output"] == [640, 400], "视口内坐标不应被移动"

    # 4) 非法尺寸要走兜底，且结果仍是合法坐标
    for item in data["degenerate"]:
        for axis in ("x", "y"):
            value = item[axis]
            assert isinstance(value, (int, float)), f"兜底结果应是数字：{item}"
            assert value == value, f"兜底结果不能是 NaN：{item}"
