"""打印不该让整轮测试变红：`safe_print` 在严格 GBK 流上不抛，正常流行为与 print 一致。

顺带守住「别再叠 app.log handler、别再散着裸 print」这两条源码形状。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.console import safe_print  # noqa: E402

RUNNER = PROJECT_ROOT / "scripts" / "check_frontend.py"
MAIN = PROJECT_ROOT / "main.py"


def _gbk_stream():
    """一个**严格** GBK 流：编不进去就抛 UnicodeEncodeError（模拟真控制台）。"""
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="gbk", errors="strict", newline="")


def test_safe_print_survives_a_strict_gbk_stream():
    stream = _gbk_stream()
    safe_print("通过 3 条，失败 0 条", file=stream)          # GBK 能编码
    safe_print("⚠ 这一行有 GBK 编不进去的字符", file=stream)  # 关键：不许抛
    stream.flush()

    written = stream.buffer.getvalue().decode("gbk", "replace")
    assert "通过 3 条" in written, written
    assert written.count("\n") >= 2, "降级的那一行也要写出来（可能变问号）"


def test_safe_print_keeps_plain_text_and_newline():
    stream = io.StringIO()
    safe_print("普通一行", 42, sep="|", file=stream)
    assert stream.getvalue() == "普通一行|42\n"


def test_safe_print_accepts_a_stream_without_encoding():
    """`StringIO` / 自定义对象没有 `encoding`：别在降级分支里再崩一次。"""

    class _NoEncoding:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, text: str) -> None:
            self.chunks.append(text)

    sink = _NoEncoding()
    safe_print("没有 encoding 属性的流", file=sink)
    assert "".join(sink.chunks).startswith("没有 encoding 属性的流")


def test_runner_never_prints_directly():
    """runner 的所有输出都走 `_safe_print`，不再有裸 `print(`。"""
    text = RUNNER.read_text(encoding="utf-8")

    offenders = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if line.lstrip().startswith("print(")
    ]
    assert not offenders, f"runner 里还有裸 print：{offenders}"
    assert "from core.console import safe_print" in text, "helper 要从 core.console 导入"
    assert "_safe_print(" in text and "def _safe_print(" in text
    assert "通过 {passed} 条" in text, "汇总那一行还在"


def test_file_log_handler_is_attached_once():
    """`main.py` 里要有"同一文件只挂一次"的去重，且轮转失败不能抛。"""
    text = MAIN.read_text(encoding="utf-8")

    assert "class _TolerantRotatingFileHandler" in text
    assert "def doRollover(self) -> None:" in text
    assert "except OSError:" in text, "轮转被占用时只跳过，不许让 logging 打 traceback"
    assert "已经挂过同一个文件" in text, "重复 import 不能叠 handler"
    assert "baseFilename" in text
