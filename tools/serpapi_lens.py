"""SerpAPI 客户端：传图、识图（Lens）与文本搜索。

两条硬约束：Lens 不能带 `country` 参数（只留 `hl=zh-cn`，否则连续 503）；上传字段名是
`image` 不是 `file`。文本搜索把 `organic_results` 裁成 title/link/snippet 三个字段。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.recipe_photo import ALLOWED_FORMATS

logger = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com"
#: 识图实测 12~15 秒，给足余量
LENS_TIMEOUT = 45.0
UPLOAD_TIMEOUT = 30.0
SEARCH_TIMEOUT = 25.0


class SerpApiError(Exception):
    """SerpAPI 调用失败。`kind` 是给上层做失败分类用的。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind          # timeout / upstream / quota
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


class SerpApiClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._key = (api_key or "").strip()
        self._client = client
        self._base = base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.configured:
            raise SerpApiError("no_key")
        try:
            response = await self._client.request(
                method, f"{self._base}{path}",
                params={"api_key": self._key, **kwargs.pop("params", {})},
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise SerpApiError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise SerpApiError("upstream", str(exc)) from exc

        if response.status_code == 401:
            raise SerpApiError("no_key", "API key 无效")
        if response.status_code in (402, 429):
            raise SerpApiError("quota", f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise SerpApiError("upstream", f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SerpApiError("upstream", "返回的不是 JSON") from exc
        if isinstance(payload, dict) and payload.get("error"):
            # 例如 Lens 对这张图给不出结果：当成"没认出来"而不是"服务坏了"
            raise SerpApiError("empty", str(payload["error"])[:200])
        return payload

    async def upload_image(self, data: bytes, fmt: str) -> str:
        """把本地图片传给 SerpAPI，换回 `image_id`（这样不用公网 URL）。"""
        mime = ALLOWED_FORMATS.get(fmt, "application/octet-stream")
        payload = await self._request(
            "POST",
            "/image",
            files={"image": (f"dish.{fmt}", data, mime)},
            timeout=UPLOAD_TIMEOUT,
        )
        image_id = payload.get("image_id")
        if not image_id:
            raise SerpApiError("upstream", "上传没返回 image_id")
        return str(image_id)

    async def lens(self, image_id: str) -> dict[str, list[str]]:
        """识图，返回**已经裁好的线索**：`related`（Google 的判断）和 `titles`。

        - `related_content[].query` 常常是英文规范名（`Mapo tofu`）；
        - `visual_matches[].title` 里才有中文菜名，但混着韩文/购物链接；
        两者一起交给模型抽中文名。
        """
        payload = await self._request(
            "GET",
            "/search",
            params={
                "engine": "google_lens",
                "image_id": image_id,
                "type": "visual_matches",
                "hl": "zh-cn",
                # 注意：**不要加 country**，加了必 503（见模块开头）
            },
            timeout=LENS_TIMEOUT,
        )
        related = [
            str(row.get("query")).strip()
            for row in (payload.get("related_content") or [])
            if isinstance(row, dict) and str(row.get("query") or "").strip()
        ]
        titles = [
            str(row.get("title")).strip()
            for row in (payload.get("visual_matches") or [])
            if isinstance(row, dict) and str(row.get("title") or "").strip()
        ]
        return {"related": related[:5], "titles": titles[:15]}

    async def text_search(self, query: str) -> list[dict[str, Any]]:
        """网页搜索，只保留写回答需要的三个字段。"""
        payload = await self._request(
            "GET",
            "/search",
            params={
                "engine": "google",
                "q": query,
                "hl": "zh-cn",
                "num": 8,
            },
            timeout=SEARCH_TIMEOUT,
        )
        results: list[dict[str, Any]] = []
        for row in (payload.get("organic_results") or [])[:6]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            link = str(row.get("link") or "").strip()
            if not title or not link:
                continue
            results.append({
                "title": title,
                "link": link,
                "snippet": str(row.get("snippet") or "").strip()[:200],
            })
        return results
