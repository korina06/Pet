"""邮箱收信（IMAP）的纯逻辑：连接与取信，不碰 HTTP，只用标准库。

全是阻塞 IO，调用方必须把它丢进线程，否则慢邮箱会卡住整个事件循环。
"""

from __future__ import annotations

import base64
import binascii
import email
import email.utils
import html
import imaplib
import logging
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header

logger = logging.getLogger(__name__)

#: IMAP 的 ID 命令（RFC 2971）不在 imaplib 的内置命令表里，
#: 不注册的话 `_command()` 会直接拒绝执行。QQ / 163 都要求先发这个命令。
#: 注册是幂等的，模块重复导入也不会出问题。
imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))

#: 一次取回列表的四段（一次往返，且**不下载整封邮件**）：
#:   UID               这一封的真实 UID（**必须显式要**，见下面的说明）
#:   FLAGS             已读 / 星标
#:   INTERNALDATE      收件时间
#:   BODY[HEADER]      发件人 / 主题 / 日期 / 顶层编码
#:   BODY[1.MIME]      第一段自己的编码（解 base64 / quoted-printable 全靠它）
#:   BODY[1]<0.800>    第一段正文预览（multipart 走这条）
#:   BODY[TEXT]<0.800> 正文预览（非 multipart 时第一段不存在，走这条）
#: 截断到 800 字节是刻意的：带 20MB 附件的信也只取这几百字节。
_FETCH_QUERY = (
    "(UID FLAGS INTERNALDATE "
    "BODY.PEEK[HEADER] BODY.PEEK[1.MIME] "
    "BODY.PEEK[1]<0.800> BODY.PEEK[TEXT]<0.800>)"
)

#: 摘要在界面上只占一行，超过这个长度没有意义
SNIPPET_LIMIT = 140
#: 「星标邮件」是虚拟文件夹：跨所有文件夹搜索，单个文件夹最多收这么多封。
#: 这是**已知取舍**：跨文件夹取元数据要逐个 SELECT + FETCH，没上限会慢得离谱。
FLAGGED_PER_MAILBOX = 200
#: 一次翻页最多给多少封（接口层的上限，防止有人一次把整个邮箱拖下来）
MAX_PAGE_SIZE = 200


# --------------------------------------------------------------- 服务商


@dataclass(frozen=True)
class Provider:
    """一家邮件服务商的 IMAP 接入信息。"""

    key: str
    label: str
    host: str
    port: int = 993
    #: 显示"去哪拿授权码"的说明（不点链接，只给文字，避免引入外部依赖）
    help_text: str = ""
    #: 用这些域名判断用户填的邮箱属于哪家
    domains: tuple[str, ...] = ()


#: 本轮只做 QQ 邮箱（见模块顶部的说明）。加服务商只需要在这里加一条，
#: 前端会跟着 /api/mail/providers 自动多一个选项。
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="qq",
        label="QQ 邮箱",
        host="imap.qq.com",
        port=993,
        help_text=(
            "QQ 邮箱要填的不是 QQ 密码，而是「授权码」："
            "网页版 QQ 邮箱 → 设置 → 账号 → 开启「IMAP/SMTP 服务」→ 生成授权码（16 位）。"
        ),
        domains=("qq.com", "foxmail.com", "vip.qq.com"),
    ),
)

DEFAULT_PROVIDER_KEY = "qq"


def provider_by_key(key: str) -> Provider | None:
    wanted = (key or "").strip().lower()
    for provider in PROVIDERS:
        if provider.key == wanted:
            return provider
    return None


def provider_for_email(address: str) -> Provider | None:
    """按域名猜服务商。猜不到返回 None（由调用方给"暂不支持"的提示）。"""
    _, _, domain = (address or "").strip().lower().rpartition("@")
    domain = domain.strip()
    if not domain:
        return None
    for provider in PROVIDERS:
        if domain in provider.domains:
            return provider
    return None


def providers_public() -> list[dict]:
    """给 `/api/mail/providers` 用的公开信息（不含任何凭据）。"""
    return [
        {
            "key": p.key,
            "label": p.label,
            "host": p.host,
            "port": p.port,
            "help_text": p.help_text,
            "domains": list(p.domains),
        }
        for p in PROVIDERS
    ]


# --------------------------------------------------------------- 错误


class MailError(Exception):
    """能直接给用户看的中文错误。

    IMAP 抛出来的原始信息是英文的协议文本（`[AUTHENTICATIONFAILED] ...`），
    直接丢给用户等于没说；这里统一翻译成人话，技术细节进日志。
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------- 小工具


def _as_text(value) -> str:
    """把 imaplib 给的 bytes 拍成 str。

    用 latin-1 而不是 utf-8：这里只用来找 ASCII 记号（`BODY[HEADER]`、`{800}`），
    而 latin-1 对任意字节都不会抛异常——**解析失败也不能让列表整页崩掉**。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin-1")
    return "" if value is None else str(value)


def _norm(typ) -> str:
    return _as_text(typ).strip().upper()


def _first_text(data) -> str:
    for item in data or []:
        if isinstance(item, bytes):
            text = item.decode("utf-8", "replace").strip()
            if text:
                return text[:200]
        elif item:
            return str(item)[:200]
    return ""


def _quote(name: str) -> str:
    """IMAP astring 的引号形式。

    内部保留的一律是服务器给的原始邮箱名（modified UTF-7，本身就是 ASCII），
    所以这里只要处理引号和反斜杠，**不需要自己写 UTF-7 编码器**。
    """
    return '"' + str(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


#: 合法的 modified UTF-7 数据段只含 base64 字符（'/' 已被替换成 ','），
#: 而且**按规范不带 '=' 填充**。这里额外容忍 '='：真实服务商不该发出来，
#: 但收到就解掉没有任何风险，比"原样显示一串乱码"好。
_UTF7_B64_RE = re.compile(r"^[A-Za-z0-9+,/=]*$")


def imap_utf7_decode(raw) -> str:
    """IMAP 的 modified UTF-7 邮箱名 → 普通字符串（RFC 3501 §5.1.3）。

    ASCII 原样输出；其它字符写成 `&<UTF-16BE 的 base64，'/' 换成 ','>-`。
    标准库没有这个 codec，所以自己实现——**只需要解码方向**：
    内部一律保留服务器给的原始写法当 id，只有显示给用户时才解码。
    """
    text = _as_text(raw)
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = text.find("-", i)
        if end < 0:
            # 没有收尾的 '-'：格式坏了，原样留下，绝不吞字符
            out.append(text[i:])
            break
        if end == i + 1:
            out.append("&")                 # "&-" 是字面量 '&'
            i = end + 1
            continue
        chunk = text[i + 1:end]
        if _UTF7_B64_RE.match(chunk):
            padded = chunk.replace(",", "/")
            padded += "=" * (-len(padded) % 4)
            try:
                out.append(base64.b64decode(padded).decode("utf-16-be"))
                i = end + 1
                continue
            except (binascii.Error, UnicodeDecodeError, ValueError):
                pass
        out.append(text[i:end + 1])         # 解不开就原样显示，不丢信息
        i = end + 1
    return "".join(out)


_IMAP_DATE_FORMATS = (
    "%d-%b-%Y %H:%M:%S %z",
    "%d-%b-%Y %H:%M:%S",
)


def parse_imap_datetime(text: str) -> datetime | None:
    """解析 INTERNALDATE，形如 `01-Jan-2024 10:00:00 +0800`。

    注意 IMAP 用 `_d`（空格补齐）而不是 `%d`，所以个位数日期是 `" 1-Jan-2024"`；
    先 strip 掉再按 `%d` 解析。
    """
    value = (text or "").strip()
    if not value:
        return None
    for fmt in _IMAP_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def iso_or_none(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def epoch_or_zero(moment: datetime | None) -> float:
    try:
        return moment.timestamp() if moment is not None else 0.0
    except (OverflowError, OSError, ValueError):
        return 0.0


# --------------------------------------------------------- FETCH 响应解析

#: 一段 FETCH 数据的开头是"序号 ("，例如 `1 (UID 1501 FLAGS (\Seen) ...`
_SEQ_RE = re.compile(r"(\d+)\s*\(")
#: 段落名：BODY[HEADER] / BODY[1.MIME] / BODY[1]<0> / BODY[TEXT]<0>
_SECTION_RE = re.compile(r"BODY\[([^\]]*)\]")
_FLAGS_RE = re.compile(r"FLAGS \(([^)]*)\)")
_INTERNALDATE_RE = re.compile(r'INTERNALDATE "([^"]*)"')
#: `UID FETCH` 的响应里会带这个数据项，**它才是这封邮件的 UID**
_UID_ITEM_RE = re.compile(r"\bUID\s+(\d+)\b")


def parse_fetch_response(data) -> dict[str, dict]:
    """把一次多段 FETCH 的响应拆成 ``{uid: {flags, internaldate, sections}}``。

    imaplib 交出来的形状是这样的（每个字面量单独成 tuple）::

        [ (b'1 (UID 1501 FLAGS (\\\\Seen) INTERNALDATE "..." BODY[HEADER] {200}', b'<200字节>'),
          b' BODY[1.MIME] {80}',  b'<80字节>',
          b' BODY[1]<0> {800}',   b'<800字节>',
          b')',
          (b'2 (UID 1502 ...)', b'...'), ... ]

    所以这里用一个"记住当前这封"的小状态机：**只有带行首序号的那一小段**
    才是新邮件，后面的片段都归给当前这封。段落名直接从 `BODY[...]` 里读，
    不依赖字段出现的顺序。

    ⚠️ 键**必须**取 `UID <n>` 数据项，不能取行首那个数字——见 `_record_key()`。
    """
    records: dict[str, dict] = {}
    current: str | None = None

    def bucket(uid: str) -> dict:
        record = records.get(uid)
        if record is None:
            record = {"flags": [], "internaldate": "", "head": "", "sections": {}}
            records[uid] = record
        return record

    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            prefix = _as_text(item[0])
            literal = item[1] if len(item) > 1 else b""
            key = _record_key(prefix) or current
            if key is None:
                continue
            current = key
            record = bucket(key)
            if not record["head"]:
                record["head"] = prefix
            section = _section_of(prefix)
            if section is not None:
                blob = bytes(literal) if isinstance(literal, (bytes, bytearray)) else b""
                # setdefault：同一段落出现两次时以第一次为准，不覆盖
                record["sections"].setdefault(section, blob)
        else:
            text = _as_text(item)
            key = _record_key(text)
            if key is None:
                continue
            current = key
            record = bucket(key)
            if not record["head"]:
                record["head"] = text

    for record in records.values():
        flags = _FLAGS_RE.search(record["head"])
        record["flags"] = flags.group(1).split() if flags else []
        when = _INTERNALDATE_RE.search(record["head"])
        record["internaldate"] = when.group(1) if when else ""
    return records


def _record_key(text: str) -> str | None:
    """判断这一小段 FETCH 数据属于哪封邮件，返回它的 **UID**。

    **必须优先取 `UID <n>` 数据项。** `* 3 FETCH (...)` 开头那个 3 是
    **消息序号**，不是 UID；用 `UID FETCH` 取信时服务器会在响应里带上
    `UID <n>`，那才是调用方对号入座用的键。

    第 33/34 步这里直接把行首序号当 uid 用了，于是 records 的键是 1、2、3，
    而查找用的是真实 UID（真账号上是几千），**一封都对不上**——
    每封都被跳过，界面显示"这个文件夹还没有邮件"，而且不报错、不崩。
    序号只有在"从没删过信、UID 从 1 开始连续"时才碰巧等于 UID，
    所以假客户端里一直没暴露。

    取不到 UID 项时退回行首序号：普通（非 UID）FETCH 的响应里没有 UID，
    那时序号就是唯一能用的键。
    """
    found = _UID_ITEM_RE.search(text)
    if found:
        return found.group(1)
    return _seq_of(text)


def _seq_of(text: str) -> str | None:
    """行首的消息序号（`* 3 FETCH` 里的 3）。**它不等于 UID。**"""
    match = _SEQ_RE.match(text.lstrip())
    return match.group(1) if match else None


def _section_of(text: str) -> str | None:
    match = _SECTION_RE.search(text)
    return match.group(1) if match else None


# ------------------------------------------------------------ 头部与正文


def decode_header_value(raw) -> str:
    """解 RFC 2047 编码字（`=?UTF-8?B?...?=`），中文主题全靠它。"""
    text = _as_text(raw).strip()
    if not text:
        return ""
    try:
        parts = decode_header(text)
    except (binascii.Error, ValueError):
        return text
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, (bytes, bytearray)):
            try:
                out.append(bytes(chunk).decode(charset or "utf-8", "replace"))
            except LookupError:             # 服务商写了个不存在的 charset
                out.append(bytes(chunk).decode("utf-8", "replace"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def parse_headers(raw: bytes) -> dict:
    """从邮件头里取出要显示的三样：主题、发件人、日期。"""
    # 补一个空行：只给头块时 email 解析器会抱怨，补上就干净了
    message = email.message_from_bytes(bytes(raw or b"") + b"\r\n")
    subject = decode_header_value(message.get("Subject", ""))
    name, address = email.utils.parseaddr(decode_header_value(message.get("From", "")))
    when = None
    try:
        when = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        when = None
    return {
        "subject": subject,
        "name": name.strip(),
        "address": address.strip(),
        "date": when,
    }


_HTML_HINT_RE = re.compile(
    r"<\s*(?:!doctype|html|body|div|p|br|a|span|table|tr|td|ul|ol|li|"
    r"h[1-6]|img|font|blockquote|strong|em|style|head|meta)\b|</\s*[a-z]",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """把一段正文压成一行能放进列表的摘要。"""
    if _HTML_HINT_RE.search(text):
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
    return _SPACE_RE.sub(" ", text).strip()


def decode_body(part_headers: bytes, body: bytes, top_headers: bytes) -> str:
    """解出摘要文本。

    `part_headers` 是那一段自己的 MIME 头（`BODY[1.MIME]`）；拿不到就退回顶层头。
    传进来的正文可能只有前 800 字节，base64 / quoted-printable 会在半个字符上被截断，
    所以解码一律 `errors="replace"` —— **宁可末尾差一点，也不能抛异常让列表整页失败**。
    """
    head = bytes(part_headers or b"").strip()
    if not head:
        head = _top_level_part_headers(top_headers)
    message = email.message_from_bytes(head + b"\r\n\r\n" + bytes(body or b""))
    payload = message.get_payload(decode=True)
    if payload is None:
        payload = bytes(body or b"")
    charset = message.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, "replace")
    except LookupError:
        text = payload.decode("utf-8", "replace")
    return clean_text(text)


def _top_level_part_headers(headers: bytes) -> bytes:
    """非 multipart 时，第一段就是整封信体，编码信息在顶层头里。"""
    message = email.message_from_bytes(bytes(headers or b"") + b"\r\n")
    lines = []
    for name in ("Content-Type", "Content-Transfer-Encoding"):
        value = message.get(name)
        if value:
            lines.append(f"{name}: {value}")
    return "\r\n".join(lines).encode("latin-1", "replace")


# --------------------------------------------------------------- 文件夹


@dataclass(frozen=True)
class FolderSpec:
    key: str
    label: str
    #: 特殊用途标记（RFC 6154），优先用它认文件夹，比认名字可靠
    use: str = ""
    #: 认不出标记时按**解码后**的名字兜底
    names: tuple[str, ...] = ()
    virtual: bool = False


#: 左侧导航。只放 IMAP 能真实取到的四项：
#: 「重要联系人」是通讯录（IMAP 是邮件协议，标准里没有联系人接口）、
#: 「附件管理」也没有对应的接口（要扫全库 BODYSTRUCTURE 才知道谁带附件），
#: 这两项已跟用户确认本轮不做，而不是摆两个点不动的项。
FOLDERS: tuple[FolderSpec, ...] = (
    FolderSpec(key="inbox", label="收件箱", use="\\Inbox", names=("INBOX",)),
    FolderSpec(key="flagged", label="星标邮件", virtual=True),
    FolderSpec(key="drafts", label="草稿箱", use="\\Drafts",
               names=("Drafts", "草稿箱", "草稿")),
    FolderSpec(key="trash", label="垃圾桶", use="\\Trash",
               names=("Trash", "Deleted Messages", "已删除", "已删除邮件", "垃圾桶")),
)


def folder_spec(key: str) -> FolderSpec | None:
    wanted = (key or "").strip().lower()
    for spec in FOLDERS:
        if spec.key == wanted:
            return spec
    return None


def _is_selectable(flags: list[str]) -> bool:
    return "\\noselect" not in {flag.lower() for flag in flags}


def pick_mailbox(entries: list[tuple[list[str], str]], spec: FolderSpec) -> str | None:
    """在 `LIST` 的结果里找出这个逻辑文件夹对应的真实邮箱名。"""
    usable = [(flags, raw) for flags, raw in entries if _is_selectable(flags)]

    if spec.use:
        wanted = spec.use.lower()
        for flags, raw in usable:
            if wanted in {flag.lower() for flag in flags}:
                return raw

    names = {name.strip().lower() for name in spec.names}
    for _flags, raw in usable:
        if imap_utf7_decode(raw).strip().lower() in names:
            return raw
    return None


#: LIST 的响应：`(\HasNoChildren) "/" "INBOX"`，名字也可能是 NIL
_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>(?:[^"\\]|\\.)*)"|NIL)\s+(?P<name>.+)$',
    re.DOTALL,
)
#: 字面量还没到齐的行尾，例如 `{12}`
_LITERAL_TAIL_RE = re.compile(r"\{\d+\}$")


def flatten_list_data(data) -> list[str]:
    """把 LIST 的响应拍平成一行一个字符串。

    名字是字面量时 imaplib 会拆成 tuple（`{12}` + 真正的字节），
    所以要把 tuple 的两半接回去；极端情况下还会分成两行，用 pending 兜住。
    """
    lines: list[str] = []
    pending = ""
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head = _as_text(item[0])
            literal = _as_text(item[1]) if len(item) > 1 else ""
            cut = head.rfind("{")
            text = (head[:cut] if cut >= 0 else head) + literal
        else:
            text = _as_text(item)
        text = pending + text
        pending = ""
        if _LITERAL_TAIL_RE.search(text.rstrip()):
            pending = text                     # 字面量还没到，等下一段
            continue
        lines.append(text)
    if pending:
        lines.append(pending)
    return lines


def parse_list_data(data) -> list[tuple[list[str], str]]:
    """LIST 响应 → ``[(flags, 原始邮箱名)]``。"""
    out: list[tuple[list[str], str]] = []
    for line in flatten_list_data(data):
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        flags = match.group("flags").split()
        name = match.group("name").strip()
        if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out.append((flags, name))
    return out


def uids_of(data) -> list[bytes]:
    """SEARCH 的响应 → uid 列表（升序，最新的在最后）。

    **必须把所有响应行都拼起来**：IMAP 允许服务器把一长串 UID 拆成多条
    untagged 响应发回来。只读第一行的话，第一行之后的 UID 会被静默丢掉——
    表现成"邮箱里的历史邮件莫名其妙少了一截"，而且**全程没有任何报错**。
    """
    found: list[bytes] = []
    for item in data or []:
        if isinstance(item, (bytes, bytearray)):
            found.extend(bytes(item).split())
    return found


# --------------------------------------------------------------- 错误文案


_LOGIN_HINTS: tuple[tuple[str, str], ...] = (
    (
        "unsafe login",
        "QQ 邮箱以「Unsafe Login」拒绝了这次登入：它要求先发 IMAP ID 命令。"
        "这通常是客户端信息没被接受，请重试；若一直如此请把这条反馈给我。",
    ),
    (
        "authorization code",
        "QQ 邮箱要求用「授权码」登入，不是 QQ 密码。"
        "获取方式：网页版 QQ 邮箱 → 设置 → 账号 → 开启「IMAP/SMTP 服务」→ 生成授权码（16 位）。",
    ),
    (
        "login fail",
        "账号或授权码不正确。请确认填的是 QQ 邮箱生成的 16 位授权码，而不是 QQ 密码。",
    ),
)


def login_error_text(exc: Exception) -> str:
    raw = str(exc)
    low = raw.lower()
    for needle, message in _LOGIN_HINTS:
        if needle in low:
            return message
    if any(word in low for word in ("authenticationfailed", "auth", "invalid", "fail")):
        return "账号或授权码不正确。请确认填的是 QQ 邮箱生成的 16 位授权码，而不是 QQ 密码。"
    return f"登入失败：{raw[:200]}"


def network_error_text(exc: Exception, host: str) -> str:
    if isinstance(exc, socket.gaierror):
        return f"找不到邮件服务器 {host}，请检查网络连接。"
    if isinstance(exc, ssl.SSLError):
        return f"与 {host} 建立加密连接失败（TLS 握手没通过），可能是网络被干扰。"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"连接 {host} 超时，请检查网络后重试。"
    if isinstance(exc, ConnectionRefusedError):
        return f"{host} 拒绝连接。"
    return f"连接 {host} 失败：{str(exc)[:150]}"


def protocol_error_text(exc: Exception) -> str:
    return f"邮箱返回了错误：{str(exc)[:200]}"


def send_client_id(client) -> None:
    """发 IMAP ID（RFC 2971）。

    有些服务商（163 最典型）收不到这个命令会直接拒登，回一句
    `Unsafe Login. Please contact kefu@188.com for help`。
    imaplib 没有现成方法，得自己拼一条 ID 命令。
    **失败不当致命**：没有 ID 能力的服务器照样能正常收信。
    """
    try:
        client.xatom(
            "ID",
            '("name" "PetDesktop" "version" "0.1.0" "vendor" "pet_desktop")',
        )
    except Exception:  # noqa: BLE001 - ID 不被支持不该导致登不上
        logger.debug("IMAP ID 命令未被接受（不影响后续操作）", exc_info=True)


def default_imap_factory(host: str, port: int, timeout: float):
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


# --------------------------------------------------------------- 客户端


class MailboxClient:
    """一个邮箱会话。

    连接是**懒建 + 掉了重连**的：imaplib 的连接放着不用会被服务器掐掉，
    所以每次操作都先确认连接还在，操作中途断了就重连再试一次。
    所有操作都在 `self._lock` 里跑——**imaplib 不是线程安全的**，
    而接口层是把它丢进线程池执行的，多个请求可能同时压到同一个客户端上。
    """

    def __init__(
        self,
        address: str,
        secret: str,
        provider: Provider,
        *,
        host: str | None = None,
        port: int | None = None,
        imap_factory=None,
        timeout: float = 20.0,
        page_limit: int = 50,
    ):
        self.address = address
        self.provider = provider
        self.host = host or provider.host
        self.port = int(port or provider.port)
        self.page_limit = max(1, int(page_limit))
        self._secret = secret
        self._factory = imap_factory or default_imap_factory
        self._timeout = timeout
        self._client = None
        self._mailboxes: list[tuple[list[str], str]] | None = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:              # 别把凭据带进日志/异常里
        return f"MailboxClient(address={self.address!r}, host={self.host!r})"

    # ---------------------------------------------------------- 连接

    def login(self) -> "MailboxClient":
        """连上并认证。失败抛 MailError（带能直接给用户看的说明）。"""
        with self._lock:
            self._connect_locked()
        return self

    def close(self) -> None:
        with self._lock:
            self._drop_locked()

    def _connect_locked(self) -> None:
        if self._client is not None:
            return
        try:
            client = self._factory(self.host, self.port, self._timeout)
        except (OSError, ssl.SSLError) as exc:
            logger.info("连接邮箱 %s 失败：%s", self.host, exc)
            raise MailError(network_error_text(exc, self.host)) from exc

        try:
            client.login(self.address, self._secret)
        except imaplib.IMAP4.error as exc:
            logger.info("邮箱 %s 认证失败：%s", self.address, exc)
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - 关连接失败无所谓
                pass
            raise MailError(login_error_text(exc)) from exc

        send_client_id(client)
        self._client = client
        self._mailboxes = None

    def _drop_locked(self) -> None:
        client, self._client = self._client, None
        self._mailboxes = None
        if client is None:
            return
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - 已经断了的连接，无须关心
            pass

    def _ensure_locked(self):
        if self._client is None:
            self._connect_locked()
        return self._client

    def _run(self, action):
        """跑一次 IMAP 操作；**连接层**出错就重连一次再试。

        只对连接断掉这类异常重试（`IMAP4.abort` / `OSError`）。
        协议错误（`IMAP4.error`：认证失败、命令不被支持）**不重试**——
        重试只会把同一个错误再犯一遍，还会把报错信息搅浑。
        """
        with self._lock:
            try:
                return action(self._ensure_locked())
            except (imaplib.IMAP4.abort, OSError) as exc:
                logger.info("邮箱连接中断，重连一次：%s", exc)
                self._drop_locked()
                try:
                    return action(self._ensure_locked())
                except (imaplib.IMAP4.abort, OSError) as retry_exc:
                    self._drop_locked()
                    raise MailError(
                        network_error_text(retry_exc, self.host)
                    ) from retry_exc
                except imaplib.IMAP4.error as retry_exc:
                    raise MailError(protocol_error_text(retry_exc)) from retry_exc
            except imaplib.IMAP4.error as exc:
                raise MailError(protocol_error_text(exc)) from exc

    def _call(self, label: str, action) -> list:
        """执行一次调用，并要求服务器回 OK。"""

        def wrapped(client):
            typ, data = action(client)
            if _norm(typ) != "OK":
                detail = _first_text(data)
                raise MailError(
                    f"邮箱拒绝了「{label}」" + (f"：{detail}" if detail else "")
                )
            return data

        return self._run(wrapped)

    # ---------------------------------------------------------- 文件夹

    def mailbox_entries(self, refresh: bool = False) -> list[tuple[list[str], str]]:
        if self._mailboxes is None or refresh:
            data = self._call("列出文件夹", lambda client: client.list())
            self._mailboxes = parse_list_data(data)
        return self._mailboxes

    def list_folders(self) -> list[dict]:
        """左侧导航用。**取不到的文件夹直接不出现**，不摆一个点不动的项。"""
        entries = self.mailbox_entries()
        rows: list[dict] = []
        for spec in FOLDERS:
            row = {
                "key": spec.key,
                "label": spec.label,
                "mailbox": None,
                "unread": 0,
                "total": 0,
            }
            if spec.virtual:
                rows.append(row)
                continue
            raw = pick_mailbox(entries, spec)
            if raw is None:
                continue
            row["mailbox"] = raw
            counts = self._status(raw)
            row["unread"] = counts.get("UNSEEN", 0)
            row["total"] = counts.get("MESSAGES", 0)
            rows.append(row)
        return rows

    def _status(self, mailbox: str) -> dict[str, int]:
        """用 STATUS 拿数量——**不需要 SELECT**，也就不会打扰当前打开的文件夹。

        多要一个 `UIDNEXT`（下一封新邮件会拿到的 UID）：它是"有没有新邮件"的
        便宜探针，只要一次往返就能判断，不用把邮件重新下一遍。
        """
        data = self._call(
            "读取文件夹信息",
            lambda client: client.status(
                _quote(mailbox), "(MESSAGES UNSEEN UIDNEXT)"
            ),
        )
        text = " ".join(_as_text(item) for item in data or [])
        counts: dict[str, int] = {}
        for name in ("MESSAGES", "UNSEEN", "UIDNEXT"):
            match = re.search(rf"\b{name}\s+(\d+)", text)
            if match:
                counts[name] = int(match.group(1))
        return counts

    def _select(self, mailbox: str, readonly: bool = True) -> int:
        data = self._call(
            "打开文件夹",
            lambda client: client.select(_quote(mailbox), readonly=readonly),
        )
        try:
            return int(_as_text(data[0]).strip() or 0)
        except (IndexError, ValueError):
            return 0

    # ---------------------------------------------------------- 邮件

    def list_messages(
        self, folder_key: str, limit: int | None = None, offset: int = 0
    ) -> dict:
        """取一页邮件。返回 ``{messages, total, offset, has_more}``。

        **分页不是可有可无的**：以前这里只有"取最新 N 封"（`uids[-size:]`），
        于是超过 N 封的历史邮件**没有任何办法取到**——不是服务器挡着，
        是压根没提供入口。现在按 offset 往回翻，滚到底就能一直往早看。

        `offset` 是"已经给过多少封"。UID 是升序（最新在最后），
        所以要"最新在前"的第 k 页，得从**尾巴**往回切：
        ``uids[total-offset-size : total-offset]``，再由 `_fetch` 反转成最新在前。
        """
        spec = folder_spec(folder_key)
        if spec is None:
            raise MailError(f"不认识的文件夹：{folder_key}")
        size = max(1, min(int(limit or self.page_limit), MAX_PAGE_SIZE))
        offset = max(0, int(offset))

        if spec.virtual:
            return self._page_flagged(size, offset)

        raw = pick_mailbox(self.mailbox_entries(), spec)
        if raw is None:
            return {"messages": [], "total": 0, "offset": offset, "has_more": False}

        self._select(raw)
        data = self._call("搜索邮件", lambda client: client.uid("SEARCH", None, "ALL"))
        uids = uids_of(data)
        total = len(uids)

        end = total - offset
        start = max(0, end - size)
        if end <= 0:
            # 翻过头了：返回空页而不是报错，前端据此停下自动加载
            return {"messages": [], "total": total, "offset": offset, "has_more": False}

        return {
            "messages": self._fetch(raw, uids[start:end], spec.key),
            "total": total,
            "offset": offset,
            # start > 0 说明前面还有更早的没给
            "has_more": start > 0,
        }

    def check_new(self, folder_key: str, since_uid: int = 0) -> dict:
        """"有没有新邮件"的**便宜探针**（第 36 步）。

        前端切走再切回来时，不该把邮件重新下一遍——只需要问一句"有新东西吗"。
        返回 ``{messages, total, unread, reset}``：

        - `messages`：只包含**比 since_uid 更新**的那几封（虚拟文件夹例外，见下）
        - `reset=True`：让前端**整份替换**第一页，而不是插到前面
        - 没有新邮件时 `messages` 为空、且**一次 STATUS 就返回**：
          不 SELECT、不 FETCH、不下载任何邮件正文。

        「星标邮件」是跨文件夹搜出来的虚拟文件夹，**没有 UIDNEXT 这种全局序号可用**，
        所以只能整份重算并让前端替换（调用方要限流，见 frontend 的
        MAIL_CHECK_THROTTLE_MS）。
        """
        spec = folder_spec(folder_key)
        if spec is None:
            raise MailError(f"不认识的文件夹：{folder_key}")
        since = max(0, int(since_uid or 0))

        if spec.virtual:
            page = self._page_flagged(self.page_limit, 0)
            return {
                "messages": page["messages"],
                "total": page["total"],
                "unread": 0,
                "reset": True,
            }

        raw = pick_mailbox(self.mailbox_entries(), spec)
        if raw is None:
            return {"messages": [], "total": 0, "unread": 0, "reset": False}

        counts = self._status(raw)
        total = counts.get("MESSAGES", 0)
        unread = counts.get("UNSEEN", 0)
        uid_next = counts.get("UIDNEXT", 0)

        # 便宜路径：UIDNEXT 告诉我们"下一封新邮件的 UID"。
        # since + 1 就是"我这边之后的第一封"，UIDNEXT 没超过它 = 没有新邮件。
        if since and uid_next and uid_next <= since + 1:
            return {"messages": [], "total": total, "unread": unread, "reset": False}

        if not since:
            # 前端还没有任何基准，给第一页让它自己接上
            page = self.list_messages(folder_key, self.page_limit, 0)
            return {
                "messages": page["messages"],
                "total": page["total"],
                "unread": unread,
                "reset": True,
            }

        self._select(raw)
        data = self._call(
            "搜索新邮件",
            lambda client: client.uid("SEARCH", None, "UID", f"{since + 1}:*"),
        )
        uids = uids_of(data)
        rows = self._fetch(raw, uids[-MAX_PAGE_SIZE:], spec.key) if uids else []
        return {"messages": rows, "total": total, "unread": unread, "reset": False}

    def _collect_flagged(self) -> list[dict]:
        """把所有能打开的文件夹里的星标邮件收齐，按时间倒序。

        单个文件夹读不了（权限、标记为 Noselect 的容器之类）不该让整个视图打不开，
        所以逐个 try。

        每个文件夹只取最新的 `FLAGGED_PER_MAILBOX` 封来排序：跨文件夹取元数据
        本身就要逐个 SELECT + FETCH，没有上限的话打开一次「星标邮件」会慢得离谱。
        这个上限是**已知取舍**，README 里记了。
        """
        rows: list[dict] = []
        for flags, raw in self.mailbox_entries():
            if not _is_selectable(flags):
                continue
            try:
                self._select(raw)
                data = self._call(
                    "搜索星标邮件",
                    lambda client: client.uid("SEARCH", None, "FLAGGED"),
                )
            except MailError as exc:
                logger.info("跳过文件夹 %s：%s", raw, exc.message)
                continue
            uids = uids_of(data)
            if uids:
                rows.extend(self._fetch(raw, uids[-FLAGGED_PER_MAILBOX:], "flagged"))
        rows.sort(key=lambda row: row.get("ts") or 0.0, reverse=True)
        return rows

    def _page_flagged(self, size: int, offset: int) -> dict:
        rows = self._collect_flagged()
        total = len(rows)
        return {
            "messages": rows[offset:offset + size],
            "total": total,
            "offset": offset,
            "has_more": offset + size < total,
        }

    def _fetch(self, mailbox: str, uids: list[bytes], folder_key: str) -> list[dict]:
        if not uids:
            return []
        uidset = b",".join(uids)
        data = self._call(
            "读取邮件",
            lambda client: client.uid("FETCH", uidset, _FETCH_QUERY),
        )
        records = parse_fetch_response(data)

        rows: list[dict] = []
        # 服务器给的是升序（最新的在最后），列表要的是最新在前
        for raw_uid in reversed(uids):
            uid = _as_text(raw_uid)
            record = records.get(uid)
            if record is None:
                # 解析不出来就跳过这一封，而不是让整页失败
                logger.info("有一封邮件没能解析出元数据（uid=%s）", uid)
                continue
            rows.append(self._row(uid, mailbox, folder_key, record))

        # "要了一整页却一封都没解析出来"是**解析出问题**，不是这个文件夹没邮件。
        # 这类失败以前太安静了：界面上只是"这个文件夹还没有邮件。"，
        # 谁也想不到是解析器把响应读错了（第 35 步就是这么栽的）。
        if uids and not rows:
            logger.warning(
                "读了 %d 封邮件却一封都没解析出来（文件夹 %s）。"
                "这通常是响应解析对不上号，不是文件夹为空。",
                len(uids),
                mailbox,
            )
        return rows

    @staticmethod
    def _row(uid: str, mailbox: str, folder_key: str, record: dict) -> dict:
        sections = record.get("sections") or {}
        headers = sections.get("HEADER", b"")
        parsed = parse_headers(headers)

        snippet = ""
        if sections.get("1"):
            snippet = decode_body(sections.get("1.MIME", b""), sections["1"], headers)
        elif sections.get("TEXT"):
            # 第一段不存在 = 这封信不是 multipart，正文就是 BODY[TEXT]
            snippet = decode_body(b"", sections["TEXT"], headers)

        received = parse_imap_datetime(record.get("internaldate", ""))
        if received is None:
            received = parsed["date"]

        flags = {flag.lower() for flag in record.get("flags") or []}
        subject = parsed["subject"] or "（无主题）"
        return {
            "uid": uid,
            "mailbox": mailbox,
            "folder": folder_key,
            "from_name": parsed["name"] or parsed["address"] or "（未知发件人）",
            "from_address": parsed["address"],
            "subject": subject,
            "snippet": snippet[:SNIPPET_LIMIT],
            "received": iso_or_none(received),
            # ts 只给后端排序用；响应模型里没有这个字段，不会漏给前端
            "ts": epoch_or_zero(received),
            "unread": "\\seen" not in flags,
            "flagged": "\\flagged" in flags,
        }

    # ---------------------------------------------------------- 星标

    def set_flagged(self, mailbox: str, uid: str, flagged: bool) -> bool:
        if not mailbox:
            raise MailError("缺少文件夹信息，无法修改星标")
        if not str(uid).strip().isdigit():
            raise MailError("邮件编号不合法")
        self._select(mailbox, readonly=False)
        op = "+FLAGS" if flagged else "-FLAGS"
        self._call(
            "修改星标",
            lambda client: client.uid("STORE", str(uid), op, "(\\Flagged)"),
        )
        return flagged


# --------------------------------------------------------------- 会话


class MailSessionStore:
    """按会话 id 记住已登入的邮箱客户端。

    **只活在进程内存里**：重启服务即失效，不落盘、不进日志。
    用户本轮明确选了"暂时只放内存"；落盘加密（Windows DPAPI）记在 README 待办里。

    容量上限是防呆：会话 id 由前端生成，不清空的话进程会一直攒着连接。
    """

    def __init__(self, factory=None, *, max_sessions: int = 8):
        self._clients: dict[str, MailboxClient] = {}
        self._factory = factory or MailboxClient
        self._max = max(1, int(max_sessions))
        self._lock = threading.Lock()

    def login(
        self,
        session_id: str,
        address: str,
        secret: str,
        provider: Provider,
        *,
        host: str | None = None,
        port: int | None = None,
    ) -> MailboxClient:
        # 先连上再顶掉旧的：填错了不该把原来登着的邮箱挤掉
        client = self._factory(address, secret, provider, host=host, port=port)
        client.login()

        with self._lock:
            previous = self._clients.pop(session_id, None)
            self._clients[session_id] = client
            evicted = []
            while len(self._clients) > self._max:
                oldest = next(iter(self._clients))
                evicted.append(self._clients.pop(oldest))

        if previous is not None:
            previous.close()
        for stale in evicted:
            stale.close()
        return client

    def get(self, session_id: str) -> MailboxClient | None:
        with self._lock:
            return self._clients.get(session_id)

    def logout(self, session_id: str) -> bool:
        with self._lock:
            client = self._clients.pop(session_id, None)
        if client is None:
            return False
        client.close()
        return True

    def clear(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
