"""12306 车次查询，数据来自外部 MCP 服务器（`MCP_12306_URL` 留空就不注册）。

工具名与参数名钉死在远端真实 schema 上；返回结果压缩到几十行并写明「共几趟、只列几趟」。
"""

from __future__ import annotations

import json
import logging

from tools.mcp_remote import DEFAULT_TIMEOUT_SECONDS, McpProxyTool

logger = logging.getLogger(__name__)

#: 最多列几趟车
MAX_TRAINS = 15
#: 白天时段（含首尾）。一天 300 多趟车按发车时间排前 15 趟全是凌晨的 K/Z 车，
#: 对"要不要坐高铁去"这个问题毫无帮助，所以白天优先。
DAYTIME_START = "06:00"
DAYTIME_END = "22:00"

#: 第 220 步：`query-tickets` 回来的每个车次都带 `seats`（余票），这里**故意不转述**。
#: 用户明确说「余票不用查，先告诉车次信息就行」；另外从正确性看，余票是实时跳动的
#: 数字，转述一次就让模型有底气说「还有票」，而它其实只是半分钟前的快照。
#: 席别名映射表也一并删掉了 —— 留着不用，下一个人会以为这里还在报余票。


class TrainTicketTool(McpProxyTool):
    name = "search_trains"
    label = "火车票"
    description = (
        "查真实开行的火车车次与时刻（12306 数据），返回车次号、出发到达时间、历时。"
        "当需要回答「哪天有哪些车、几点发、坐多久」时用它；"
        "站名直接给中文（例如「南京」「苏州」），服务器会自己换算车站代码。"
        "查不到会明确说没查到——它不会编车次。"
    )
    #: 与远端 `query-tickets` 的 schema 完全一致，不做改名映射（少一层翻译就少一个 bug）
    parameters = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "出发站或城市，例如「南京」"},
            "to_station": {"type": "string", "description": "到达站或城市，例如「苏州」"},
            "train_date": {"type": "string", "description": "出发日期，格式 YYYY-MM-DD"},
        },
        "required": ["from_station", "to_station", "train_date"],
    }
    #: 远端服务器上这个工具叫什么
    remote_name = "query-tickets"
    #: 手动入口只在出行窗口里，不进桌宠轮盘
    show_in_menu = False
    intent_template = "帮我查{label}，从南京到苏州，下周六出发。"

    def __init__(
        self, *, url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, token: str = ""
    ) -> None:
        self.url = str(url or "").strip()
        self.timeout = timeout
        self.token = str(token or "").strip()
        self._destination = ""

    async def run(self, **kwargs):  # noqa: ANN003, ANN201
        """父类负责调用与截断；压缩在 `_shape` 里（先压后截，顺序不能反）。"""
        self._destination = str(kwargs.get("to_station") or "").strip()
        return await super().run(**kwargs)

    def _shape(self, text: str) -> str:
        """把 `query-tickets` 的 JSON 压成"共 N 趟 + 挑 15 趟"。

        宁可是"只有 15 趟"也不能是"4 万 token"：前者模型还能答，后者会挤掉整轮
        历史和工具结果。压缩是**信息损失**，所以正文里必须写清总数与筛选规则，
        让模型知道"这不是全部"，也让它能自己决定要不要换条件再查。
        """
        try:
            payload = json.loads(text)
        except ValueError:
            return text[: self.max_chars]

        if not isinstance(payload, dict) or not isinstance(payload.get("trains"), list):
            return text[: self.max_chars]

        trains = [item for item in payload["trains"] if isinstance(item, dict)]
        total = payload.get("count") or len(trains)
        chosen, deduped = self._pick(trains)

        header = (
            f"{payload.get('from_station', '?')} → {payload.get('to_station', '?')}"
            f" {payload.get('train_date', '?')}：共 {total} 趟（去重后 {deduped} 趟），"
            f"下面列 {len(chosen)} 趟（优先白天、按发车时间）。"
        )
        lines = [header]
        for item in chosen:
            lines.append(
                f"- {item.get('train_no', '?')} {item.get('from_station', '')}"
                f" {item.get('start_time', '?')}→{item.get('to_station', '')}"
                f" {item.get('arrive_time', '?')}（{item.get('duration', '?')}）"
            )
        if total and total > len(chosen):
            lines.append(
                f"（只列了 {len(chosen)} 趟。要看某个时段，带更具体的日期/出发时间再查。"
                "余票与票价不在这次结果里，要买票请去 12306 看。）"
            )
        else:
            lines.append("（余票与票价不在这次结果里，要买票请去 12306 看。）")
        return "\n".join(lines)

    def _pick(self, trains: list[dict]) -> tuple[list[dict], int]:
        """去重（同一车次会因途经站重复出现）→ 白天优先 → 取前 MAX_TRAINS 趟。"""
        destination = self._destination
        ordered = sorted(trains, key=lambda item: str(item.get("start_time") or "99:99"))

        unique: list[dict] = []
        seen: set[str] = set()
        for item in ordered:
            code = str(item.get("train_no") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            unique.append(item)

        if destination:
            matched = [
                item for item in unique if destination in str(item.get("to_station") or "")
            ]
            # 有精确到站的记录就用它们；否则回到全部（"苏州北"也算到苏州，模糊匹配已经覆盖）
            unique = matched or unique

        daytime = [
            item
            for item in unique
            if DAYTIME_START <= str(item.get("start_time") or "") <= DAYTIME_END
        ]
        # 白天不够就用夜车补齐，但白天的一定排在前面
        chosen = (daytime + [item for item in unique if item not in daytime])[:MAX_TRAINS]
        return chosen, len(unique)
