"""景点配图接口：查不到或失败一律回 `{"photo": null}`，署名信息必须随图返回，结果要缓存。
"""

from __future__ import annotations

import httpx
import pytest

from api.routes import clear_photo_cache, search_photo
from core.config import Settings


class _Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._payload


class _Client:
    """假的 httpx.AsyncClient：记下最后一次请求，返回预设结果。"""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


def make_request(client, key: str = "test-unsplash-key", monkeypatch=None):
    """造一个最小的 Request：只需要 app.state.http_client 与配置。"""
    settings = Settings(_env_file=None, unsplash_api_key=key)
    if monkeypatch is not None:
        monkeypatch.setattr("api.routes.get_settings", lambda: settings)

    class _App:
        class state:  # noqa: N801
            pass

    app = _App()
    app.state.http_client = client

    class _Request:
        def __init__(self, app):
            self.app = app

    return _Request(app)


def ok_payload() -> dict:
    return {
        "total": 937,
        "results": [
            {
                "id": "fp-atvi5WuE",
                "alt_description": "brown and green temple near lake",
                "urls": {
                    "regular": "https://images.unsplash.com/photo-1?w=1080",
                    "small": "https://images.unsplash.com/photo-1?w=400",
                },
                "user": {
                    "name": "zhang kaiyv",
                    "links": {"html": "https://unsplash.com/@zhangkaiyv"},
                },
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_photo_cache()
    yield
    clear_photo_cache()


async def test_returns_photo_with_credit(monkeypatch):
    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client, monkeypatch=monkeypatch)

    result = await search_photo("北海公园 北京", request)

    assert result.photo is not None
    photo = result.photo
    assert photo.url == "https://images.unsplash.com/photo-1?w=1080"
    assert photo.thumb.endswith("w=400")
    assert photo.author == "zhang kaiyv"
    assert photo.author_url == "https://unsplash.com/@zhangkaiyv"
    assert "unsplash.com" in photo.source_url, "必须带上 Unsplash 的链接（条款要求）"
    assert photo.alt, "alt 文本不能为空，否则读屏用户不知道图是什么"


async def test_uses_client_id_header(monkeypatch):
    """认证必须是 `Client-ID <access key>`：填成 Secret Key 会 401，写法也不能错。"""
    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client, key="my-access-key", monkeypatch=monkeypatch)

    await search_photo("故宫 北京", request)

    call = client.calls[0]
    assert call["headers"]["Authorization"] == "Client-ID my-access-key"
    assert "api.unsplash.com/search/photos" in call["url"]
    assert call["params"]["query"] == "故宫 北京"
    assert call["params"]["orientation"] == "landscape"


async def test_missing_key_returns_null_without_request(monkeypatch):
    """没配 key 时安静地返回 null，不发请求、不报错。

    必须显式把 settings 打成空 key：以前这里传的是 `monkeypatch=None`，
    于是 `get_settings()` 根本没被替身接住、读的是真实 .env，
    只要本机配了 UNSPLASH_API_KEY 就会真的发出请求、用例就红。
    （这个文件此前**没进 check_frontend 的清单**，所以一直没人发现。）
    """
    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client, key="", monkeypatch=monkeypatch)

    result = await search_photo("北海公园", request)

    assert result.photo is None
    assert client.calls == [], "没 key 就不该发请求"


async def test_empty_query_returns_null():
    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client)

    assert (await search_photo("   ", request)).photo is None
    assert client.calls == []


async def test_no_results_returns_null(monkeypatch):
    client = _Client(response=_Response(200, {"total": 0, "results": []}))
    request = make_request(client, monkeypatch=monkeypatch)

    assert (await search_photo("不存在的地方", request)).photo is None


async def test_http_error_is_swallowed(monkeypatch):
    """接口挂掉不能把整页带崩：返回 null 而不是抛异常。"""
    client = _Client(response=_Response(500, {}))
    request = make_request(client, monkeypatch=monkeypatch)

    assert (await search_photo("北海公园", request)).photo is None


async def test_network_error_is_swallowed(monkeypatch):
    client = _Client(error=httpx.ConnectError("boom"))
    request = make_request(client, monkeypatch=monkeypatch)

    assert (await search_photo("北海公园", request)).photo is None


async def test_result_is_cached(monkeypatch):
    """第二次同样的关键词不该再打接口（每小时只有 50 次搜索额度）。"""
    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client, monkeypatch=monkeypatch)

    first = await search_photo("北海公园 北京", request)
    second = await search_photo("北海公园 北京", request)

    assert first.photo is not None and second.photo is not None
    assert len(client.calls) == 1, f"应只请求一次，实际 {len(client.calls)} 次"


async def test_failure_is_cached_only_briefly(monkeypatch):
    """失败（含限流）只缓存很短时间。

    第 32 步的教训：失败也被缓存 24 小时的话，赶上 Unsplash 限流就会
    "一整天都再也试不出来"。所以失败用短 TTL（PHOTO_FAIL_TTL）。
    """
    from api import routes

    assert routes.PHOTO_FAIL_TTL <= 300, "失败缓存应远短于成功缓存"
    assert routes.PHOTO_FAIL_TTL < routes.PHOTO_CACHE_TTL

    client = _Client(error=httpx.ConnectError("boom"))
    request = make_request(client, monkeypatch=monkeypatch)

    first = await search_photo("北海公园", request)
    second = await search_photo("北海公园", request)

    assert first.photo is None and second.photo is None
    assert len(client.calls) == 1, "短时间内不该重复打接口"


async def test_photo_cache_is_allowed_to_cache_by_http_layer():
    """图片接口要允许缓存（main.py 的中间件给它开了 Cache-Control）。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "/api/photo" in source, "中间件应单独处理图片接口"
    assert "max-age=86400" in source, "图片接口应可被浏览器缓存一天"


# ------------------------------------------------- 多张候选与行程内去重（第 44 步）


def multi_payload() -> dict:
    """Unsplash 返回多张结果：后端要**全**给出来，前端才有得挑。"""
    results = []
    for index in (1, 2, 3):
        results.append(
            {
                "id": f"fp-{index}",
                "alt_description": f"photo {index}",
                "urls": {
                    "regular": f"https://images.unsplash.com/photo-{index}?w=1080",
                    "small": f"https://images.unsplash.com/photo-{index}?w=400",
                },
                "user": {
                    "name": f"author {index}",
                    "links": {"html": f"https://unsplash.com/@a{index}"},
                },
            }
        )
    return {"total": 937, "results": results}


async def test_returns_multiple_candidates_for_dedup(monkeypatch):
    """只给一张就没法在行程内去重，所以要多给几张，且每张都带署名。"""
    client = _Client(response=_Response(200, multi_payload()))
    result = await search_photo("公园 北京", make_request(client, monkeypatch=monkeypatch))

    assert result.photo is not None
    assert len(result.candidates) == 3, f"应返回 3 张候选，实际 {len(result.candidates)}"
    assert result.candidates[0].url == result.photo.url, "photo 永远是第一张候选"
    urls = [item.url for item in result.candidates]
    assert len(set(urls)) == 3, "候选之间不该重复"
    assert all(item.author and item.author_url for item in result.candidates), (
        "每张候选都要带署名，否则换一张就没法满足 Unsplash 条款"
    )


async def test_requests_more_than_one_result(monkeypatch):
    client = _Client(response=_Response(200, ok_payload()))
    await search_photo("故宫 北京", make_request(client, monkeypatch=monkeypatch))
    assert client.calls[0]["params"]["per_page"] > 1, (
        "要请求多张，否则永远只能拿到第一张（重复的根源）"
    )


async def test_candidates_skip_entries_without_a_usable_url(monkeypatch):
    """没有可用地址的条目要跳过，不能塞一个空 url 给前端。"""
    payload = multi_payload()
    payload["results"][0]["urls"] = {}
    payload["results"].append("垃圾数据")
    client = _Client(response=_Response(200, payload))
    result = await search_photo("北海公园", make_request(client, monkeypatch=monkeypatch))

    assert len(result.candidates) == 2
    assert all(item.url for item in result.candidates)


async def test_all_broken_entries_yield_null_not_an_empty_photo(monkeypatch):
    payload = {"total": 3, "results": [{"urls": {}}, {"urls": {}}]}
    client = _Client(response=_Response(200, payload))
    result = await search_photo("北海公园", make_request(client, monkeypatch=monkeypatch))

    assert result.photo is None and result.candidates == []


async def test_candidates_are_cached_together(monkeypatch):
    client = _Client(response=_Response(200, multi_payload()))
    request = make_request(client, monkeypatch=monkeypatch)
    first = await search_photo("公园 北京", request)
    second = await search_photo("公园 北京", request)

    assert len(first.candidates) == len(second.candidates) == 3
    assert len(client.calls) == 1, "候选列表也走同一个缓存，不该重复打接口"


async def test_cache_does_not_grow_without_bound(monkeypatch):
    """缓存条目有上限（第 179 步，评价13 N-2）。

    以前每个唯一关键词都会**永久**驻留一条（过期只会覆盖、从不淘汰）。
    单机自用增长慢，但"没有上限"本身是隐患：搜过的地名/景点会一直堆着。
    """
    import api.routes as routes

    client = _Client(response=_Response(200, ok_payload()))
    request = make_request(client, monkeypatch=monkeypatch)

    for index in range(routes.PHOTO_CACHE_MAX + 20):
        await routes.search_photo(f"关键词{index}", request)

    assert len(routes._PHOTO_CACHE) <= routes.PHOTO_CACHE_MAX, (
        f"缓存涨到 {len(routes._PHOTO_CACHE)} 条，超过上限 {routes.PHOTO_CACHE_MAX}"
    )
    # 最老的那批应当被淘汰，最近的还在
    assert f"关键词{routes.PHOTO_CACHE_MAX + 19}" in routes._PHOTO_CACHE
    assert "关键词0" not in routes._PHOTO_CACHE
