"""评价12 那批前端修复的回归：全部是对 app.js 的定点源码断言（控制流/顺序问题）。
"""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _block_after(source: str, marker: str, open_char: str = "{") -> str:
    """从 `marker` 之后第一个 `open_char` 开始，返回**配对**的那一段（含括号）。"""
    start = source.index(marker)
    open_at = source.index(open_char, start)
    depth = 0
    for index in range(open_at, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_at:index + 1]
    raise AssertionError(f"括号不配对：{marker}")


def _function_body(source: str, name: str) -> str:
    return _block_after(source, f"function {name}(")


def test_photo_sending_is_reset_outside_the_controller_guard():
    """识图的收尾必须**无条件**复位（评价12 里我补的 P2）。

    以前 `state.recipeSending = false` 写在 `if (state.recipeAskController === controller) { … }`
    里面，而关窗走的 `abortRecipeAsk()` 会先把 controller 置空 ——
    于是这块永远不执行，`recipeSending` 卡在 true，食谱窗口从此发不出任何消息。
    """
    finally_block = _block_after(_function_body(_source(), "sendRecipePhoto"), "finally")

    # 复位必须直接出现在 finally 里（而不是嵌套在条件块内）
    assert "state.recipeSending = false;" in finally_block
    guarded = _block_after(
        finally_block, "if (state.recipeAskController === controller)", "{"
    )
    assert "state.recipeSending" not in guarded, (
        "复位又被写进 if 里了 —— 关窗时 controller 已被置空，这块永远不会执行"
    )


def test_photo_cache_only_remembers_real_answers():
    """配图缓存：失败不缓存、空结果 120 秒、有上限（评价12 P3-2 + P4-4）。"""
    body = _function_body(_source(), "loadPhotoCandidates")

    # 只有 `response.ok` 分支才允许置 cacheable = true
    ok_branch = body.index("if (response.ok)")
    catch_branch = body.index("} catch (error)")
    ok_section = body[ok_branch:catch_branch]
    assert "cacheable = true" in ok_section, "正常响应应当标记为可缓存"
    assert "cacheable = true" not in body[catch_branch:], "失败路径不许标记可缓存"

    # 空结果要有短 TTL，且必须与后端常量同口径
    assert "PHOTO_EMPTY_TTL_MS = 120 * 1000" in _source()
    assert "candidates.length ? 0 : Date.now() + PHOTO_EMPTY_TTL_MS" in body

    # 上限：超了要淘汰
    assert "PHOTO_CACHE_MAX = 200" in _source()
    assert "while (photoCache.size > PHOTO_CACHE_MAX)" in body


def test_restoring_recipe_history_waits_for_the_stream():
    """流在途时不重建历史（评价12 P3-1）。"""
    body = _function_body(_source(), "prepareRecipe")

    assert "state.recipeEpoch !== epoch || state.recipeSending" in body, (
        "守卫里缺了 recipeSending：切走再切回会把正在打字的气泡摘掉"
    )


def test_checkin_parses_json_safely():
    """签到先判状态再解析 JSON（评价12 P4-1）。"""
    source = _source()
    assert "async function readJsonSafely(response)" in source

    body = _function_body(source, "readJsonSafely")
    assert "catch (_error)" in body and "return {}" in body, "解析失败要返回空对象而不是抛"

    # 签到那处必须用它，而不是裸 response.json()
    submit = _block_after(source, "async function checkinDay(")
    assert "readJsonSafely(response)" in submit, "签到还在裸调 response.json()"


def test_trip_responses_are_guarded_by_an_epoch():
    """出行响应要按代次作废（评价12 P4-2）。"""
    source = _source()

    assert "tripEpoch: 0" in source, "state 里没有 tripEpoch"

    # 重开窗口时 +1
    open_block = _function_body(source, "openDialog")
    assert "state.tripEpoch += 1" in open_block, "重开窗口没有让在途响应作废"

    submit = _function_body(source, "onTravelSubmit")
    assert "const epoch = state.tripEpoch" in submit
    guards = submit.count("epoch !== state.tripEpoch")
    assert guards >= 3, f"代次守卫太少（{guards} 处）：HTTP 失败、成功、catch、finally 都要看"

    # 作废的那一代不许改按钮状态
    assert "if (epoch === state.tripEpoch) setTravelGenerating(false)" in submit


def test_the_dead_recipe_timeout_constant_is_gone():
    """死常量 RECIPE_TIMEOUT_MS 应当删掉（评价12 P4-3）。"""
    source = _source()
    assert "RECIPE_TIMEOUT_MS" not in source, "常量还在（全文件只有定义处引用它）"
    assert "不惊动签到" not in source, "那句过期的注释也应当一起删掉"
