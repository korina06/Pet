"""让真的 `imaplib` 连一个说真协议、且消息序号不等于 UID 的假服务器。

验的是"我真读懂协议了"，而不是拿自己的假设验证自己的假设。
"""

from __future__ import annotations

import imaplib
import re
import socket
import threading

from core.mailbox import (
    MailboxClient,
    parse_fetch_response,
    provider_by_key,
    uids_of,
)

HEADER = (
    b"From: =?UTF-8?B?5byg5LiJ?= <zhang@qq.com>\r\n"
    b"Subject: =?UTF-8?B?5L2g5aW9?=\r\n"
    b"Date: Fri, 10 Oct 2026 22:00:09 +0800\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
)
PART_MIME = (
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
)
PART_BODY = b"5L2g5aW977yM5LiW55WM"          # base64("你好，世界")
TEXT_BODY = b"--b\r\nContent-Type: text/plain\r\n\r\nhello"

MAILBOXES = (
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
    b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
    b'(\\HasNoChildren \\Sent) "/" "Sent Messages"',
)


class FakeImapServer:
    """脚本化的假 IMAP 服务器——**说真的协议**，不是假对象。

    刻意让行首的**序号**从 1 开始连续，而 **UID** 从 1501 开始，
    这样"把序号当 UID"的错误一定会露出来（第 35 步就是它）。
    """

    def __init__(
        self,
        *,
        uids=(1501, 1502, 1503),
        unread=2,
        flagged=(),
        include_uid_item=True,
    ):
        self.uids = list(uids)
        self.unread = unread
        self.flagged = set(flagged)
        self.include_uid_item = include_uid_item
        self.commands: list[str] = []
        #: 当前打开的文件夹（服务器视角），SEARCH 要看它
        self.selected: bytes | None = None
        self._stop = threading.Event()
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(4)
        self._socket.settimeout(0.2)
        self.host, self.port = self._socket.getsockname()
        self._threads: list[threading.Thread] = []

    # ---------------------------------------------------------- 生命周期

    def __enter__(self) -> "FakeImapServer":
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._accept_thread.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except (TimeoutError, socket.timeout, OSError):
                continue
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def factory(self):
        """给 MailboxClient 用的 imap_factory：连到本服务器（明文 IMAP4）。"""
        return lambda *args, **kwargs: imaplib.IMAP4(self.host, self.port, timeout=10)

    # ---------------------------------------------------------- 协议

    def _fetch_block(self, seq: int, uid: int) -> bytes:
        """一条多字面量的 FETCH 响应。

        真实服务器在 `UID FETCH` 的响应里会带上 `UID <n>` 数据项——
        这正是调用方对号入座用的键。
        """
        out = bytearray()
        out += b"* %d FETCH (" % seq
        if self.include_uid_item:
            out += b"UID %d " % uid
        out += b"FLAGS (\\Seen)"
        out += b' INTERNALDATE "10-Oct-2026 22:00:09 +0800"'
        sections = [(b"BODY[HEADER]", HEADER)]
        # 偶数 UID 当作非 multipart：没有第一段，只有 BODY[TEXT]
        if uid % 2:
            sections.append((b"BODY[1.MIME]", PART_MIME))
            sections.append((b"BODY[1]<0>", PART_BODY))
        sections.append((b"BODY[TEXT]<0>", TEXT_BODY))
        for name, payload in sections:
            out += b" " + name + b" {%d}\r\n" % len(payload) + payload
        out += b")\r\n"
        return bytes(out)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            conn.sendall(b"* OK [CAPABILITY IMAP4rev1 ID] fake server ready\r\n")
            stream = conn.makefile("rb")
            while not self._stop.is_set():
                raw = stream.readline()
                if not raw:
                    break
                line = raw.rstrip(b"\r\n")
                self.commands.append(line.decode("latin-1"))
                match = re.match(rb"(\S+) (\S+)(?: (.*))?$", line)
                if not match:
                    continue
                tag = match.group(1)
                cmd = match.group(2).upper()
                rest = match.group(3) or b""
                if not self._dispatch(conn, tag, cmd, rest, line):
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, conn, tag: bytes, cmd: bytes, rest: bytes, line: bytes) -> bool:
        upper = rest.upper()
        if cmd == b"CAPABILITY":
            conn.sendall(b"* CAPABILITY IMAP4rev1 ID\r\n" + tag + b" OK done\r\n")
        elif cmd == b"LOGIN":
            conn.sendall(tag + b" OK LOGIN completed\r\n")
        elif cmd == b"ID":
            conn.sendall(b'* ID ("name" "PetDesktop")\r\n' + tag + b" OK done\r\n")
        elif cmd == b"LIST":
            for box in MAILBOXES:
                conn.sendall(b"* LIST " + box + b"\r\n")
            conn.sendall(tag + b" OK LIST completed\r\n")
        elif cmd == b"STATUS":
            name = rest.split(b" ")[0].strip(b'"')
            conn.sendall(
                b'* STATUS "' + name + b'" (MESSAGES %d UNSEEN %d)\r\n'
                % (len(self.uids), self.unread)
                + tag + b" OK STATUS completed\r\n"
            )
        elif cmd in (b"SELECT", b"EXAMINE"):
            # 只读打开走的是 EXAMINE —— 探针第一次就栽在这里。
            # 而且 SELECT 必须回 READ-WRITE：回成 READ-ONLY 的话，
            # imaplib 会记住它是只读的，之后任何写命令都直接拒发
            # （"INBOX" is not writable），根本到不了网络。
            access = b"READ-ONLY" if cmd == b"EXAMINE" else b"READ-WRITE"
            self.selected = rest.split(b" ")[0].strip(b'"')
            conn.sendall(
                b"* %d EXISTS\r\n" % len(self.uids)
                + b"* 0 RECENT\r\n"
                + b"* OK [UIDVALIDITY 1] UIDs valid\r\n"
                + tag + b" OK [" + access + b"] " + cmd + b" completed\r\n"
            )
        elif cmd == b"UID":
            if upper.startswith(b"SEARCH"):
                mailbox = self.selected.upper() if self.selected else b"INBOX"
                if b"FLAGGED" in upper:
                    # 星标只在收件箱里：每个文件夹都返回同一批的话，
                    # 「星标邮件」会把同一封重复算好几遍
                    wanted = self.flagged if mailbox == b"INBOX" else set()
                else:
                    wanted = set(self.uids)
                found = [uid for uid in self.uids if uid in wanted]
                conn.sendall(
                    b"* SEARCH " + b" ".join(str(u).encode() for u in found) + b"\r\n"
                    + tag + b" OK UID SEARCH completed\r\n"
                )
            elif upper.startswith(b"FETCH"):
                for seq, uid in enumerate(self.uids, start=1):
                    conn.sendall(self._fetch_block(seq, uid))
                conn.sendall(tag + b" OK UID FETCH completed\r\n")
            elif upper.startswith(b"STORE"):
                conn.sendall(b"* 1 FETCH (FLAGS (\\Flagged))\r\n" + tag + b" OK done\r\n")
            else:
                conn.sendall(tag + b" BAD unknown UID command\r\n")
        elif cmd == b"LOGOUT":
            conn.sendall(b"* BYE bye\r\n" + tag + b" OK LOGOUT completed\r\n")
            return False
        else:
            conn.sendall(tag + b" BAD unknown command\r\n")
        return True


def make_client(server: FakeImapServer, address: str = "12345@qq.com") -> MailboxClient:
    return MailboxClient(
        address, "authorization-code", provider_by_key("qq"),
        imap_factory=server.factory(),
    )


# ------------------------------------------------- 第 35 步的回归防线


def test_uid_fetch_is_keyed_by_uid_not_by_sequence_number():
    """**这就是收件箱空着的那个 bug。**

    服务器返回的 UID 是 1501/1502/1503，行首序号是 1/2/3。
    把序号当 UID 用的话，`records.get("1501")` 永远拿不到东西，
    每一封都被跳过 → 接口返回空数组 → 界面显示"这个文件夹还没有邮件"。
    """
    with FakeImapServer(uids=[1501, 1502, 1503]) as server:
        client = make_client(server)
        client.login()
        page = client.list_messages("inbox", limit=10)

    assert page["total"] == 3
    assert [row["uid"] for row in page["messages"]] == ["1503", "1502", "1501"], (
        "行的 uid 必须是服务器给的真实 UID，而不是响应行首的消息序号"
    )
    # 顺带确认字段真的解出来了，而不是"有行但都是空的"
    assert page["messages"][0]["subject"] == "你好"
    assert page["messages"][0]["mailbox"] == "INBOX"
    assert page["messages"][0]["received"].startswith("2026-10-10")


def test_uids_far_from_sequence_numbers_still_line_up():
    """真账号上 UID 和序号差得很远，一个都不能错位。"""
    uids = [90001, 90002, 90003, 90004]
    with FakeImapServer(uids=uids) as server:
        client = make_client(server)
        client.login()
        page = client.list_messages("inbox", limit=10)

    assert [row["uid"] for row in page["messages"]] == [
        "90004", "90003", "90002", "90001"
    ]


def test_paging_by_uid_over_the_real_protocol():
    """翻页也走一遍真协议：offset 切片用的是 UID 列表，返回的也是 UID。"""
    with FakeImapServer(uids=[1501, 1502, 1503, 1504, 1505]) as server:
        client = make_client(server)
        client.login()
        first = client.list_messages("inbox", limit=2, offset=0)
        second = client.list_messages("inbox", limit=2, offset=2)

    assert [row["uid"] for row in first["messages"]] == ["1505", "1504"]
    assert first["has_more"] is True and first["total"] == 5
    assert [row["uid"] for row in second["messages"]] == ["1503", "1502"]


def test_folders_and_counts_over_the_real_protocol():
    with FakeImapServer(uids=[1501, 1502, 1503], unread=2) as server:
        client = make_client(server)
        client.login()
        folders = client.list_folders()

    assert [row["key"] for row in folders] == ["inbox", "flagged", "drafts", "trash"]
    assert folders[0]["mailbox"] == "INBOX"
    assert folders[0]["unread"] == 2
    assert folders[0]["total"] == 3

    # ID 命令发过了：QQ / 163 收不到它会直接拒登
    assert any(cmd.upper().endswith("ID (\"NAME\" \"PETDESKTOP\" \"VERSION\" \"0.1.0\" \"VENDOR\" \"PET_DESKTOP\")")
               or " ID (" in cmd.upper() for cmd in server.commands), server.commands


def test_readonly_open_uses_examine():
    """只读打开走 EXAMINE（不是 SELECT）——协议细节，别再踩第二次。"""
    with FakeImapServer(uids=[1501]) as server:
        client = make_client(server)
        client.login()
        client.list_messages("inbox")

    assert any(cmd.upper().endswith('EXAMINE "INBOX"') for cmd in server.commands), (
        server.commands
    )


def test_star_is_sent_as_uid_store():
    """星标改的是真实 UID，不是序号。"""
    with FakeImapServer(uids=[1501, 1502]) as server:
        client = make_client(server)
        client.login()
        client.set_flagged("INBOX", "1502", True)

    assert any(
        cmd.upper().endswith("UID STORE 1502 +FLAGS (\\FLAGGED)") for cmd in server.commands
    ), server.commands


def test_flagged_folder_over_the_real_protocol():
    with FakeImapServer(uids=[1501, 1502, 1503], flagged={1502}) as server:
        client = make_client(server)
        client.login()
        page = client.list_messages("flagged")

    assert [row["uid"] for row in page["messages"]] == ["1502"]


# ------------------------------------------------- 解析器本身的边界


def test_parse_prefers_the_uid_item_over_the_sequence_number():
    """单测层面把这条钉死：`* 7 FETCH (UID 1501 ...)` 的键必须是 1501。"""
    data = [(b"7 (UID 1501 FLAGS (\\Seen) BODY[HEADER] {5}", b"a: b\n")]
    records = parse_fetch_response(data)
    assert set(records) == {"1501"}, "行首的 7 是序号，不是 UID"
    assert records["1501"]["flags"] == ["\\Seen"]


def test_parse_falls_back_to_the_sequence_number_without_a_uid_item():
    """普通（非 UID）FETCH 的响应里没有 UID 项，这时序号是唯一能用的键。"""
    data = [(b"7 (FLAGS () BODY[HEADER] {5}", b"a: b\n")]
    records = parse_fetch_response(data)
    assert set(records) == {"7"}


def test_parse_does_not_confuse_uidvalidity_like_tokens():
    """别把别的字段误当成 UID 项。"""
    data = [(b"1 (UID 42 FLAGS () BODY[HEADER] {5}", b"a: b\n")]
    assert set(parse_fetch_response(data)) == {"42"}


def test_uid_item_inside_a_body_fragment_is_not_treated_as_a_new_message():
    """只有带行首序号的那一段才算新邮件，后续片段要归给当前这封。"""
    data = [
        (b"1 (UID 1501 BODY[HEADER] {5}", b"a: b\n"),
        (b" BODY[TEXT]<0> {20}", b"UID 9999 in the body"),
        b")",
    ]
    records = parse_fetch_response(data)
    assert set(records) == {"1501"}, "正文里的 UID 字样不能变成第二封邮件"
    assert records["1501"]["sections"]["TEXT"] == b"UID 9999 in the body"


def test_uids_of_still_joins_every_line():
    assert uids_of([b"1501 1502", b"1503"]) == [b"1501", b"1502", b"1503"]
