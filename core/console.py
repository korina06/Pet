"""把脚本的 stdout/stderr 强制成 UTF-8；有编不进去的字符就替换成 `?`，而不是让脚本崩。

没有 `reconfigure` 的对象（pytest 捕获、重定向）直接跳过。
"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8（拿不到就跳过，不抛异常）。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue


def safe_print(*args: object, sep: str = " ", end: str = "\n", file: object = None) -> None:
    """打印一行，**绝不因为控制台编码而崩**（第 214 步）。

    ## 为什么光有 `force_utf8()` 不够

    `force_utf8()` 只能改**手上这两个流对象**。而真实机器上流会被换掉，
    换掉的流不一定长得像 `TextIOWrapper`：

    - 某些终端/IDE 把 `sys.stdout` 换成自己的对象，`reconfigure` 根本不存在
      → `force_utf8()` 按设计静默跳过 → 打印仍走控制台的 GBK 严格编码；
    - 被重定向到文件、被 `contextlib.redirect_stdout` 换成 `StringIO` 时同理。

    于是最后一行（或者某条失败的断言消息里带个 `⚠`）就抛
    `UnicodeEncodeError: 'gbk' codec can't encode character ...`，
    而**测试其实全绿** —— 一个显示问题直接变成 exit 1。

    ## 做法

    先按原样打印；真撞上 `UnicodeEncodeError` 就按目标流的编码
    `encode(..., "replace")` 降级重打一遍（编不了的字变成 `?`）。
    第二步还失败就**放弃这一行**：少一行输出远好过整轮崩掉。
    """

    text = sep.join(str(arg) for arg in args)
    stream = file if file is not None else sys.stdout

    try:
        print(text, end=end, file=stream)  # type: ignore[arg-type]
        return
    except UnicodeEncodeError:
        pass

    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        fallback = text.encode(encoding, "replace").decode(encoding, "replace")
    except (LookupError, UnicodeError):
        fallback = text.encode("ascii", "replace").decode("ascii")

    try:
        print(fallback, end=end, file=stream)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 —— 连降级都打不出去，就安静丢掉这一行
        return
