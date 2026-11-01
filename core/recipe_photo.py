"""上传图片识菜：图片 → Google Lens → 中文菜名 → 文本搜做法 → 模型组织回答。

与主检索链路完全独立，坏了不影响菜谱功能。改之前注意：Lens 请求不能带 `country`，
上传字段名是 `image`。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.schemas import Message

logger = logging.getLogger(__name__)

#: 上传大小上限。真实的手机照片 2~6 MB，8 MB 足够；再大就是在浪费带宽和额度。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 只收这几种（SerpAPI / Google Lens 支持 JPEG/PNG/GIF/WebP）。
ALLOWED_FORMATS = {"jpeg": "image/jpeg", "png": "image/png",
                   "gif": "image/gif", "webp": "image/webp"}


def sniff_image(data: bytes) -> str | None:
    """按**文件头**判断真实格式。

    刻意不信扩展名和声明的 MIME：`foo.jpg` 里可能是一段 HTML。
    返回 `jpeg` / `png` / `gif` / `webp`，认不出返回 None。
    """
    if not data:
        return None
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def decode_image(image_base64: str) -> tuple[bytes, str]:
    """把前端传来的 base64 解成 `(字节, 格式)`；不合法就抛 `ValueError`。

    容忍带 `data:image/webp;base64,` 前缀的写法（前端 FileReader 默认给这个）。
    """
    text = (image_base64 or "").strip()
    if not text:
        raise ValueError("没有收到图片数据")
    if text.startswith("data:"):
        _, _, text = text.partition(",")

    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片数据不是合法的 base64") from exc

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片太大了（{len(data) / 1024 / 1024:.1f} MB），上限是 "
            f"{MAX_IMAGE_BYTES // 1024 // 1024} MB"
        )
    fmt = sniff_image(data)
    if fmt is None:
        raise ValueError("这看起来不是图片（JPEG / PNG / GIF / WebP）")
    return data, fmt


# --------------------------------------------------------------- 名称抽取


_NAME_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_name_reply(text: str) -> dict[str, Any]:
    """解析模型返回的"菜名 JSON"，**宽容但不脑补**。

    模型偶尔会包代码块或加一句解释，所以先截出第一个 JSON 对象；
    解析失败就当作"没认出来"，绝不拿半截文本当菜名。
    """
    raw = (text or "").strip()
    if not raw:
        return {"name": None, "confidence": "low", "alternatives": []}

    match = _NAME_JSON_RE.search(raw)
    candidate = match.group(0) if match else raw
    try:
        payload = json.loads(candidate)
    except ValueError:
        return {"name": None, "confidence": "low", "alternatives": []}
    if not isinstance(payload, dict):
        return {"name": None, "confidence": "low", "alternatives": []}

    name = payload.get("name")
    name = str(name).strip() if isinstance(name, str) else ""
    if not name or len(name) > 12:
        # 太长的"名字"多半是一句话（"这一看就是经典的…"），不可信。
        # 12 字够覆盖库里的长菜名（"北京烤鸭（家庭烤箱版）"是 11 字）。
        name = ""

    alternatives = [
        str(item).strip()
        for item in (payload.get("alternatives") or [])
        if isinstance(item, (str, int, float)) and str(item).strip()
    ][:3]

    confidence = str(payload.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "low"):
        confidence = "low"
    return {"name": name or None, "confidence": confidence, "alternatives": alternatives}


def build_name_prompt(related: list[str], titles: list[str]) -> list[Message]:
    """让模型从 Lens 的线索里抽**一个中文菜名**。

    只输出 JSON，不让它写解释——它的唯一职责是把噪声压成一个名字。
    """
    lines: list[str] = []
    if related:
        lines.append("规范名（可能是英文）：" + "、".join(related[:5]))
    if titles:
        lines.append("相似图标题（中英韩混排，噪声很多）：")
        lines.extend(f"  - {title[:60]}" for title in titles[:15])

    system = (
        "你是一个菜名抽取器。给你 Google Lens 的识别线索，你只回答这张图里"
        "**最主要的那道菜**叫什么（用中文常用名）。\n"
        "只输出 JSON，格式：{\"name\": \"麻婆豆腐\", \"confidence\": \"high\", "
        "\"alternatives\": [\"家常麻婆豆腐\"]}\n"
        "规则：\n"
        "1. 必须是**菜名**。不要回答「美食」「中餐」「家常菜」「餐厅」这类词——"
        "那就等于没认出来，此时 name 输出 null；\n"
        "2. 拿不准就把 confidence 设为 low，并在 alternatives 里给 2~3 个候选；\n"
        "3. 完全看不出是菜（比如拍的是人和风景）→ {\"name\": null}；\n"
        "4. 不要输出 JSON 以外的任何文字。"
    )
    user = "\n".join(lines) if lines else "（没有识别线索）"
    return [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]


def build_answer_prompt(
    name: str, results: list[dict[str, Any]], aversions: list[str]
) -> list[Message]:
    """把"菜名 + 搜索结果"组织成回答：名字、简要步骤、链接。"""
    lines: list[str] = []
    for index, row in enumerate(results[:5], start=1):
        title = str(row.get("title") or "").strip()
        link = str(row.get("link") or "").strip()
        snippet = str(row.get("snippet") or "").strip()[:200]
        lines.append(f"{index}. {title}\n   链接：{link}\n   摘要：{snippet}")

    aversions_text = "、".join(aversions) if aversions else "没有"
    system = (
        "你在桌宠应用的食谱窗口里。用户上传了一张菜的照片，识别结果已经确定，"
        "下面是网上搜到的资料。你要据此写一段**简短**的回答。\n"
        "要求：\n"
        "1. 第一句说这是什么菜（可以加一句特点）；\n"
        "2. 然后给 2~4 步**概括过的**做法（**不要整段照抄**网页原文，用自己的话压缩）；\n"
        "3. 最后列最多 2 个链接（原样给 URL）；\n"
        "4. 纯文本，不要用 markdown（不要 **、#、- 这些记号）；\n"
        "5. 不要写任何营养数值；\n"
        "6. 如果资料明显对不上（比如标题在讲别的菜）、或者摘要里没有可用的做法，"
        "就直说「没找到靠谱的做法」，**不要硬编步骤**；\n"
        f"7. 用户的长期忌口是：{aversions_text}。可以照实描述这道菜，"
        "但不要主动推荐含忌口的做法；如果这道菜本身含忌口食材，顺口提醒一句。"
    )
    user = (
        f"识别结果：{name}\n\n搜索到的资料：\n"
        + ("\n".join(lines) if lines else "（没有搜到结果）")
    )
    return [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]


# --------------------------------------------------------------- 流程编排


#: 失败分类。**每一种都要给用户明确说法**（用户明确要求：不能乱说）。
FAILURE_COPY = {
    "no_key": "还没配置识图服务，这个功能暂时用不了。",
    "timeout": "识图服务超时了，这张图没认出来。稍后再试一次吧。",
    "upstream": "识图服务暂时不可用，稍后再试一次吧。",
    "quota": "今天的识图次数用完了，明天再试吧。",
    "empty": "没认出这张图里是什么菜。换一张更清楚的、只拍一道菜的试试？",
    "no_recipe": "认出这是「{name}」，但网上没找到靠谱的做法。",
}


@dataclass
class PhotoOutcome:
    """一次识图的结果。`status` 决定前端怎么显示。"""

    status: str                     # ok / uncertain / empty / timeout / upstream / no_key
    reply: str = ""
    dish: str | None = None
    alternatives: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class PhotoFailure(Exception):
    """流程中的可预期失败（会被翻译成给用户看的话）。"""

    def __init__(self, kind: str, **kwargs: Any) -> None:
        self.kind = kind
        self.kwargs = kwargs
        super().__init__(kind)

    def message(self) -> str:
        template = FAILURE_COPY.get(self.kind, "识图暂时不可用。")
        try:
            return template.format(**self.kwargs)
        except (KeyError, IndexError):
            return template


class PhotoRecognizer:
    """图片 → 菜名 → 做法 → 回答。

    依赖全部注入，所以测试里可以塞假客户端，**不联网、不花额度**。
    """

    def __init__(
        self,
        serp: Any,
        llm: Any,
        *,
        aversions_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self._serp = serp
        self._llm = llm
        self._aversions = aversions_provider or (lambda: [])

    async def run(self, data: bytes, fmt: str) -> PhotoOutcome:
        try:
            return await self._run(data, fmt)
        except PhotoFailure as failure:
            logger.info("识图失败：%s", failure.kind)
            return PhotoOutcome(status=failure.kind, reply=failure.message())
        except Exception as exc:  # noqa: BLE001
            # SerpAPI 客户端抛的错带 `kind`（timeout/upstream/quota/empty）。
            # 这里用 duck typing 而不是 import tools：core 不该依赖 tools。
            kind = getattr(exc, "kind", None)
            mapped = {
                "no_key": "no_key",
                "timeout": "timeout",
                "quota": "quota",
                "empty": "empty",
            }.get(kind, "upstream")
            logger.warning("识图异常（%s）：%s", kind or type(exc).__name__, exc)
            failure = PhotoFailure(mapped)
            return PhotoOutcome(status=mapped, reply=failure.message())

    async def _run(self, data: bytes, fmt: str) -> PhotoOutcome:
        # 1) 上传拿 image_id（本地图不用公网可访问）
        image_id = await self._serp.upload_image(data, fmt)
        if not image_id:
            raise PhotoFailure("upstream")

        # 2) Lens 识图。**不带 country** —— 见模块开头的实测记录
        clues = await self._serp.lens(image_id)
        related = [str(item) for item in (clues.get("related") or []) if str(item).strip()]
        titles = [str(item) for item in (clues.get("titles") or []) if str(item).strip()]
        if not related and not titles:
            raise PhotoFailure("empty")

        # 3) 抽中文菜名（只让它输出一个名字）
        reply = await self._llm.complete(build_name_prompt(related, titles))
        picked = parse_name_reply(getattr(reply, "content", "") or "")
        name = picked["name"]
        if not name:
            return PhotoOutcome(
                status="uncertain" if related else "empty",
                reply=FAILURE_COPY["empty"],
                alternatives=picked["alternatives"],
            )

        # 4) 搜做法
        results = await self._serp.text_search(f"{name} 做法")
        if not results:
            raise PhotoFailure("no_recipe", name=name)

        # 5) 组织回答
        answer = await self._llm.complete(
            build_answer_prompt(name, results, self._aversions())
        )
        text = (getattr(answer, "content", "") or "").strip()
        if not text:
            raise PhotoFailure("no_recipe", name=name)

        return PhotoOutcome(
            status="ok",
            reply=text,
            dish=name,
            alternatives=picked["alternatives"],
            sources=[
                str(row.get("link")) for row in results[:3] if row.get("link")
            ],
        )
