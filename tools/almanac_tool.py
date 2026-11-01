"""黄历工具：把远端"一次只回一天、字段一大堆"的接口包成按日期区间取。

只留宜、忌、农历、节日等与出门相关的信息，纳音、彭祖百忌、诸神方位一律丢掉。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from tools.mcp_remote import McpProxyTool, McpUnavailable, ToolResult, _cap, call_remote_tool

logger = logging.getLogger(__name__)

#: 一次最多查几天。两周足够覆盖绝大多数行程，再多也没人看；
#: 更重要的是它给"模型把 end_date 写错成一年后"兜了个底。
MAX_DAYS = 14
#: 区间写反、日期写成未来几百年时的上限（防止一次跑几百次远端调用）
MAX_SPAN_DAYS = 60


def _parse_day(text: str) -> date | None:
    """只认 `YYYY-MM-DD`。远端要的是 ISO 时间，日期由我们拼。"""
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def _weekday_cn(day: date) -> str:
    return "周" + "一二三四五六日"[day.weekday()]


def day_line(payload: dict[str, Any]) -> str:
    """把一天的真实 JSON 压成 3 行（第 4 行只在有冲煞/值宿时出现）。

    真实返回长这样（2026-10-30）：
    `{"公历":"2026年10月30日 星期五","农历":"农历丙午年九月廿一","干支":"丙午 戊戌 丁丑",
    "节气":"霜降","二十八宿":"娄金狗吉","冲煞":"冲羊(未)煞东","宜":"祭祀,平治道涂,馀事勿取",
    "忌":"嫁娶,开市", …}`
    """
    solar = str(payload.get("公历") or "").strip()
    lunar = str(payload.get("农历") or "").strip()
    solar_term = str(payload.get("节气") or "").strip()
    head = solar or "（日期未知）"
    if lunar:
        head = f"{head} {lunar}" if head else lunar
    if solar_term:
        head = f"{head} · {solar_term}"

    def join(raw: str) -> str:
        parts = [piece.strip() for piece in str(raw or "").split(",") if piece.strip()]
        return "、".join(parts) if parts else "（无）"

    lines = [
        head,
        f"  宜：{join(payload.get('宜', ''))}",
        f"  忌：{join(payload.get('忌', ''))}",
    ]
    tail = [
        str(payload.get("冲煞") or "").strip(),
        str(payload.get("二十八宿") or "").strip(),
    ]
    tail = [item for item in tail if item]
    if tail:
        lines.append("  " + " · ".join(tail))
    return "\n".join(lines)


class AlmanacTool(McpProxyTool):
    name = "search_almanac"
    label = "黄历"
    description = (
        "查指定日期区间的黄历：每天回宜、忌、冲煞、节气。"
        "规划行程时传行程第一天到最末一天（一次查完，别一天天问）；"
        "只问「今天/明天/某天」就只传 start_date，不传 end_date。"
        "它只说这天宜什么忌什么，不给吉凶评分。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "start_date": {
                "type": "string",
                "description": "公历起始日，格式 YYYY-MM-DD",
            },
            "end_date": {
                "type": "string",
                "description": "公历结束日，格式 YYYY-MM-DD；不填就只查 start_date 那一天",
            },
        },
        "required": ["start_date"],
    }
    #: 远端服务器上的工具名（Bazi Server 的 getChineseCalendar）
    remote_name = "getChineseCalendar"
    #: 这台服务器的配置名（`MCP_SERVERS` 里的 `name`），装配时写入
    server: str = ""
    #: 只在出行窗口里，不进桌宠轮盘
    show_in_menu = False
    intent_template = "帮我看看 11 月 5 号到 7 号的黄历，宜什么忌什么。"

    def __init__(
        self,
        *,
        url: str,
        timeout: float = 20.0,
        token: str = "",
        max_chars: int = 8000,
    ) -> None:
        self.url = str(url or "").strip()
        self.timeout = float(timeout)
        self.token = str(token or "").strip()
        self.max_chars = int(max_chars)

    async def run(self, **kwargs: Any) -> ToolResult:  # noqa: ANN003, ANN201
        if not str(self.url or "").strip():
            return ToolResult(
                ok=False,
                error=f"{self.display_label}还没配置服务器地址（见 .env 的说明）",
                tool_name=self.name,
                arguments=kwargs,
            )

        start_text = str(kwargs.get("start_date") or "").strip()
        end_text = str(kwargs.get("end_date") or "").strip() or start_text
        start = _parse_day(start_text)
        end = _parse_day(end_text)
        if start is None or end is None:
            return ToolResult(
                ok=False,
                error=(
                    f"{self.display_label}的日期要是 YYYY-MM-DD 格式"
                    f"（收到 start_date={start_text!r}、end_date={end_text!r}）"
                ),
                tool_name=self.name,
                arguments=kwargs,
            )
        if end < start:
            return ToolResult(
                ok=False,
                error=f"{self.display_label}的 end_date 比 start_date 还早，日期写反了",
                tool_name=self.name,
                arguments=kwargs,
            )
        span = (end - start).days + 1
        if span > MAX_SPAN_DAYS:
            return ToolResult(
                ok=False,
                error=(
                    f"{self.display_label}一次最多查 {MAX_SPAN_DAYS} 天"
                    f"（这次要 {span} 天），把区间缩小再查"
                ),
                tool_name=self.name,
                arguments=kwargs,
            )

        days = [start + timedelta(days=offset) for offset in range(span)]
        dropped = max(0, len(days) - MAX_DAYS)
        days = days[:MAX_DAYS]

        blocks: list[str] = []
        failures: list[str] = []
        for day in days:
            try:
                text = await call_remote_tool(
                    self.url,
                    self.remote_name,
                    # 用中午 12 点：远端按小时算干支，正午最不会跨日
                    {"solarDatetime": f"{day.isoformat()}T12:00:00+08:00"},
                    timeout=self.timeout,
                    token=self.token,
                )
            except McpUnavailable as error:
                failures.append(f"{day.isoformat()} {_weekday_cn(day)}：{error}")
                continue
            blocks.append(self._shape(text))

        if not blocks:
            return ToolResult(
                ok=False,
                error=(
                    f"{self.display_label}这几天都没查到："
                    + ("；".join(failures) if failures else "远端没返回内容")
                ),
                tool_name=self.name,
                arguments=kwargs,
            )

        body = "\n".join(blocks)
        notes: list[str] = []
        if failures:
            notes.append("没查到：" + "；".join(failures))
        if dropped:
            notes.append(f"只列了前 {MAX_DAYS} 天，其余 {dropped} 天没列")
        if notes:
            body = body + "\n（" + "；".join(notes) + "）"

        return ToolResult(
            ok=True,
            content=_cap(body, self.max_chars, self.display_label),
            tool_name=self.name,
            arguments=kwargs,
        )

    def _shape(self, text: str) -> str:
        """一天的 JSON → 最多 4 行。解析不了就退回原文（不静默丢数据）。"""
        try:
            payload = json.loads(text)
        except ValueError:
            return text.strip()
        if not isinstance(payload, dict):
            return text.strip()
        return day_line(payload)
