"""邮箱核心逻辑：UTF-7 邮箱名解码、多段 FETCH 解析、摘要解码、文件夹识别、排序、星标与凭据不外泄。
"""

from __future__ import annotations

import base64
import imaplib
import quopri

import pytest

from core.mailbox import (
    PROVIDERS,
    MailboxClient,
    MailError,
    MailSessionStore,
    clean_text,
    decode_body,
    decode_header_value,
    epoch_or_zero,
    flatten_list_data,
    folder_spec,
    imap_utf7_decode,
    login_error_text,
    network_error_text,
    parse_fetch_response,
    parse_headers,
    parse_imap_datetime,
    parse_list_data,
    pick_mailbox,
    provider_by_key,
    provider_for_email,
    providers_public,
    uids_of,
)

# --------------------------------------------------------------- 假 IMAP


class FakeIMAP:
    """够用的 imaplib 替身。

    返回值严格照抄 imaplib 的形状 `(typ, data)`，尤其是 FETCH：
    它把"序号 + 一堆段落"拍成一个**扁平列表**，每个字面量单独成 tuple。
    照抄这个形状，解析逻辑才是被真跑过的，而不是被绕过的。
    """

    def __init__(
        self,
        *,
        mailboxes=None,
        boxes=None,
        statuses=None,
        login_error=None,
        connect_errors=0,
    ):
        #: LIST 的原始响应行
        self.mailboxes = list(mailboxes or [])
        #: 邮箱名 -> {"uids": [...], "fetch": [...], "flagged": [...]}
        self.boxes = dict(boxes or {})
        self.statuses = dict(statuses or {})
        self.login_error = login_error
        #: 前 N 次 connect 之类抛 OSError（测重连）
        self.connect_errors = connect_errors
        self.capabilities = ("IMAP4REV1", "ID")
        self.calls: list[tuple] = []
        self.logged_out = False
        self.selected = None
        self.readonly = True

    # --- 认证 ---
    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error is not None:
            raise self.login_error
        return ("OK", [b"LOGIN completed"])

    def logout(self):
        self.calls.append(("logout",))
        self.logged_out = True
        return ("BYE", [b"bye"])

    def xatom(self, name, *args):
        self.calls.append(("xatom", name, args))
        return ("OK", [b"ID completed"])

    # --- 文件夹 ---
    def list(self):
        self.calls.append(("list",))
        return ("OK", list(self.mailboxes))

    def status(self, mailbox, what):
        self.calls.append(("status", mailbox))
        name = mailbox.strip('"')
        counts = self.statuses.get(name, {"MESSAGES": 0, "UNSEEN": 0})
        body = " ".join(f"{key} {value}" for key, value in counts.items())
        return ("OK", [f'"{name}" ({body})'.encode()])

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        self.calls.append(("select", name, readonly))
        if name not in self.boxes and name not in self.statuses:
            return ("NO", [b"Mailbox does not exist"])
        self.selected = name
        self.readonly = readonly
        return ("OK", [str(len(self.boxes.get(name, {}).get("uids", []))).encode()])

    # --- 邮件 ---
    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        box = self.boxes.get(self.selected, {})
        if command == "SEARCH":
            needle = args[-1]
            found = list(box.get("uids", []))
            if needle == "FLAGGED":
                flagged = {u.encode() if isinstance(u, str) else u
                           for u in box.get("flagged", [])}
                found = [u for u in found if u in flagged]
            elif isinstance(needle, str) and needle.endswith(":*"):
                # `UID SEARCH UID 1504:*` —— 只有比基准新的才算新邮件
                low = int(needle.split(":")[0])
                found = [u for u in found if int(u) >= low]
            return ("OK", [b" ".join(found)])
        if command == "FETCH":
            return ("OK", list(box.get("fetch", [])))
        if command == "STORE":
            return ("OK", [b"1 (FLAGS (\\Flagged))"])
        return ("BAD", [b"unknown command"])


def make_client(client: FakeIMAP, address: str = "12345@qq.com", secret: str = "code"):
    provider = provider_by_key("qq")
    return MailboxClient(
        address, secret, provider, imap_factory=lambda *a, **k: client
    )


#: QQ 邮箱常见的文件夹
QQ_MAILBOXES = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
    b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
    b'(\\HasNoChildren \\Sent) "/" "Sent Messages"',
    b'(\\Noselect \\HasChildren) "/" "&XfJT0ZAB-"',
]


def fetch_entry(uid: bytes, *, flags=b"", internaldate=b'01-Oct-2025 20:49:00 +0800',
                header=b"", part_mime=None, part_body=None, text_body=None):
    """照 imaplib 的形状拼一条多段 FETCH 响应。"""
    prefix = (
        uid + b" (FLAGS (" + flags + b") INTERNALDATE \""
        + internaldate + b"\" BODY[HEADER] {" + str(len(header)).encode() + b"}"
    )
    items: list = [(prefix, header)]
    if part_mime is not None:
        items.append((b" BODY[1.MIME] {%d}" % len(part_mime), part_mime))
    if part_body is not None:
        items.append((b" BODY[1]<0> {%d}" % len(part_body), part_body))
    if text_body is not None:
        items.append((b" BODY[TEXT]<0> {%d}" % len(text_body), text_body))
    items.append(b")")
    return items


def imap_utf7_encode_for_test(text: str) -> str:
    """**只给测试用的**编码器，用来造真实的测试数据。

    被测代码里刻意只有解码方向（内部保留服务器给的原始名字当 id），
    但测试数据不能靠手算 base64——那是在用手算结果验证手算结果。
    所以这里把编码方向补出来造数据，同时用下面那组**真实服务商的已知值**
    锚住正确性（`&XfJT0ZAB-` = 已发送 是 163/QQ 的实际写法）。
    """
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            raw = "".join(buffer).encode("utf-16-be")
            # modified UTF-7 按规范**不带 '=' 填充**，这里要跟着去掉；
            # 之前带上了，于是解码器把 "&V4NXPpCuTvY=-" 当非法数据原样吐回来
            # ——是测试数据造错了，不是解码器错了（这个 bug 就是被这条测试抓出来的）。
            encoded = base64.b64encode(raw).decode().replace("/", ",").rstrip("=")
            out.append("&" + encoded + "-")
            buffer.clear()

    for char in text:
        if char == "&":
            flush()
            out.append("&-")
        elif "\x20" <= char <= "\x7e":
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
    return "".join(out)


# --------------------------------------------------------------- 服务商


def test_only_qq_is_supported_for_now():
    """用户明确要求"先暂时只用 qq 邮箱登入"。

    这条同时是个闸门：以后想加服务商，必须是有意为之，
    而不是顺手把没验证过的一堆都摆上去。
    """
    assert [p.key for p in PROVIDERS] == ["qq"]


def test_provider_by_key_and_email():
    assert provider_by_key("qq").host == "imap.qq.com"
    assert provider_by_key("QQ").key == "qq"
    assert provider_by_key("gmail") is None

    assert provider_for_email("12345@qq.com").key == "qq"
    assert provider_for_email("someone@FoxMail.com").key == "qq"
    # 认不出来必须返回 None，不能瞎猜一家
    assert provider_for_email("someone@gmail.com") is None
    assert provider_for_email("没有at符号") is None


def test_providers_public_never_carries_secrets():
    row = providers_public()[0]
    assert set(row) == {"key", "label", "host", "port", "help_text", "domains"}
    assert "secret" not in row and "password" not in row
    assert row["help_text"], "必须告诉用户去哪拿授权码，否则登不上只会干瞪眼"


# --------------------------------------------------------- modified UTF-7


def test_imap_utf7_decode_plain_ascii():
    assert imap_utf7_decode("INBOX") == "INBOX"
    assert imap_utf7_decode("Deleted Messages") == "Deleted Messages"


def test_imap_utf7_decode_chinese():
    """163 / QQ 的文件夹名可能是中文，而 IMAP 规定要按 modified UTF-7 传。"""
    # 真实服务商的已知值，用来锚住正确性
    assert imap_utf7_decode("&XfJT0ZAB-") == "已发送"
    assert imap_utf7_encode_for_test("已发送") == "&XfJT0ZAB-"

    for name in ("草稿箱", "已删除", "垃圾邮件", "重要联系人"):
        assert imap_utf7_decode(imap_utf7_encode_for_test(name)) == name


def test_imap_utf7_decode_ampersand_and_bytes():
    assert imap_utf7_decode("A&-B") == "A&B"
    assert imap_utf7_decode(b"INBOX") == "INBOX"


def test_imap_utf7_decode_broken_input_keeps_everything():
    """格式坏了就原样显示——**绝不能吞字符**，否则用户会看到莫名其妙的空名。"""
    assert imap_utf7_decode("&XfJT0ZAB") == "&XfJT0ZAB"     # 少了收尾的 '-'
    assert imap_utf7_decode("&!!!-") == "&!!!-"             # 不是合法 base64


def test_imap_utf7_decode_tolerates_equals_padding():
    """规范里 modified UTF-7 不带 '=' 填充。

    真实服务商不该发出来，但既然收到了就当填充解掉——比原样吐一串
    "&V4NXPpCuTvY=-" 给用户看好得多。这条是写测试时真踩到的。
    """
    assert imap_utf7_decode("&V4NXPpCuTvY=-") == "垃圾邮件"


# --------------------------------------------------------------- 时间


def test_parse_imap_datetime_handles_space_padded_day():
    """IMAP 用 `_d`（空格补齐）而不是 `%d`：个位数日期是 " 1-Jan-2024"。"""
    assert parse_imap_datetime("01-Oct-2025 20:49:00 +0800").hour == 20
    assert parse_imap_datetime(" 1-Oct-2025 09:00:00 +0800").day == 1
    assert parse_imap_datetime("") is None
    assert parse_imap_datetime("不是日期") is None


def test_epoch_or_zero_survives_missing_date():
    assert epoch_or_zero(None) == 0.0
    assert epoch_or_zero(parse_imap_datetime("01-Oct-2025 20:49:00 +0800")) > 0


# ---------------------------------------------------------- 响应解析


def test_parse_fetch_response_multipart():
    """一次 FETCH 里夹着多个字面量，要按 `BODY[...]` 的段落名各归各位。"""
    header = b"From: =?UTF-8?B?5byg5LiJ?= <zhang@qq.com>\r\nSubject: hello\r\n\r\n"
    mime = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
    body = base64.b64encode("第一段正文".encode())
    wrapper = b"--b\r\nContent-Type: text/plain\r\n\r\n..."

    data = fetch_entry(
        b"7", flags=b"\\Seen", header=header,
        part_mime=mime, part_body=body, text_body=wrapper,
    )
    records = parse_fetch_response(data)

    assert set(records) == {"7"}
    record = records["7"]
    assert record["flags"] == ["\\Seen"]
    assert record["internaldate"] == "01-Oct-2025 20:49:00 +0800"
    assert record["sections"]["HEADER"] == header
    assert record["sections"]["1.MIME"] == mime
    assert record["sections"]["1"] == body
    assert record["sections"]["TEXT"] == wrapper


def test_parse_fetch_response_multiple_messages_and_nil_part():
    """非 multipart 的信没有第一段：`BODY[1.MIME]` / `BODY[1]` 整段缺席。"""
    first = fetch_entry(b"1", header=b"Subject: one\r\n\r\n", text_body=b"body one")
    second = fetch_entry(b"2", flags=b"\\Flagged", header=b"Subject: two\r\n\r\n")
    records = parse_fetch_response([*first, *second])

    assert set(records) == {"1", "2"}
    assert records["1"]["sections"].get("1") is None
    assert records["1"]["sections"]["TEXT"] == b"body one"
    assert records["2"]["flags"] == ["\\Flagged"]
    assert records["2"]["sections"]["HEADER"] == b"Subject: two\r\n\r\n"


def test_parse_fetch_response_tolerates_garbage():
    """解析不出来时返回空表，而不是抛异常——列表不能整页崩掉。"""
    assert parse_fetch_response([b")", b"* OK whatever", None]) == {}
    assert parse_fetch_response([]) == {}


def test_uids_of_reads_the_search_line():
    assert uids_of([b"1 2 3"]) == [b"1", b"2", b"3"]
    assert uids_of([b""]) == []
    assert uids_of([None]) == []


# ------------------------------------------------------------- 头部/正文


def test_parse_headers_decodes_chinese_and_rfc2047():
    raw = (
        b"From: =?UTF-8?B?5byg5LiJ?= <zhang@qq.com>\r\n"
        b"Subject: =?UTF-8?B?5L2g5aW9?=\r\n"
        b"Date: Wed, 01 Oct 2025 20:49:00 +0800\r\n\r\n"
    )
    parsed = parse_headers(raw)
    assert parsed["subject"] == "你好"
    assert parsed["name"] == "张三"
    assert parsed["address"] == "zhang@qq.com"
    assert parsed["date"] is not None


def test_parse_headers_survives_empty_input():
    parsed = parse_headers(b"")
    assert parsed["subject"] == ""
    assert parsed["address"] == ""
    assert parsed["date"] is None


def test_decode_header_value_keeps_plain_text():
    assert decode_header_value("普通主题") == "普通主题"
    assert decode_header_value("") == ""


def test_decode_body_plain_text():
    header = b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    assert decode_body(b"", "正文内容".encode(), header) == "正文内容"


def test_decode_body_base64_uses_part_mime_headers():
    """关键：CTE 在**那一段自己的** MIME 头里，不看它就会把 base64 原样显示出来。"""
    mime = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
    body = base64.b64encode("你好世界".encode())
    assert decode_body(mime, body, b"") == "你好世界"


def test_decode_body_quoted_printable():
    mime = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n"
    body = quopri.encodestring("中文 测试".encode("utf-8"))
    assert decode_body(mime, body, b"") == "中文 测试"


def test_decode_body_strips_html():
    header = b"Content-Type: text/html; charset=utf-8\r\n\r\n"
    html = "<p>你好 <b>世界</b></p><br><span>&amp; 更多</span>"
    assert decode_body(b"", html.encode(), header) == "你好 世界 & 更多"


def test_decode_body_truncated_base64_does_not_raise():
    """预览只取前 800 字节，base64 会在半个字符上被截断——不能因此抛异常。"""
    mime = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
    truncated = base64.b64encode("很长的一段中文内容".encode() * 20)[:37]
    assert isinstance(decode_body(mime, truncated, b""), str)


def test_clean_text_collapses_whitespace():
    assert clean_text("  a \n\n b\t c ") == "a b c"
    # 普通文本里的 "<" 不该被当成 HTML 标签剥掉内容
    assert "a < b" in clean_text("a < b and c > d")


# ------------------------------------------------------------- 文件夹


def test_parse_list_data_handles_quoted_and_literal_names():
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        # 名字是字面量时 imaplib 给的是 tuple
        (b'(\\HasNoChildren) "/" {12}', b"&XfJT0ZAB-"),
        b'(\\Noselect) "/" "NIL"',
    ]
    assert flatten_list_data(data)[1] == '(\\HasNoChildren) "/" &XfJT0ZAB-'

    entries = parse_list_data(data)
    assert entries[0] == (["\\HasNoChildren"], "INBOX")
    assert entries[1][1] == "&XfJT0ZAB-"
    assert "\\Noselect" in entries[2][0]


def test_pick_mailbox_prefers_special_use_flag():
    entries = parse_list_data(QQ_MAILBOXES) + [([], "草稿箱")]
    # 有 \Drafts 标记时用它，而不是按名字猜
    assert pick_mailbox(entries, folder_spec("drafts")) == "Drafts"


def test_pick_mailbox_falls_back_to_chinese_name():
    """服务商不给特殊用途标记时，只能认中文名。"""
    encoded = imap_utf7_encode_for_test("草稿箱").encode("ascii")
    entries = parse_list_data([
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "' + encoded + b'"',
    ])
    assert pick_mailbox(entries, folder_spec("drafts")) == encoded.decode("ascii")


def test_pick_mailbox_never_returns_a_noselect_folder():
    """\\Noselect 的容器（Gmail 的 [Gmail] 那种）不能拿来做邮件列表。"""
    entries = parse_list_data([b'(\\Noselect \\HasChildren) "/" "Deleted Messages"'])
    assert pick_mailbox(entries, folder_spec("trash")) is None


def test_pick_mailbox_inbox_is_case_insensitive():
    entries = parse_list_data([b'(\\HasNoChildren) "/" "inbox"'])
    assert pick_mailbox(entries, folder_spec("inbox")) == "inbox"


# ------------------------------------------------------------- 客户端


def test_client_logs_in_sends_id_and_lists_folders():
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 109, "UNSEEN": 12}, "Drafts": {"MESSAGES": 1, "UNSEEN": 0}},
    )
    client = make_client(fake)
    client.login()

    names = [call[0] for call in fake.calls]
    assert "login" in names
    # QQ / 163 必须发 ID 命令，否则会被拒登
    assert ("xatom", "ID", ('("name" "PetDesktop" "version" "0.1.0" "vendor" "pet_desktop")',)) in fake.calls

    folders = client.list_folders()
    keys = [row["key"] for row in folders]
    # 「重要联系人」「附件管理」IMAP 拿不到，本轮不做，所以不该出现
    assert keys == ["inbox", "flagged", "drafts", "trash"]
    inbox = folders[0]
    assert inbox["mailbox"] == "INBOX"
    assert inbox["unread"] == 12
    assert inbox["total"] == 109


def test_client_skips_folders_the_account_does_not_have():
    fake = FakeIMAP(
        mailboxes=[b'(\\HasNoChildren) "/" "INBOX"'],
        statuses={"INBOX": {"MESSAGES": 3, "UNSEEN": 0}},
    )
    client = make_client(fake)
    assert [row["key"] for row in client.list_folders()] == ["inbox", "flagged"]


def test_list_messages_returns_newest_first():
    header_a = b"From: a@qq.com\r\nSubject: A\r\n\r\n"
    header_b = b"From: b@qq.com\r\nSubject: B\r\n\r\n"
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 2, "UNSEEN": 1}},
        boxes={"INBOX": {
            "uids": [b"10", b"11"],          # 服务器给的是升序
            "fetch": [*fetch_entry(b"10", header=header_a, text_body=b"body a"),
                      *fetch_entry(b"11", header=header_b, text_body=b"body b")],
        }},
    )
    client = make_client(fake)
    page = client.list_messages("inbox")
    rows = page["messages"]

    assert [row["uid"] for row in rows] == ["11", "10"], "列表必须最新在前"
    assert rows[0]["subject"] == "B"
    assert rows[0]["mailbox"] == "INBOX"
    assert rows[0]["snippet"] == "body b"
    assert rows[0]["unread"] is True
    assert rows[1]["unread"] is True
    # 一共才 2 封，第一页就给完了
    assert page["total"] == 2
    assert page["has_more"] is False


def test_list_messages_marks_read_and_flagged():
    header = b"From: a@qq.com\r\nSubject: A\r\n\r\n"
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 1, "UNSEEN": 0}},
        boxes={"INBOX": {
            "uids": [b"5"],
            "fetch": fetch_entry(b"5", flags=b"\\Seen \\Flagged", header=header,
                                 text_body=b"hi"),
        }},
    )
    row = make_client(fake).list_messages("inbox")["messages"][0]
    assert row["unread"] is False
    assert row["flagged"] is True


def test_list_messages_prefers_part_one_over_the_multipart_wrapper():
    """multipart 时 BODY[TEXT] 是带 boundary 的壳，摘要必须用第一段。"""
    mime = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
    body = base64.b64encode("真正的正文".encode())
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 1, "UNSEEN": 0}},
        boxes={"INBOX": {
            "uids": [b"1"],
            "fetch": fetch_entry(b"1", header=b"Subject: s\r\n\r\n", part_mime=mime,
                                 part_body=body, text_body=b"--b\r\nContent-Type: text/plain"),
        }},
    )
    assert make_client(fake).list_messages("inbox")["messages"][0]["snippet"] == "真正的正文"


def test_list_messages_unknown_folder_is_rejected():
    client = make_client(FakeIMAP(mailboxes=QQ_MAILBOXES))
    with pytest.raises(MailError) as excinfo:
        client.list_messages("重要联系人")
    assert "不认识的文件夹" in excinfo.value.message


def test_list_messages_empty_folder_returns_empty_list():
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 0, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": [], "fetch": []}},
    )
    page = make_client(fake).list_messages("inbox")
    assert page["messages"] == []
    assert page["total"] == 0
    assert page["has_more"] is False


# ------------------------------------------------------------- 翻页


def _paging_client(count: int = 5, fetch: bool = True):
    """造一个有 `count` 封信的收件箱（uid = 1..count，升序）。"""
    uids = [str(i).encode() for i in range(1, count + 1)]
    entries: list = []
    if fetch:
        for uid in uids:
            entries.extend(fetch_entry(
                uid, header=b"Subject: s" + uid + b"\r\n\r\n", text_body=b"b" + uid))
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": count, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": uids, "fetch": entries}},
    )
    return make_client(fake)


def test_paging_walks_the_whole_mailbox_without_gaps_or_repeats():
    """这是用户报的"历史邮件获取不到"的核心断言。

    以前只有"取最新 N 封"，更早的**没有任何入口**。现在按 offset 往回翻，
    翻完必须刚好覆盖每一封：不重、不漏、顺序始终最新在前。
    """
    client = _paging_client(count=5)
    seen: list[str] = []
    has_more_flags: list[bool] = []
    offsets: list[int] = []

    offset = 0
    for _ in range(10):                       # 给足次数，靠 has_more 收口
        page = client.list_messages("inbox", limit=2, offset=offset)
        seen.extend(row["uid"] for row in page["messages"])
        has_more_flags.append(page["has_more"])
        offsets.append(page["offset"])
        if not page["has_more"]:
            break
        offset += len(page["messages"])

    assert seen == ["5", "4", "3", "2", "1"], f"翻页结果不对：{seen}"
    assert offsets == [0, 2, 4], f"offset 应当逐页递进：{offsets}"
    # 前两页还有更多，最后一页到头
    assert has_more_flags == [True, True, False]
    assert len(seen) == len(set(seen)), "不能重复给同一封"


def test_paging_reports_total_every_page():
    client = _paging_client(count=5)
    for offset in (0, 2, 4):
        page = client.list_messages("inbox", limit=2, offset=offset)
        assert page["total"] == 5, "total 应是文件夹里的邮件总数，而不是本页条数"


def test_paging_offset_past_the_end_is_an_empty_page_not_an_error():
    """翻过头返回空页而不是报错：前端据此停下自动加载。"""
    client = _paging_client(count=3)
    page = client.list_messages("inbox", limit=10, offset=99)
    assert page["messages"] == []
    assert page["total"] == 3
    assert page["has_more"] is False


def test_paging_a_page_bigger_than_the_mailbox():
    client = _paging_client(count=2)
    page = client.list_messages("inbox", limit=50, offset=0)
    assert [row["uid"] for row in page["messages"]] == ["2", "1"]
    assert page["has_more"] is False


def test_paging_never_returns_more_than_the_page_size():
    client = _paging_client(count=10)
    page = client.list_messages("inbox", limit=3, offset=0)
    assert len(page["messages"]) == 3
    assert page["has_more"] is True


def test_uids_of_joins_every_response_line():
    """SEARCH 的一长串 UID 可能被服务器拆成多条响应行。

    以前只读第一行，第一行之后的 UID 会被**静默丢掉**——表现成
    "邮箱里的历史邮件少了一截"，而且毫无报错。这条就是钉住那个 bug。
    """
    assert uids_of([b"1 2 3", b"4 5", b"6"]) == [b"1", b"2", b"3", b"4", b"5", b"6"]
    assert uids_of([b"1 2 3"]) == [b"1", b"2", b"3"]
    assert uids_of([]) == []
    assert uids_of([None, b"7"]) == [b"7"]


def test_split_search_response_is_not_truncated_end_to_end():
    """同样的坑走一遍真实调用路径：拆分响应时不能少邮件。"""

    class SplitSearch(FakeIMAP):
        def uid(self, command, *args):
            if command == "SEARCH":
                self.calls.append(("uid", command, args))
                # 服务器把 5 个 UID 拆成三行发回来
                return ("OK", [b"1 2", b"3", b"4 5"])
            return super().uid(command, *args)

    uids = [str(i).encode() for i in range(1, 6)]
    entries: list = []
    for uid in uids:
        entries.extend(fetch_entry(uid, header=b"Subject: s\r\n\r\n", text_body=b"b"))

    fake = SplitSearch(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 5, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": uids, "fetch": entries}},
    )
    page = make_client(fake).list_messages("inbox", limit=50)
    assert page["total"] == 5, "拆分响应不能把后面几行丢掉"
    assert [row["uid"] for row in page["messages"]] == ["5", "4", "3", "2", "1"]


# ------------------------------------------------------------- 后台查新


def _inbox_with(uids, *, uid_next, unread=0, fetch_all=True):
    """造一个收件箱，所有 uid 都有元数据可取。"""
    entries: list = []
    if fetch_all:
        for uid in uids:
            entries.extend(fetch_entry(
                uid, header=b"Subject: s" + uid + b"\r\n\r\n", text_body=b"b" + uid))
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": len(uids), "UNSEEN": unread,
                            "UIDNEXT": uid_next}},
        boxes={"INBOX": {"uids": list(uids), "fetch": entries}},
    )
    return make_client(fake), fake


def test_check_new_is_one_status_when_nothing_is_new():
    """**这条是"默默检测有没有新邮件"的核心。**

    切回一个已缓存的文件夹时不该把邮件重下一遍。UIDNEXT 告诉我们下一封新邮件
    会拿到的 UID；它没超过"我手上最大 UID + 1"就说明没有新邮件——
    此时只发一次 STATUS，**不 EXAMINE、不 SEARCH、不 FETCH**。
    """
    client, fake = _inbox_with([b"1501", b"1502", b"1503"],
                              uid_next=1504, unread=1)
    result = client.check_new("inbox", since_uid=1503)

    assert result["messages"] == []
    assert result["reset"] is False
    assert result["total"] == 3
    assert result["unread"] == 1

    kinds = [call[0] for call in fake.calls]
    assert "select" not in kinds, f"没有新邮件时不该打开文件夹：{fake.calls}"
    assert "uid" not in kinds, f"没有新邮件时不该 SEARCH/FETCH：{fake.calls}"
    assert kinds.count("status") == 1, f"应当只问一次 STATUS：{fake.calls}"


def test_check_new_fetches_only_the_new_messages():
    """有新邮件时只取新的那几封，已经加载过的旧邮件一封都不重新下。"""
    client, fake = _inbox_with([b"1501", b"1502", b"1503", b"1504"],
                              uid_next=1505, unread=2)
    result = client.check_new("inbox", since_uid=1503)

    assert [row["uid"] for row in result["messages"]] == ["1504"], (
        "只该拿到比 since_uid 新的那一封（所有 uid 都有可取的元数据，"
        "所以多出来的一定是搜索条件没生效）"
    )
    assert result["reset"] is False
    assert result["total"] == 4
    assert result["unread"] == 2

    # 搜索用的是 UID 区间，而不是把整个文件夹再拉一遍
    search = [call for call in fake.calls if call[0] == "uid" and call[1] == "SEARCH"]
    assert search and search[-1][2][-1] == "1504:*", fake.calls


def test_check_new_without_uidnext_falls_back_to_searching():
    """服务器不给 UIDNEXT 时没法便宜判断，只能真去搜一次——但不能崩。"""
    entries: list = []
    for uid in (b"1501", b"1502", b"1503", b"1504"):
        entries.extend(fetch_entry(uid, header=b"Subject: s\r\n\r\n", text_body=b"b"))
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 4, "UNSEEN": 2}},      # 没有 UIDNEXT
        boxes={"INBOX": {"uids": [b"1501", b"1502", b"1503", b"1504"], "fetch": entries}},
    )
    result = make_client(fake).check_new("inbox", since_uid=1503)
    assert [row["uid"] for row in result["messages"]] == ["1504"]


def test_check_new_without_a_baseline_returns_the_first_page():
    """前端手上还没有基准（since_uid=0）时给第一页，并让前端整份替换。"""
    client, _fake = _inbox_with([b"1501", b"1502", b"1503"], uid_next=1504)
    result = client.check_new("inbox", since_uid=0)

    assert result["reset"] is True
    assert [row["uid"] for row in result["messages"]] == ["1503", "1502", "1501"]


def test_check_new_on_virtual_folder_asks_for_a_full_replace():
    """「星标邮件」没有 UIDNEXT 可用，只能整份重算并让前端替换。"""
    entries: list = []
    for uid in (b"1501", b"1502"):
        entries.extend(fetch_entry(uid, flags=b"\\Flagged",
                                   header=b"Subject: s\r\n\r\n", text_body=b"b"))
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 2, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": [b"1501", b"1502"], "fetch": entries,
                         "flagged": [b"1501", b"1502"]}},
    )
    result = make_client(fake).check_new("flagged", since_uid=99)

    assert result["reset"] is True
    assert [row["uid"] for row in result["messages"]] == ["1502", "1501"]


def test_check_new_rejects_unknown_folder():
    client = make_client(FakeIMAP(mailboxes=QQ_MAILBOXES))
    with pytest.raises(MailError):
        client.check_new("重要联系人")


def test_flagged_folder_searches_across_mailboxes_and_sorts():
    """「星标邮件」是虚拟文件夹：跨文件夹搜 FLAGGED 再按时间倒序合并。"""
    older = fetch_entry(b"1", flags=b"\\Flagged",
                        internaldate=b'01-Oct-2025 08:00:00 +0800',
                        header=b"Subject: old\r\n\r\n", text_body=b"old")
    newer = fetch_entry(b"2", flags=b"\\Flagged",
                        internaldate=b'02-Oct-2025 08:00:00 +0800',
                        header=b"Subject: new\r\n\r\n", text_body=b"new")
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 1, "UNSEEN": 0},
                  "Drafts": {"MESSAGES": 1, "UNSEEN": 0}},
        boxes={
            "INBOX": {"uids": [b"1"], "fetch": older, "flagged": [b"1"]},
            "Drafts": {"uids": [b"2"], "fetch": newer, "flagged": [b"2"]},
        },
    )
    page = make_client(fake).list_messages("flagged")
    rows = page["messages"]
    assert [row["subject"] for row in rows] == ["new", "old"]
    # 每一行都要带真实邮箱名——星标要靠它定位
    assert {row["mailbox"] for row in rows} == {"INBOX", "Drafts"}
    assert page["total"] == 2
    assert page["has_more"] is False


def test_flagged_folder_skips_unreadable_mailbox():
    """某个文件夹读不了，不该让整个「星标邮件」打不开。"""

    class Grumpy(FakeIMAP):
        def uid(self, command, *args):
            if self.selected == "Deleted Messages":
                return ("NO", [b"permission denied"])
            return super().uid(command, *args)

    fake = Grumpy(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 1, "UNSEEN": 0},
                  "Deleted Messages": {"MESSAGES": 1, "UNSEEN": 0}},
        boxes={
            "INBOX": {"uids": [b"1"],
                      "fetch": fetch_entry(b"1", flags=b"\\Flagged",
                                           header=b"Subject: ok\r\n\r\n",
                                           text_body=b"ok"),
                      "flagged": [b"1"]},
            "Deleted Messages": {"uids": [b"9"], "fetch": [], "flagged": [b"9"]},
        },
    )
    page = make_client(fake).list_messages("flagged")
    assert [row["subject"] for row in page["messages"]] == ["ok"]


def test_flagged_folder_pages_too():
    """「星标邮件」也要能翻页，否则星标一多，早的同样看不到。"""
    entries: list = []
    flagged: list = []
    for i in range(1, 6):
        uid = str(i).encode()
        entries.extend(fetch_entry(
            uid, flags=b"\\Flagged",
            internaldate=("0%d-Oct-2025 08:00:00 +0800" % i).encode(),
            header=b"Subject: s" + uid + b"\r\n\r\n", text_body=b"b"))
        flagged.append(uid)

    fake = FakeIMAP(
        mailboxes=[b'(\\HasNoChildren) "/" "INBOX"'],
        statuses={"INBOX": {"MESSAGES": 5, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": [str(i).encode() for i in range(1, 6)],
                         "fetch": entries, "flagged": flagged}},
    )
    client = make_client(fake)

    first = client.list_messages("flagged", limit=2, offset=0)
    assert [row["uid"] for row in first["messages"]] == ["5", "4"]
    assert first["total"] == 5
    assert first["has_more"] is True

    last = client.list_messages("flagged", limit=2, offset=4)
    assert [row["uid"] for row in last["messages"]] == ["1"]
    assert last["has_more"] is False


def test_set_flagged_opens_readwrite_and_stores_flag():
    fake = FakeIMAP(
        mailboxes=QQ_MAILBOXES,
        statuses={"INBOX": {"MESSAGES": 1, "UNSEEN": 0}},
        boxes={"INBOX": {"uids": [b"3"], "fetch": []}},
    )
    client = make_client(fake)
    assert client.set_flagged("INBOX", "3", True) is True

    # 改标记必须用可写方式打开，readonly=True 会被服务器拒绝
    assert ("select", "INBOX", False) in fake.calls
    assert ("uid", "STORE", ("3", "+FLAGS", "(\\Flagged)")) in fake.calls

    client.set_flagged("INBOX", "3", False)
    assert ("uid", "STORE", ("3", "-FLAGS", "(\\Flagged)")) in fake.calls


def test_set_flagged_rejects_bad_input():
    client = make_client(FakeIMAP())
    with pytest.raises(MailError):
        client.set_flagged("", "1", True)
    with pytest.raises(MailError):
        client.set_flagged("INBOX", "1 OR 2", True)


def test_repr_never_leaks_the_secret():
    """授权码等同密码：它绝不能出现在 repr / 日志里。"""
    client = MailboxClient("12345@qq.com", "super-secret-code", provider_by_key("qq"))
    assert "super-secret-code" not in repr(client)
    assert "12345@qq.com" in repr(client)


# ------------------------------------------------------------- 错误文案


def test_login_error_text_is_actionable():
    """IMAP 的英文报错要翻成"我该去做什么"，而不是原样丢给用户。"""
    code = login_error_text(imaplib.IMAP4.error("Login fail. Please enter your authorization code"))
    assert "授权码" in code

    unsafe = login_error_text(imaplib.IMAP4.error("Unsafe Login. Please contact kefu@188.com"))
    assert "Unsafe Login" in unsafe

    generic = login_error_text(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"))
    assert "不正确" in generic


def test_network_error_text_mentions_the_host():
    assert "imap.qq.com" in network_error_text(TimeoutError("timed out"), "imap.qq.com")
    assert "imap.qq.com" in network_error_text(OSError("boom"), "imap.qq.com")


def test_login_failure_raises_readable_mail_error():
    fake = FakeIMAP(login_error=imaplib.IMAP4.error("Login fail. Please enter your authorization code"))
    with pytest.raises(MailError) as excinfo:
        make_client(fake).login()
    assert "授权码" in excinfo.value.message
    # 认证失败要顺手把连接关掉，别把半开的连接留着
    assert fake.logged_out is True


# ------------------------------------------------------------- 会话存储


def make_store_client(secret: str = "code") -> MailboxClient:
    fake = FakeIMAP(mailboxes=QQ_MAILBOXES,
                    statuses={"INBOX": {"MESSAGES": 0, "UNSEEN": 0}})
    return make_client(fake, secret=secret)


def test_session_store_login_status_logout():
    store = MailSessionStore(factory=lambda *a, **k: make_store_client())
    store.login("s1", "12345@qq.com", "code", provider_by_key("qq"))

    assert store.get("s1") is not None
    assert store.logout("s1") is True
    assert store.get("s1") is None
    # 没登过也不该报错：用户想回登入页，结果一样
    assert store.logout("s1") is False


def test_session_store_evicts_the_oldest_session():
    """容量上限是防呆：会话 id 由前端生成，不清就会一直攒着连接。"""
    store = MailSessionStore(factory=lambda *a, **k: make_store_client(), max_sessions=2)
    store.login("s1", "a@qq.com", "c", provider_by_key("qq"))
    store.login("s2", "b@qq.com", "c", provider_by_key("qq"))
    store.login("s3", "c@qq.com", "c", provider_by_key("qq"))

    assert store.get("s1") is None, "最老的会话应被顶掉"
    assert store.get("s2") is not None
    assert store.get("s3") is not None


def test_session_store_replaces_without_dropping_on_failure():
    """新登入失败时，原来登着的邮箱不能被挤掉。"""

    class Flaky:
        def __init__(self, address, secret, provider, **kwargs):
            if address.startswith("bad"):
                raise MailError("登不上")
            self.address = address
            self.closed = False

        def login(self):
            return self

        def close(self):
            self.closed = True

    store = MailSessionStore(factory=Flaky)
    good = store.login("s1", "good@qq.com", "c", provider_by_key("qq"))
    assert store.get("s1") is good

    with pytest.raises(MailError):
        store.login("s1", "bad@qq.com", "c", provider_by_key("qq"))
    assert store.get("s1") is good, "新登入失败不该把原来的会话顶掉"
    assert good.closed is False


def test_session_store_clear_closes_everything():
    store = MailSessionStore(factory=lambda *a, **k: make_store_client())
    store.login("s1", "a@qq.com", "c", provider_by_key("qq"))
    store.login("s2", "b@qq.com", "c", provider_by_key("qq"))
    store.clear()
    assert store.get("s1") is None and store.get("s2") is None
