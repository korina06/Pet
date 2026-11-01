"""语义召回之后的裁决者：把候选菜名交给模型，让它只按编号选，选不出就返回空。

解析失败、超时或报错一律当「没有符合的」，绝不放未经裁决的向量结果出去。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

#: 一次给模型看多少个候选。10 个足够覆盖语义相近的菜，又不会让它挑花眼。
JUDGE_CANDIDATES = 10

#: 最多接受几个选择（与工具的 MAX_RESULTS 一致，避免多给）。
JUDGE_MAX_PICKS = 5

_PROMPT = (
    "你在帮用户从本地菜谱库里挑菜。**只能从候选里选，不能编造**。\n"
    "如果候选里没有真正符合的，就返回空数组 —— 宁可说没有，也不要硬凑。\n"
    "候选后面括号里依次是：标签、主要食材、用时。\n"
    "只输出 JSON：{{\"picks\": [候选编号, ...]}}，按贴合程度从高到低。\n\n"
    "用户说：{query}\n\n候选：\n{candidates}"
)


def build_prompt(query: str, candidates: Sequence[str]) -> str:
    body = "\n".join(
        f"{index + 1}. {name}" for index, name in enumerate(candidates[:JUDGE_CANDIDATES])
    )
    return _PROMPT.format(query=query.strip(), candidates=body)


def parse_picks(text: str, *, total: int) -> list[int]:
    """从模型输出里取编号，过滤掉越界/重复/非整数。解析失败返回 []。"""
    if not text:
        return []

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []

    try:
        payload: Any = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []

    raw = payload.get("picks") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    picked: list[int] = []
    for item in raw:
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= total and index not in picked:
            picked.append(index)
        if len(picked) >= JUDGE_MAX_PICKS:
            break
    return picked


class LlmJudge:
    """用现成的对话模型做裁决。`llm` 只需有 `async complete(messages) -> reply`。"""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def __call__(self, query: str, candidates: Sequence[str]) -> list[int]:
        """返回**候选编号**（1 起）列表；空列表表示"都不合适"。"""
        if not candidates:
            return []

        try:
            from core.schemas import Message

            reply = await self._llm.complete(
                [Message(role="user", content=build_prompt(query, candidates))]
            )
        except Exception as error:  # noqa: BLE001 —— 裁决失败一律降级为"没有"
            logger.warning("语义裁决失败，按「没有符合的」处理：%s", error)
            return []

        content = getattr(reply, "content", "") or ""
        picks = parse_picks(content, total=min(len(candidates), JUDGE_CANDIDATES))
        logger.info("语义裁决：%s → 候选 %s", query[:24], picks)
        return picks
