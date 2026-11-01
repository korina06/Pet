"""评价11 那批修复的回归：每条都钉住「这个 bug 不再发生」，不是照新代码抄断言。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from agents.llm import LLMError
from core.checkin import CheckinArchive, NoMakeupCard
from core.itinerary import Meal, distribute_meals

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_requirements_txt_is_pure_ascii():
    """requirements.txt 必须纯 ASCII（评价11 P2-2）。

    pip 用**本地代码页**解码 requirements.txt。中文 Windows 默认 GBK，
    而 UTF-8 的中文注释按 GBK 解会出现无法映射的字节 —— 直接
    `UnicodeDecodeError`，"照 README 装"的第一步就挂。
    """
    data = (PROJECT_ROOT / "requirements.txt").read_bytes()

    assert all(byte < 128 for byte in data), (
        "requirements.txt 里有非 ASCII 字节，GBK 代码页下 pip 会报 UnicodeDecodeError"
    )
    # 顺带确认真的还能解出依赖（不是被清空了）
    text = data.decode("ascii")
    assert "fastapi==" in text and "pytest-asyncio==" in text


def test_trim_never_evicts_an_inflight_entry():
    """容量满时只能淘汰已完成的条目（评价11 P3-2）。

    以前 `_trim` 不区分状态、按时间淘汰最老的一条 —— 正在处理中的请求被逐出后，
    同 key 的第二个请求不再拿到 409，而是作为全新请求**真实重放**
    （烧一次 2–25 秒的出行流水线 + 外部配额）。
    """
    from api.idempotency import IdempotencyMiddleware

    middleware = IdempotencyMiddleware(app=None, paths={"/api/trip"}, max_entries=2)
    inflight = type("E", (), {})()
    inflight.state = "inflight"
    inflight.at = 1.0
    middleware._entries[("/api/trip", "inflight-1")] = inflight

    inflight2 = type("E", (), {})()
    inflight2.state = "inflight"
    inflight2.at = 2.0
    middleware._entries[("/api/trip", "inflight-2")] = inflight2

    done = type("E", (), {})()
    done.state = "done"
    done.at = 3.0
    middleware._entries[("/api/trip", "done-1")] = done

    middleware._trim()

    assert ("/api/trip", "inflight-1") in middleware._entries, "在途条目不许被淘汰"
    assert ("/api/trip", "inflight-2") in middleware._entries
    assert ("/api/trip", "done-1") not in middleware._entries, "该淘汰的是已完成的那条"


def test_sign_with_card_spends_at_most_one_card_per_day():
    """同一天补签两次只扣一张卡（评价11 P3-3）。

    以前"查重 → 扣卡 → 写签到"是三个独立步骤，两个并发请求都能通过查重，
    结果各扣一张卡、只有一次签到生效。
    """
    archive = CheckinArchive()
    archive.set_cards("s1", 2)

    fresh, cards = archive.sign_with_card("s1", date(2026, 3, 5))
    assert fresh is True
    assert cards == 1

    fresh_again, cards_again = archive.sign_with_card("s1", date(2026, 3, 5))
    assert fresh_again is False, "同一天第二次不算新签"
    assert cards_again == 1, "重复补签不该再扣卡"


def test_sign_with_card_refuses_when_out_of_cards():
    archive = CheckinArchive()
    archive.set_cards("s2", 0)

    try:
        archive.sign_with_card("s2", date(2026, 3, 5))
    except NoMakeupCard:
        pass
    else:
        raise AssertionError("没有补签卡时应当抛 NoMakeupCard")
    assert "2026-03-05" not in archive.checked_days("s2"), "没扣成卡就不该写进签到"


def test_llm_error_carries_a_machine_readable_retry_flag():
    """重试判定用异常标记，不用中文文案（评价11 P3-4）。"""
    plain = LLMError("上游业务错误：请求超时")     # 文案里"超时"，但不是网络问题
    network = LLMError("网络错误", retryable=True)

    assert plain.retryable is False, "文案里有「超时」不代表可以重试"
    assert network.retryable is True


def test_the_stream_retry_uses_the_flag_not_the_text():
    source = (PROJECT_ROOT / "agents" / "deepseek_llm.py").read_text(encoding="utf-8")

    assert "exc.retryable" in source, "流式重试应当看 exc.retryable"
    assert '"网络错误" in text or "超时" in text' not in source, "还在按中文文案判重试"


def test_env_file_is_resolved_to_an_absolute_path():
    """.env 用绝对路径（评价11 P4-3）。

    相对路径是按**进程 CWD** 解析的：从别的目录启动（任务计划、IDE 自定义 workdir）
    就读不到 .env，症状是"服务起得来但所有联网工具都报没配 key"。
    """
    from core.config import Settings

    env_file = Settings.model_config.get("env_file")
    assert env_file, "Settings 没配 env_file"
    assert Path(str(env_file)).is_absolute(), f"env_file 还是相对路径：{env_file}"


def test_a_restaurant_at_the_exact_centroid_ranks_first():
    """坐标与质心完全相同的餐厅要排**第一**（评价11 P4-4）。

    `straight_line_m(...) or 10 ** 9` 里 0 是 falsy —— 距离 0 反而被当成"最远"，
    恰好把唯一"最佳值"排到了最后。
    """
    spot = type("S", (), {"location": (30.0, 120.0)})()
    same = Meal(name="就在质心", location=(30.0, 120.0))
    far = Meal(name="远一点", location=(30.5, 120.5))
    buckets = distribute_meals([far, same], 1, per_day=3, day_spots=[[spot]])

    assert [meal.name for meal in buckets[0]][0] == "就在质心"


def test_chat_endpoints_have_a_server_side_deadline():
    """聊天端点要有服务端总预算（评价11 P3-1）。"""
    routes = (PROJECT_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    config = (PROJECT_ROOT / "core" / "config.py").read_text(encoding="utf-8")

    assert "chat_deadline_seconds" in config
    # 两个端点各一次：一次性调用 + 流式
    assert routes.count("asyncio.timeout(deadline)") >= 2, (
        "/chat 与 /chat/stream 都应当套服务端总预算"
    )
    assert "TimeoutError" in routes


def test_the_provider_name_comes_from_the_instance_not_a_literal():
    """错误文案不许写死 DeepSeek（评价11 P4-1）。"""
    source = (PROJECT_ROOT / "agents" / "deepseek_llm.py").read_text(encoding="utf-8")

    assert "DeepSeek 响应缺少可用的 choices" not in source
    assert "{self.label}响应缺少可用的 choices" in source


def _run(coro):
    return asyncio.run(coro)
