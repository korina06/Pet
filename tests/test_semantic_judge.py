"""语义裁决者：模型输出不合法或裁决失败，一律返回空 —— 未经裁决的向量结果不许放出去。
"""

from __future__ import annotations

import logging

from core.semantic_judge import (
    JUDGE_CANDIDATES,
    JUDGE_MAX_PICKS,
    LlmJudge,
    build_prompt,
    parse_picks,
)


def test_build_prompt_numbers_the_candidates_and_caps_them():
    candidates = [f"菜{index}" for index in range(20)]
    prompt = build_prompt("有什么下酒的", candidates)

    assert "有什么下酒的" in prompt
    assert "1. 菜0" in prompt and f"{JUDGE_CANDIDATES}. 菜{JUDGE_CANDIDATES - 1}" in prompt
    assert f"{JUDGE_CANDIDATES + 1}." not in prompt, "候选超过上限还在送"


def test_parse_picks_accepts_a_clean_payload():
    assert parse_picks('{"picks": [2, 1]}', total=3) == [2, 1]


def test_parse_picks_drops_out_of_range_and_duplicates():
    # 5 越界（只有 3 个候选）、2 重复、字符串 '1' 允许（模型常给字符串）
    assert parse_picks('{"picks": [5, 2, 2, "1"]}', total=3) == [2, 1]


def test_parse_picks_rejects_junk():
    for text in ("", "没有", "{picks: []}", '{"picks": "2"}', "[1,2]"):
        assert parse_picks(text, total=3) == [], f"这段不该解析出编号：{text!r}"


def test_parse_picks_caps_the_number_of_picks():
    picks = parse_picks('{"picks": [1,2,3,4,5,6,7]}', total=10)
    assert len(picks) <= JUDGE_MAX_PICKS


class _FakeLlm:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[list] = []

    async def complete(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error

        class _Reply:
            pass

        reply = _Reply()
        reply.content = self.content
        return reply


async def test_judge_returns_the_model_choice():
    judge = LlmJudge(_FakeLlm('{"picks": [2]}'))
    picks = await judge("有点腻", ["番茄炒蛋", "西湖醋鱼", "姜撞奶"])
    assert picks == [2]


async def test_judge_fails_closed_when_the_model_errors():
    judge = LlmJudge(_FakeLlm(error=RuntimeError("boom")))
    assert await judge("有点腻", ["番茄炒蛋"]) == []


async def test_judge_fails_closed_on_empty_candidates():
    judge = LlmJudge(_FakeLlm('{"picks": [1]}'))
    assert await judge("有点腻", []) == []


def test_judge_max_picks_matches_the_tool_limit():
    """裁决最多给 5 个，和工具的 MAX_RESULTS 对齐（不然多出来的会被截掉）。"""
    from tools.recipe_search_tool import MAX_RESULTS

    assert JUDGE_MAX_PICKS <= MAX_RESULTS


# ---------------------------------------------------------------- 裁决者怎么建


class _Provider:
    def __init__(self, label: str, available: bool) -> None:
        self.label = label
        self.available = available


class _FakeRouter:
    """只实现 `build_judge` 用到的两个东西：providers 和 complete。"""

    def __init__(self, providers, content: str = '{"picks": [1]}') -> None:
        self.providers = tuple(providers)
        self.content = content

    async def complete(self, messages):
        class _Reply:
            pass

        reply = _Reply()
        reply.content = self.content
        return reply


def _load_main():
    """按路径加载真 `main.py`（自研 runner 里 `from main import ...` 拿到的是桩）。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("pet_desktop_main_for_judge", root / "main.py")
    assert spec is not None and spec.loader is not None, "找不到 main.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_judge_is_none_without_an_embedder():
    """没有嵌入器 → 连候选都捞不到，裁决者不该建（评价15 T1-6②）。"""
    main = _load_main()
    assert main.build_judge(None, _FakeRouter([_Provider("DeepSeek", True)])) is None


def test_build_judge_is_none_when_no_provider_is_available():
    """一家都用不了 → 不建（否则每次 miss 白炸一次调用，还不如老实退化）。"""
    main = _load_main()
    router = _FakeRouter([_Provider("DeepSeek", False), _Provider("GLM（智谱）", False)])

    assert main.build_judge(object(), router) is None


async def test_build_judge_uses_the_router_not_a_hardcoded_provider():
    """**只有 GLM key 也得能裁** —— 这就是评价15 T1-6② 那个边角。

    第 188 步写死 `LlmJudge(DeepSeekLLM(...))`：只有 GLM key 的机器上嵌入器建得起来
    （它要 GLM key），裁决者却是一次注定失败的调用。现在裁决者就是当前的 ModelRouter，
    哪家配了 key 就用哪家。
    """
    main = _load_main()
    router = _FakeRouter(
        [_Provider("DeepSeek", False), _Provider("GLM（智谱）", True)],
        content='{"picks": [2]}',
    )

    judge = main.build_judge(object(), router)

    assert judge is not None, "有可用提供方就该建裁决者（不该只看 DeepSeek 的 key）"
    picks = await judge("补补身子", ["番茄炒蛋", "红枣乌鸡汤"])
    assert picks == [2], "裁决结果要真的能从路由那家回来"


# ------------------------------------------------------- 库里没向量时说一句话


class _FakeStorage:
    def __init__(self, count: int) -> None:
        self._count = count

    def execute(self, _sql: str):
        count = self._count

        class _Cursor:
            def fetchone(self):
                return (count,)

        return _Cursor()


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _warnings_from(call) -> list[str]:
    """跑一次 `call()`，把它打出的日志收集回来（不用 caplog：自研 runner 没有那个 fixture）。"""
    sink = _Collect()
    root = logging.getLogger()
    root.addHandler(sink)
    try:
        call()
    finally:
        root.removeHandler(sink)
    return sink.messages


def test_warns_when_the_vector_table_is_empty():
    """嵌入器与裁决者都在、但库里 0 条向量 → 启动时必须说清「语义召回是空转」。

    真实数据：本机 `data/pet.db` 的 `recipe_vectors` 一直是 **0 行**（建库脚本从没跑过），
    所以第 188 步的功能在用户面前一直不存在，日志里也看不出来。跑一次
    `scripts/build_recipe_vectors.py` 后同样的查询就能召回（96 条向量）。
    """
    main = _load_main()

    messages = _warnings_from(
        lambda: main._warn_if_no_vectors(_FakeStorage(0), object(), object())
    )

    assert any("build_recipe_vectors" in message for message in messages), messages


def test_no_warning_when_vectors_exist():
    main = _load_main()

    messages = _warnings_from(
        lambda: main._warn_if_no_vectors(_FakeStorage(96), object(), object())
    )

    assert not any("build_recipe_vectors" in message for message in messages), messages


def test_no_warning_when_the_vector_path_is_off_anyway():
    """没有嵌入器/裁决者时这条提示就是噪音 —— 那条路本来就没打算走。"""
    main = _load_main()

    messages = _warnings_from(
        lambda: main._warn_if_no_vectors(_FakeStorage(0), None, None)
    )

    assert not any("build_recipe_vectors" in message for message in messages), messages
