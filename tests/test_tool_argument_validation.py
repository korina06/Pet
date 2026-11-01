"""工具参数校验：模型给来不合法的参数时系统怎么反应（适用于每个工具，所以单独一份）。

要守住的是「不许静默吞错」——不合法的条件要么被矫正，要么明确拒绝，不能当作没这个条件。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.recipe_library import create_recipe, ensure_schema  # noqa: E402
from core.storage import Storage  # noqa: E402
from tools.recipe_search_tool import RecipeSearchTool  # noqa: E402


class _Sink(logging.Handler):
    """自研 runner 没有 caplog，自己截一份日志。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _capture(action) -> list[str]:
    sink = _Sink()
    root = logging.getLogger()
    root.addHandler(sink)
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        action()
    finally:
        root.setLevel(previous)
        root.removeHandler(sink)
    return sink.messages


def _db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db, name="清炒时蔬", tags=["清淡"], ingredients=["青菜"],
        steps=["炒"], time_minutes=10,
    )
    create_recipe(
        db, name="花生炖猪蹄", tags=["炖菜"], ingredients=["猪蹄", "花生"],
        steps=["炖"], time_minutes=90,
    )
    return db


def _names(result) -> list[str]:
    return [item.get("name") for item in ((result.data or {}).get("items") or [])]


def _run(tool, **arguments):
    return asyncio.run(tool.safe_run(arguments))


# ------------------------------------------------- 猜不出来的值必须报错


def test_a_non_numeric_time_limit_is_rejected_with_a_readable_reason():
    db = _db()
    try:
        result = _run(RecipeSearchTool(storage=db), max_minutes="abc")

        assert result.ok is False, f"不该悄悄放过：{result.data!r}"
        assert "max_minutes" in (result.error or ""), result.error
        assert "整数" in (result.error or ""), result.error
    finally:
        db.close()


def test_the_error_is_a_readable_sentence_not_a_stack_trace():
    """错误要能被模型读懂并据以改参数，所以必须是人的话。"""
    db = _db()
    try:
        result = _run(RecipeSearchTool(storage=db), min_minutes=[1, 2])

        assert result.ok is False, result.error
        error = result.error or ""
        assert "min_minutes" in error and "整数" in error, error
        assert "Traceback" not in error
    finally:
        db.close()


# ------------------------------------------------- 无害的偏差必须矫正


def test_a_numeric_string_time_limit_is_coerced_and_still_filters():
    db = _db()
    try:
        result = _run(RecipeSearchTool(storage=db), max_minutes="15")

        assert result.ok is True, result.error
        names = _names(result)
        assert names, f"矫正后应当照常查得到：data={result.data!r}"
        assert "花生炖猪蹄" not in names, f"90 分钟的菜不该出现：{names}"
    finally:
        db.close()


def test_a_single_tag_string_is_widened_into_a_list():
    """`tags="辣"` 是模型常见的写法，矫正成 `["辣"]` 比拒绝更友好。"""
    db = _db()
    try:
        result = _run(RecipeSearchTool(storage=db), tags="清淡")

        assert result.ok is True, result.error
        assert _names(result), "矫正成单元素列表后应当照常命中"
    finally:
        db.close()


def test_an_unknown_argument_key_is_dropped_instead_of_crashing():
    """模型偶尔多塞一个 schema 里没有、`run()` 也不接受的键 —— 以前会 TypeError。

    注意规则是「接不了才丢」：`run()` 有 `**kwargs` 或同名形参时**原样透传**
    （`geocode_city` 的 schema 就只声明了一部分参数，丢掉会让调用静默变空）。
    """
    db = _db()
    try:
        result = _run(RecipeSearchTool(storage=db), keyword="", spiciness="很辣")

        assert result.ok is True, result.error
        assert result.error is None
    finally:
        db.close()


def test_a_key_the_tool_can_actually_take_is_passed_through_untouched():
    """`strict` 不在 schema 里，但 `run()` 有形参 —— 不能被当成"多余参数"丢掉。"""

    class _EchoTool(RecipeSearchTool):
        seen: dict = {}

        async def run(self, **kwargs):
            type(self).seen = dict(kwargs)
            return await super().run(**kwargs)

    db = _db()
    try:
        _run(_EchoTool(storage=db), keyword="", strict=True)
    finally:
        db.close()

    assert _EchoTool.seen.get("strict") is True, _EchoTool.seen



def test_unknown_keys_are_logged_so_they_do_not_vanish_silently():
    db = _db()
    try:
        messages = _capture(
            lambda: _run(RecipeSearchTool(storage=db), keyword="", spiciness="很辣")
        )
    finally:
        db.close()

    assert any("spiciness" in message for message in messages), messages


# ------------------------------------------------- 内部调用传错也要留痕


def test_the_internal_time_filter_no_longer_swallows_a_bad_value_silently():
    """`_within` 是内部函数：能走到它说明校验被绕过了，必须留下警告。

    这条盯的是「别再无声无息」：以前它 `return True` 就完了，
    连一行日志都没有，所以谁也没发现参数根本没生效。
    """

    class _Recipe:
        time_minutes = 90

    messages = _capture(lambda: RecipeSearchTool._within(_Recipe(), "abc"))

    assert any("时间上限" in message for message in messages), messages
