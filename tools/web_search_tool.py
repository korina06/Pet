"""联网搜索工具：把远端搜索的若干条结果压成几行可读条目（标题 / 摘要 / 链接）。

远端一次返回 10 条约 3192 字，比一轮回答的预算还大，所以条数与摘要长度都由这里定上限；
`uuid` 只用于后续抓正文，抓正文默认不开。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tools.mcp_remote import McpProxyTool, ToolResult, _cap, call_remote_tool

logger = logging.getLogger(__name__)

#: 一次最多回几条。5 条够模型判断"有没有答案"，再多只是重复。
MAX_RESULTS = 8
#: 摘要截断长度：实测摘要本就 60~80 字，超过 60 的部分多是套话
SNIPPET_CHARS = 60


def format_results(payload: Any, limit: int = MAX_RESULTS) -> str:
    """把远端的 JSON 压成 `序号 / 标题 / 摘要 / 链接`。

    真实返回（2026-10-29）：
    `{"query": "成都 11 月 旅游 攻略", "results": [{"uuid": "c5ac…", "title": "成都市_百度百科",
    "url": "https://baike.baidu.com/…", "snippet": "成都市（Chengdu City），简称…",
    "displayUrl": "https://baike.baidu.com"}, …], "totalResults": 100000}`
    """
    if not isinstance(payload, dict):
        text = str(payload or "").strip()
        return _cap(text, 2000, "搜索") if text else "（搜索没有返回内容）"

    items = payload.get("results")
    if not isinstance(items, list) or not items:
        return "没搜到结果。换个说法再搜一次，或直接问用户要更具体的地点。"

    lines: list[str] = []
    # 远端**不看我们传的 count**（实测传 5 仍回 8 条），所以上限必须在这边自己切
    for index, item in enumerate(items[: max(1, int(limit))], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip() or "（无标题）"
        snippet = " ".join(str(item.get("snippet") or "").split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS] + "…"
        url = str(item.get("url") or "").strip()
        lines.append(f"{index}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        if url:
            lines.append(f"   {url}")

    total = payload.get("totalResults")
    shown = sum(1 for line in lines if line[:1].isdigit() and "." in line[:4])
    head = f"搜到 {shown} 条"
    lines.insert(0, head + (f"（共约 {total} 条）" if isinstance(total, int) else ""))
    return "\n".join(lines)


class WebSearchTool(McpProxyTool):
    """远端 `bing_search` 的压缩版：只回标题 / 摘要 / 链接。"""

    name = "search_web"
    description = (
        "联网搜索（返回标题、摘要、链接）。用于查最新的旅游资讯、门票、开放时间这类"
        "模型自己不知道的信息。只要概览，不要试图抓取网页正文。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，例如「成都 11 月 旅游 注意事项」"},
            "count": {
                "type": "integer",
                "description": f"要几条，默认 5，最多 {MAX_RESULTS}",
                "minimum": 1,
                "maximum": MAX_RESULTS,
            },
        },
        "required": ["query"],
    }
    remote_name = "bing_search"
    #: 这台服务器的配置名（`MCP_SERVERS` 里的 `name`），装配时写入
    server: str = ""
    #: 只在出行窗口里，不进桌宠轮盘
    show_in_menu = False
    intent_template = "帮我搜一下 11 月去成都玩要注意什么。"

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

    async def run(  # type: ignore[override]
        self,
        query: str = "",
        count: int = 5,
        **_: Any,
    ) -> ToolResult:
        keyword = " ".join(str(query or "").split())
        if not keyword:
            return ToolResult(ok=False, error="要搜什么？query 不能是空的。")

        wanted = max(1, min(int(count or 5), MAX_RESULTS))
        try:
            text = await call_remote_tool(
                self.url,
                self.remote_name,
                {"query": keyword, "count": wanted},
                token=self.token,
                timeout=self.timeout,
            )
        except Exception as error:  # noqa: BLE001 —— 搜索挂掉不该炸掉整轮
            logger.warning("联网搜索失败：%s", error)
            return ToolResult(ok=False, error=f"搜索没成功：{error}")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ToolResult(ok=True, content=_cap(text, 2000, "搜索"))

        body = format_results(payload, limit=wanted)
        return ToolResult(ok=True, content=body)
