"""邮箱接口：授权码不出现在响应里、报错是可读中文、未登入时 401。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

import api.mail_routes as routes
from api.mail_routes import router
from core.mailbox import MailError, Provider, provider_by_key


class FakeMailClient:
    """接口层用到的全部方法，行为可预测。"""

    def __init__(self, address: str = "12345@qq.com") -> None:
        self.address = address
        self.provider = provider_by_key("qq")
        self.host = "imap.qq.com"
        self.message_calls: list[tuple] = []
        self.flagged_calls: list[tuple] = []
        self.check_calls: list[tuple] = []

    def list_folders(self) -> list[dict]:
        return [
            {"key": "inbox", "label": "收件箱", "mailbox": "INBOX",
             "unread": 12, "total": 109},
            {"key": "flagged", "label": "星标邮件", "mailbox": None,
             "unread": 0, "total": 0},
        ]

    def list_messages(self, folder: str, limit: int, offset: int = 0) -> dict:
        self.message_calls.append((folder, limit, offset))
        rows = [{
            "uid": "7",
            "mailbox": "INBOX",
            "folder": folder,
            "from_name": "张三",
            "from_address": "zhang@qq.com",
            "subject": "你好",
            "snippet": "正文预览",
            "received": "2025-10-01T20:49:00+08:00",
            # ts 是后端排序用的内部字段，响应模型里没有，**不该漏给前端**
            "ts": 1759322940.0,
            "unread": True,
            "flagged": False,
        }]
        return {"messages": rows, "total": 120, "offset": offset, "has_more": offset + 1 < 120}

    def set_flagged(self, mailbox: str, uid: str, flagged: bool) -> bool:
        self.flagged_calls.append((mailbox, uid, flagged))
        return flagged

    def check_new(self, folder: str, since_uid: int = 0) -> dict:
        self.check_calls.append((folder, since_uid))
        # 默认"没有新邮件"：这正是便宜路径该有的样子
        return {"messages": [], "total": 106, "unread": 3, "reset": False}

    def close(self) -> None:
        pass


class FakeStore:
    """替掉 MailSessionStore，避免真的去连 IMAP。"""

    def __init__(self, client: FakeMailClient | None = None, *, login_error=None):
        self.login_error = login_error
        self.sessions: dict[str, FakeMailClient] = {}
        if client is not None:
            self.sessions["s1"] = client
        self.logins: list[tuple] = []

    def get(self, session_id: str):
        return self.sessions.get(session_id)

    def login(self, session_id, address, secret, provider, **kwargs):
        self.logins.append((session_id, address, secret, provider.key))
        if self.login_error is not None:
            raise self.login_error
        fresh = FakeMailClient(address)
        self.sessions[session_id] = fresh
        return fresh

    def logout(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None


def build_app(*, client: FakeMailClient | None = None, login_error=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.mail_store = FakeStore(client, login_error=login_error)
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ------------------------------------------------------------------ 接线


def test_all_mail_routes_are_registered():
    paths = {route.path for route in router.routes}
    assert paths == {
        "/api/mail/providers",
        "/api/mail/login",
        "/api/mail/logout",
        "/api/mail/status",
        "/api/mail/folders",
        "/api/mail/messages",
        "/api/mail/check",
        "/api/mail/flag",
    }


async def test_providers_endpoint_lists_qq_only_and_no_secrets():
    async with make_client(build_app()) as client:
        response = await client.get("/api/mail/providers")

    assert response.status_code == 200
    body = response.json()
    assert [row["key"] for row in body["providers"]] == ["qq"]
    assert body["default"] == "qq"
    # 授权码怎么拿必须由后端告诉前端，前端不硬编码
    assert "授权码" in body["providers"][0]["help_text"]
    assert "secret" not in response.text


# ------------------------------------------------------------------ 登入


async def test_login_returns_account_without_the_secret():
    """最要紧的一条：授权码进了后端就不能再出来。"""
    secret = "super-secret-authorization-code"
    app = build_app()

    async with make_client(app) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1",
            "address": "12345@qq.com",
            "secret": secret,
            "provider": "qq",
        })

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"address", "provider", "provider_label", "host", "logged_in"}
    assert "secret" not in body
    assert secret not in response.text
    # 但确实原样传给了邮箱客户端（否则上面只是"什么都没做"）
    assert app.state.mail_store.logins == [("s1", "12345@qq.com", secret, "qq")]


async def test_login_strips_surrounding_spaces():
    """用户从网页复制授权码时经常带上空格。"""
    app = build_app()
    async with make_client(app) as client:
        await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "  12345@qq.com  ",
            "secret": "  abcdef  ", "provider": "qq",
        })
    assert app.state.mail_store.logins[0][1] == "12345@qq.com"
    assert app.state.mail_store.logins[0][2] == "abcdef"


async def test_login_rejects_incomplete_address():
    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "12345", "secret": "x",
        })
    assert response.status_code == 400
    assert "@" in response.json()["detail"]


async def test_login_rejects_blank_secret():
    """空白授权码要走"可读报错"，而不是 pydantic 的 422 机器话。"""
    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "12345@qq.com", "secret": "   ",
        })
    assert response.status_code == 400
    assert "授权码" in response.json()["detail"]


async def test_login_rejects_unsupported_mailbox_and_says_what_is_supported():
    """现在只支持 QQ。说清楚"支持什么"，而不是让用户对着"登入失败"猜。"""
    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "someone@gmail.com", "secret": "x",
        })
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "暂时还不支持" in detail
    assert "QQ 邮箱" in detail


async def test_login_rejects_provider_address_mismatch(monkeypatch):
    """UI 选的服务商和地址对不上时要早点说。

    否则会拿 QQ 的服务器去登别家邮箱，报回来的是"账号或授权码不正确"——
    用户按提示改了十遍授权码也没用。
    """
    other = Provider(key="other", label="别家邮箱", host="imap.example.com",
                     domains=("example.com",))
    monkeypatch.setattr(
        routes, "provider_by_key", lambda key: other if key == "other" else None
    )

    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "12345@qq.com",
            "secret": "x", "provider": "other",
        })

    assert response.status_code == 400
    assert "对不上" in response.json()["detail"]


async def test_login_surfaces_mail_error_as_readable_400():
    """IMAP 那层的 MailError 要变成 400 + 中文，而不是 500 + traceback。"""
    app = build_app(login_error=MailError("账号或授权码不正确。请确认填的是 16 位授权码。"))
    async with make_client(app) as client:
        response = await client.post("/api/mail/login", json={
            "session_id": "s1", "address": "12345@qq.com", "secret": "x",
        })
    assert response.status_code == 400
    assert "授权码" in response.json()["detail"]


async def test_login_requires_session_id():
    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/login", json={
            "address": "12345@qq.com", "secret": "x",
        })
    assert response.status_code == 422


# ------------------------------------------------------------------ 状态


async def test_status_reports_not_logged_in():
    async with make_client(build_app()) as client:
        response = await client.get("/api/mail/status", params={"session_id": "s1"})
    assert response.status_code == 200
    assert response.json() == {"logged_in": False, "account": None}


async def test_status_reports_the_account_after_login():
    client = FakeMailClient()
    app = build_app(client=client)
    async with make_client(app) as http:
        response = await http.get("/api/mail/status", params={"session_id": "s1"})

    body = response.json()
    assert body["logged_in"] is True
    assert body["account"]["address"] == "12345@qq.com"
    assert body["account"]["provider_label"] == "QQ 邮箱"


async def test_status_requires_session_id():
    async with make_client(build_app()) as client:
        response = await client.get("/api/mail/status")
    assert response.status_code == 422


async def test_logout_is_idempotent():
    """未登入时退出不算错：用户想回登入页，结果一样。"""
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        first = await client.post("/api/mail/logout", json={"session_id": "s1"})
        second = await client.post("/api/mail/logout", json={"session_id": "s1"})

    assert first.status_code == 200
    assert first.json()["status"] == "logged_out"
    assert second.status_code == 200


# ------------------------------------------------ 未登入必须是 401


@pytest.mark.parametrize("path", ["/api/mail/folders", "/api/mail/messages"])
async def test_read_endpoints_require_login(path):
    """401 而不是 404：语义是"没登入"，前端据此退回登入页。"""
    async with make_client(build_app()) as client:
        response = await client.get(path, params={"session_id": "nobody"})
    assert response.status_code == 401
    assert "尚未登入" in response.json()["detail"]


async def test_flag_requires_login():
    async with make_client(build_app()) as client:
        response = await client.post("/api/mail/flag", json={
            "session_id": "nobody", "mailbox": "INBOX", "uid": "1", "flagged": True,
        })
    assert response.status_code == 401


# ------------------------------------------------------------------ 文件夹


async def test_folders_returns_rows_with_counts():
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        response = await client.get("/api/mail/folders", params={"session_id": "s1"})

    assert response.status_code == 200
    folders = response.json()["folders"]
    assert [row["key"] for row in folders] == ["inbox", "flagged"]
    assert folders[0]["unread"] == 12
    assert folders[0]["mailbox"] == "INBOX"


async def test_folders_surfaces_mail_error_as_400():
    class Broken(FakeMailClient):
        def list_folders(self):
            raise MailError("邮箱拒绝了「列出文件夹」")

    app = build_app(client=Broken())
    async with make_client(app) as client:
        response = await client.get("/api/mail/folders", params={"session_id": "s1"})
    assert response.status_code == 400
    assert "列出文件夹" in response.json()["detail"]


# ------------------------------------------------------------------ 邮件


async def test_messages_forwards_folder_limit_and_offset():
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        response = await client.get("/api/mail/messages", params={
            "session_id": "s1", "folder": "flagged", "limit": 10, "offset": 20,
        })

    assert response.status_code == 200
    # offset 必须一路透到 core 层——没有它，历史邮件就永远取不到
    assert mail_client.message_calls == [("flagged", 10, 20)]
    body = response.json()
    assert body["folder"] == "flagged"
    assert body["messages"][0]["subject"] == "你好"
    # ts 是后端排序用的内部字段，不能漏给前端
    assert "ts" not in body["messages"][0]
    assert body["messages"][0]["unread"] is True


async def test_messages_returns_paging_metadata():
    """total / has_more 是前端"滚到底继续加载"的依据，不能少。"""
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        first = await client.get("/api/mail/messages", params={
            "session_id": "s1", "limit": 1, "offset": 0,
        })
        last = await client.get("/api/mail/messages", params={
            "session_id": "s1", "limit": 1, "offset": 119,
        })

    assert first.json()["total"] == 120
    assert first.json()["has_more"] is True
    assert last.json()["has_more"] is False


async def test_messages_defaults_to_inbox_page_zero_and_50():
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        await client.get("/api/mail/messages", params={"session_id": "s1"})
    assert mail_client.message_calls == [("inbox", 50, 0)]


@pytest.mark.parametrize("limit", [0, 201, 1000])
async def test_messages_rejects_out_of_range_limit(limit):
    """上限 200 是防呆：别让人一次把整个邮箱拖下来。"""
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        response = await client.get("/api/mail/messages", params={
            "session_id": "s1", "limit": limit,
        })
    assert response.status_code == 422


@pytest.mark.parametrize("offset", [-1, -100])
async def test_messages_rejects_negative_offset(offset):
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        response = await client.get("/api/mail/messages", params={
            "session_id": "s1", "offset": offset,
        })
    assert response.status_code == 422


# ------------------------------------------------------------------ 查新


async def test_check_forwards_folder_and_since_uid_and_reports_no_new_mail():
    """切回已缓存的文件夹时走这条：没有新邮件就不该带任何邮件回去。"""
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        response = await client.get("/api/mail/check", params={
            "session_id": "s1", "folder": "inbox", "since_uid": 1503,
        })

    assert response.status_code == 200
    assert mail_client.check_calls == [("inbox", 1503)]
    body = response.json()
    assert body["messages"] == []
    assert body["reset"] is False
    # 未读数要带回来，左侧数字才能自己更新
    assert body["unread"] == 3
    assert body["total"] == 106


async def test_check_returns_new_messages_to_prepend():
    class WithNew(FakeMailClient):
        def check_new(self, folder, since_uid=0):
            self.check_calls.append((folder, since_uid))
            return {
                "messages": [{
                    "uid": "1504", "mailbox": "INBOX", "folder": folder,
                    "from_name": "李四", "from_address": "li@qq.com",
                    "subject": "新邮件", "snippet": "刚到的",
                    "received": "2026-10-10T22:00:00+08:00",
                    "ts": 1.0, "unread": True, "flagged": False,
                }],
                "total": 107, "unread": 4, "reset": False,
            }

    app = build_app(client=WithNew())
    async with make_client(app) as client:
        response = await client.get("/api/mail/check", params={
            "session_id": "s1", "folder": "inbox", "since_uid": 1503,
        })

    body = response.json()
    assert [row["uid"] for row in body["messages"]] == ["1504"]
    assert body["unread"] == 4
    # ts 仍然不能漏给前端
    assert "ts" not in body["messages"][0]


async def test_check_can_ask_for_a_full_replace():
    """虚拟文件夹（星标邮件）没有 UIDNEXT，只能整份重算，靠 reset 告诉前端。"""

    class Virtual(FakeMailClient):
        def check_new(self, folder, since_uid=0):
            self.check_calls.append((folder, since_uid))
            return {"messages": [], "total": 2, "unread": 0, "reset": True}

    app = build_app(client=Virtual())
    async with make_client(app) as client:
        response = await client.get("/api/mail/check", params={
            "session_id": "s1", "folder": "flagged",
        })
    assert response.status_code == 200
    assert response.json()["reset"] is True


async def test_check_defaults_to_inbox_and_no_baseline():
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        await client.get("/api/mail/check", params={"session_id": "s1"})
    assert mail_client.check_calls == [("inbox", 0)]


async def test_check_requires_login():
    async with make_client(build_app()) as client:
        response = await client.get("/api/mail/check", params={"session_id": "nobody"})
    assert response.status_code == 401
    assert "尚未登入" in response.json()["detail"]


async def test_check_rejects_negative_since_uid():
    app = build_app(client=FakeMailClient())
    async with make_client(app) as client:
        response = await client.get("/api/mail/check", params={
            "session_id": "s1", "since_uid": -1,
        })
    assert response.status_code == 422


async def test_check_surfaces_mail_error_as_400():
    class Broken(FakeMailClient):
        def check_new(self, folder, since_uid=0):
            raise MailError("邮箱拒绝了「读取文件夹信息」")

    app = build_app(client=Broken())
    async with make_client(app) as client:
        response = await client.get("/api/mail/check", params={"session_id": "s1"})
    assert response.status_code == 400
    assert "读取文件夹信息" in response.json()["detail"]


# ------------------------------------------------------------------ 星标


async def test_flag_toggles_and_forwards_the_real_mailbox():
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        response = await client.post("/api/mail/flag", json={
            "session_id": "s1", "mailbox": "INBOX", "uid": "7", "flagged": True,
        })

    assert response.status_code == 200
    assert response.json() == {"uid": "7", "flagged": True}
    # 必须带真实邮箱名：删掉星标时要定位到那一个文件夹里的那一封
    assert mail_client.flagged_calls == [("INBOX", "7", True)]


async def test_flag_can_be_cleared():
    mail_client = FakeMailClient()
    app = build_app(client=mail_client)
    async with make_client(app) as client:
        await client.post("/api/mail/flag", json={
            "session_id": "s1", "mailbox": "INBOX", "uid": "7", "flagged": False,
        })
    assert mail_client.flagged_calls == [("INBOX", "7", False)]


@pytest.mark.parametrize("field,value", [
    ("mailbox", ""),
    ("uid", ""),
])
async def test_flag_rejects_empty_locator(field, value):
    app = build_app(client=FakeMailClient())
    payload = {"session_id": "s1", "mailbox": "INBOX", "uid": "7", "flagged": True}
    payload[field] = value
    async with make_client(app) as client:
        response = await client.post("/api/mail/flag", json=payload)
    assert response.status_code == 422


async def test_flag_surfaces_mail_error_as_400():
    class Broken(FakeMailClient):
        def set_flagged(self, mailbox, uid, flagged):
            raise MailError("邮箱拒绝了「修改星标」")

    app = build_app(client=Broken())
    async with make_client(app) as client:
        response = await client.post("/api/mail/flag", json={
            "session_id": "s1", "mailbox": "INBOX", "uid": "7", "flagged": True,
        })
    assert response.status_code == 400
    assert "修改星标" in response.json()["detail"]


# ------------------------------------------------ 没初始化也不能 500


async def test_missing_store_returns_503_not_crash():
    """main.py 的 lifespan 正常一定建好了 store；这里防的是"直接构造 app"的用法。"""
    app = FastAPI()
    app.include_router(router)
    async with make_client(app) as client:
        response = await client.get("/api/mail/status", params={"session_id": "s1"})
    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]
