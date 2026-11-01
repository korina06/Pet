"""上传图片识菜：按文件头判断是不是图、模型给的 JSON 要安全解析、失败分类的文案不能说错话。
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_photo import (  # noqa: E402
    MAX_IMAGE_BYTES,
    PhotoRecognizer,
    build_answer_prompt,
    decode_image,
    parse_name_reply,
    sniff_image,
)
from core.schemas import Message  # noqa: E402

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 40
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 40


def test_sniff_image_trusts_the_file_header_not_the_name():
    assert sniff_image(JPEG_BYTES) == "jpeg"
    assert sniff_image(PNG_BYTES) == "png"
    assert sniff_image(WEBP_BYTES) == "webp"
    assert sniff_image(b"GIF89a" + b"0" * 20) == "gif"
    # 一段 HTML 冒充 jpg
    assert sniff_image(b"<!DOCTYPE html>") is None
    assert sniff_image(b"") is None


def test_decode_image_accepts_a_data_url_prefix():
    """前端 FileReader 给的是 `data:image/png;base64,xxx`，要能剥掉前缀。"""
    raw = base64.b64encode(PNG_BYTES).decode()
    data, fmt = decode_image(f"data:image/png;base64,{raw}")
    assert data == PNG_BYTES and fmt == "png"


def test_decode_image_rejects_bad_input_with_a_readable_reason():
    for bad, hint in (
        ("", "没有收到图片"),
        ("这不是 base64 !!", "base64"),
        (base64.b64encode(b"<!DOCTYPE html>").decode(), "不是图片"),
    ):
        try:
            decode_image(bad)
        except ValueError as exc:
            assert hint in str(exc), f"{bad!r} 的提示不对：{exc}"
        else:
            raise AssertionError(f"{bad!r} 应该被拒绝")


def test_oversized_image_is_refused():
    huge = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1)
    try:
        decode_image(base64.b64encode(huge).decode())
    except ValueError as exc:
        assert "太大" in str(exc)
    else:
        raise AssertionError("超过上限的图应该被拒绝")


def test_parse_name_reply_handles_fences_and_junk():
    """模型偶尔包代码块或加解释——要能截出 JSON，但解析失败时绝不能瞎编。"""
    assert parse_name_reply(
        '```json\n{"name": "麻婆豆腐", "confidence": "high"}\n```'
    )["name"] == "麻婆豆腐"
    assert parse_name_reply("我觉得是红烧肉")["name"] is None
    assert parse_name_reply("")["name"] is None
    assert parse_name_reply('{"name": null}')["name"] is None
    # 太长的"名字"多半是一句话，不可信
    assert parse_name_reply('{"name": "这一看就是一道非常经典的家常菜"}' )["name"] is None
    # 候选取前三个
    parsed = parse_name_reply(
        '{"name": "红烧肉", "confidence": "low", '
        '"alternatives": ["东坡肉", "扣肉", "把子肉", "第四个别要"]}'
    )
    assert parsed["alternatives"] == ["东坡肉", "扣肉", "把子肉"]


def test_answer_prompt_carries_the_aversions_and_forbids_copying():
    messages = build_answer_prompt("红烧肉", [
        {"title": "红烧肉的做法", "link": "https://example.com/a", "snippet": "五花肉切块焯水"},
    ], ["香菜"])
    assert isinstance(messages[0], Message)
    system = messages[0].content
    assert "香菜" in system, "忌口要进提示词"
    assert "不要整段照抄" in system
    assert "不要硬编" in system
    assert "https://example.com/a" in messages[1].content


# ------------------------------------------------------- 流程：假客户端


class FakeSerp:
    def __init__(self, *, related=None, titles=None, results=None, fail=None):
        self.related = related if related is not None else ["Mapo tofu"]
        self.titles = titles if titles is not None else ["麻婆豆腐的做法", "🥰 #麻婆豆腐"]
        self.results = results if results is not None else [
            {"title": "麻婆豆腐的做法", "link": "https://example.com/1", "snippet": "豆腐切块"}
        ]
        self.fail = fail
        self.calls: list[str] = []

    async def upload_image(self, data, fmt):
        self.calls.append("upload")
        if self.fail == "upload":
            raise _SerpError("upstream")
        return "image-id"

    async def lens(self, image_id):
        self.calls.append("lens")
        if self.fail == "lens":
            raise _SerpError("timeout")
        return {"related": self.related, "titles": self.titles}

    async def text_search(self, query):
        self.calls.append("search")
        return self.results


class _SerpError(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.seen: list[list[Message]] = []

    async def complete(self, messages, tools=None):
        self.seen.append(messages)
        content = self._replies.pop(0) if self._replies else ""
        return Message(role="assistant", content=content)


def _run(recognizer, data=PNG_BYTES, fmt="png"):
    return asyncio.run(recognizer.run(data, fmt))


def test_happy_path_returns_dish_and_links():
    serp = FakeSerp()
    llm = FakeLLM([
        '{"name": "麻婆豆腐", "confidence": "high"}',
        "这是麻婆豆腐，做法：豆腐焯水，炒豆瓣酱，下豆腐烧入味。参考：https://example.com/1",
    ])
    outcome = _run(PhotoRecognizer(serp, llm, aversions_provider=lambda: ["香菜"]))

    assert outcome.ok, outcome.status
    assert outcome.dish == "麻婆豆腐"
    assert "麻婆豆腐" in outcome.reply
    assert outcome.sources == ["https://example.com/1"]
    assert serp.calls == ["upload", "lens", "search"], "三步都要走到"


def test_unknown_dish_is_reported_not_invented():
    """模型认不出时必须明说，而且**不再去搜做法**（省额度、也不给错答案）。"""
    serp = FakeSerp()
    llm = FakeLLM(['{"name": null}'])
    outcome = _run(PhotoRecognizer(serp, llm))

    assert outcome.status == "uncertain"
    assert "没认出" in outcome.reply
    assert "search" not in serp.calls, "认不出就不该再搜做法"


def test_timeout_maps_to_a_clear_message():
    serp = FakeSerp(fail="lens")
    outcome = _run(PhotoRecognizer(serp, FakeLLM([])))

    assert outcome.status == "timeout"
    assert "超时" in outcome.reply


def test_no_search_results_says_so_instead_of_faking_steps():
    serp = FakeSerp(results=[])
    llm = FakeLLM(['{"name": "红烧肉", "confidence": "high"}'])
    outcome = _run(PhotoRecognizer(serp, llm))

    assert outcome.status == "no_recipe"
    assert "红烧肉" in outcome.reply and "没找到" in outcome.reply


def test_every_failure_status_has_a_human_message():
    """每种失败都必须有话说——不能让前端显示空白气泡。"""
    from core.recipe_photo import FAILURE_COPY

    for kind in ("no_key", "timeout", "upstream", "quota", "empty"):
        assert FAILURE_COPY.get(kind), f"{kind} 没有文案"
