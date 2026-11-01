"""签到功能的纯逻辑层：日历网格、当天判定、签到与补签，以及配套的农历/节日信息。

只依赖标准库，便于被测试直接调用；持久化交给 storage，没有 storage 时退回内存。
"""

from __future__ import annotations

import logging
import threading
import time
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core import holidays, lunar
from core.storage import MIGRATIONS, register_migration

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- 常量


#: 日历网格固定 7 列（周一到周日，与 date.weekday() 的顺序一致）。
WEEKDAY_COLUMNS = 7
WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")

#: 三种状态码。前端按码上色、按 status_note 写字，不自己拼中文。
STATUS_DONE = "done"
STATUS_TODO = "todo"
STATUS_FUTURE = "future"

STATUS_NOTES = {
    STATUS_DONE: "已签",
    STATUS_TODO: "未签",
    STATUS_FUTURE: "",          # 还没到的日期：按用户要求"不算"，不写字
}

#: 能回看/补签的最早日期。再往前没有意义，也顺手给翻月按钮一个绝对下限。
MIN_DATE = date(2020, 1, 1)

#: 能往后翻的最晚日期（用户第 5 条：支持**自由翻到 2050 年**）。
#:
#: 上限不是"今天"，因为用户要能看未来的日历（只是不显示签到状态、不可点）。
#: 取 2050 年还有另一个原因：农历表只做到 2050 农历年（`core/lunar.py`），
#: 再往后农历日期就算不出来了，与其显示错的，不如把可翻范围与数据范围对齐。
MAX_DATE = date(2050, 12, 31)

#: 补签要消耗的**道具卡**初始数量。
#:
#: 用户第 4 条：补签要消耗道具卡，没有就提示"未持有补签卡"；
#: 道具本身"后面再做"，现在只做消耗与提示。所以初始数量给 **0**：
#: 一打开就能看到"未持有补签卡"这句提示（正是用户要的行为），
#: 而不是凭空发卡、让人以为道具系统已经做好了。
#: 将来做道具系统时把它换成真的来源即可（发卡 / 兑换 / 每日赠送）。
MAKEUP_CARDS_START = 0

#: 道具卡的名字，以及没有卡时给用户看的那句话（用户第 4 条指定的文案）。
MAKEUP_CARD_NAME = "补签卡"
NO_CARD_MESSAGE = "未持有补签卡"

#: 单个会话最多留多少条签到记录（一天一条，10 年也用不完）。
MAX_RECORDS = 3660


# --------------------------------------------------------------- 落库（v8）
#
# 两张表，各管一件事：
#   - checkin_days：一天一行，(session_id, day) 做主键 → 同一天签两次天然不重复，
#     不用在代码里先查后插（并发下那种写法会重复插入）。
#   - checkin_cards：一个会话一行，存剩下的补签卡。
# 拆两张而不是塞一个 JSON 字符串：卡会单独变动（扣一张），
# 用 JSON 就要"读-改-写整份"，正好是丢数据最经典的写法。

SCHEMA_V8 = (
    """
    CREATE TABLE IF NOT EXISTS checkin_days (
        session_id TEXT NOT NULL,
        day TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkin_cards (
        session_id TEXT PRIMARY KEY,
        cards INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
)


def register() -> None:
    """幂等登记 v8（由 `ensure_schema()` 显式调用，见第 91 步的教训）。"""
    if 8 not in MIGRATIONS:
        register_migration(SCHEMA_V8, version=8)


#: import 时也登记一份：`scripts/check_frontend.py` 之类单独 import 本模块的路径
#: 不该因为"没走 ensure_schema"就拿不到表结构。
register()


class CheckinError(ValueError):
    """签到相关的输入错误。接口层把它转成 400 + 这句中文。"""


class NoMakeupCard(CheckinError):
    """补签时没有道具卡。单独一类，接口层据此给出 NO_CARD_MESSAGE。"""


# --------------------------------------------------------------- 会话记录


@dataclass
class CheckinSession:
    """一个会话的签到记录：一个"签过哪几天"的集合 + 剩下的道具卡。

    刻意只存**已签的日期**：未签和未到都是"没记录"推导出来的状态，
    存下来反而会出现"记录说未签，但那天其实还没到"这种自相矛盾的数据。

    这个 dataclass 只在**没有 storage**（测试 / 降级启动）时用；
    有 storage 时真值在 SQLite 里，不在这里留副本（见模块说明）。
    """

    session_id: str
    days: set[str] = field(default_factory=set)
    makeup_cards: int = MAKEUP_CARDS_START
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def sorted_days(self) -> list[str]:
        return sorted(self.days)

    def latest(self) -> str:
        return max(self.days) if self.days else ""


def _now() -> str:
    """库里存的时间戳（UTC ISO，`core/history.py` 用的是同一种）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CheckinArchive:
    """按会话 id 记签到。

    - **传了 `storage`**（主程序）：读写都走 SQLite，重启不丢。
    - **没传**（测试、库打不开的降级启动）：退回进程内存，重启即清空。

    两种模式都保留同一套方法签名，所以接口层完全不用关心用的是哪种——
    它只调 `checked_days / sign / cards / spend_makeup_card`。

    锁：内存模式用 `threading.Lock`（纯字典读写、没有 await，线程锁更省心）。
    SQLite 模式直接靠 `Storage` 自己的 `RLock`，事务边界也交给它。
    """

    def __init__(
        self,
        *,
        storage: Any | None = None,
        max_sessions: int = 16,
        max_records: int = MAX_RECORDS,
    ) -> None:
        self._storage = storage
        self._sessions: dict[str, CheckinSession] = {}
        self._max_sessions = max(1, int(max_sessions))
        self._max_records = max(1, int(max_records))
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 模式判断

    @property
    def persistent(self) -> bool:
        """是否落盘。接口与启动日志用它说明"重启会不会丢"。"""
        return self._storage is not None

    def ensure(self, session_id: str) -> CheckinSession:
        """取/建**内存**会话。持久模式下没有内存会话，用 `set_cards()`。"""
        if self.persistent:
            raise RuntimeError(
                "持久模式下没有内存会话：请用 set_cards()/sign()，真值在 SQLite 里"
            )
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = CheckinSession(session_id=session_id)
                self._sessions[session_id] = session
                while len(self._sessions) > self._max_sessions:
                    oldest = next(iter(self._sessions))
                    if oldest == session_id:        # 别把自己挤掉
                        break
                    self._sessions.pop(oldest, None)
            session.updated_at = time.time()
            return session

    def get(self, session_id: str) -> CheckinSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    # ------------------------------------------------------------ 读

    def checked_days(self, session_id: str) -> set[str]:
        if self._storage is None:
            session = self.get(session_id)
            return set(session.days) if session is not None else set()
        rows = self._storage.query(
            "SELECT day FROM checkin_days WHERE session_id = ?", (session_id,)
        )
        return {row["day"] for row in rows}

    def cards(self, session_id: str) -> int:
        """查这个会话还剩几张补签卡。没记录就是初始数量（=0）。"""
        if self._storage is None:
            session = self.get(session_id)
            return session.makeup_cards if session is not None else MAKEUP_CARDS_START
        row = self._storage.query_one(
            "SELECT cards FROM checkin_cards WHERE session_id = ?", (session_id,)
        )
        return int(row["cards"]) if row is not None else MAKEUP_CARDS_START

    # ------------------------------------------------------------ 写

    def set_cards(self, session_id: str, cards: int) -> int:
        """把补签卡设成 N 张（道具系统还没做，现在只有测试/运维用它）。"""
        count = max(0, int(cards))
        if self._storage is None:
            self.ensure(session_id).makeup_cards = count
            return count
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO checkin_cards (session_id, cards, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " cards = excluded.cards, updated_at = excluded.updated_at",
                (session_id, count, _now()),
            )
        return count

    def spend_makeup_card(self, session_id: str) -> int:
        """补签前扣一张卡，返回扣完还剩几张；没有卡就抛 NoMakeupCard。

        **先扣再签**：真实场景里"扣了卡但签到失败"比"签上了但没扣卡"
        更难解释，所以让扣卡成为签到的前置条件——扣不成就别往下走。
        重复补签同一天时不该扣卡，所以调用方要先判"这天是不是已经签过"
        （接口层就是这么做的）。
        """
        if self._storage is None:
            session = self.ensure(session_id)
            with self._lock:
                if session.makeup_cards <= 0:
                    raise NoMakeupCard(NO_CARD_MESSAGE)
                session.makeup_cards -= 1
                session.updated_at = time.time()
                return session.makeup_cards

        # 扣减写成一条带条件的 UPDATE（cards > 0）：
        # "先查余额再减"在并发下会把同一张卡用两次。
        with self._storage.transaction() as conn:
            cursor = conn.execute(
                "UPDATE checkin_cards SET cards = cards - 1, updated_at = ?"
                " WHERE session_id = ? AND cards > 0",
                (_now(), session_id),
            )
            # 第 145 步：**看 rowcount**，而不是"先查再减"（评价9 P2）。
            # 单进程 asyncio 下检查-扣卡之间没有 await，所以现在不会交错；
            # 但这行代码的正确性不该建立在"只有一个 worker"上 ——
            # 条件 UPDATE + rowcount 判断在并发下也是对的，成本只是多一个判断。
            if cursor.rowcount != 1:
                # 没改成行 = 没卡（或这一行根本不存在）。两种都按"没有卡"处理。
                raise NoMakeupCard(NO_CARD_MESSAGE)
            row = conn.execute(
                "SELECT cards FROM checkin_cards WHERE session_id = ?", (session_id,)
            ).fetchone()
            return int(row["cards"]) if row is not None else 0

    def sign_with_card(self, session_id: str, day: date) -> tuple[bool, int]:
        """补签：**一个事务里**完成「查重 → 扣卡 → 写签到」。

        评价11 P3-3：以前是三段分开的 —— 路由先 `checked_days()` 查重、
        再 `spend_makeup_card()` 扣卡、最后 `sign()` 写入。两个并发请求都能通过
        查重，于是**各扣一张卡、只有一次签到生效**（卡白扣一张）。

        这里用一条条件 UPDATE 扣卡 + INSERT OR IGNORE 写签到，两步在同一个
        `transaction()` 里：要么都发生，要么都不发生。返回 `(fresh, 剩余卡数)`。
        没有卡时抛 :class:`NoMakeupCard`（与 `spend_makeup_card` 同一套语义）。
        """
        key = day.isoformat()

        if self._storage is None:
            session = self.ensure(session_id)
            with self._lock:
                if key in session.days:
                    return False, session.makeup_cards
                if session.makeup_cards <= 0:
                    raise NoMakeupCard(NO_CARD_MESSAGE)
                session.makeup_cards -= 1
                session.days.add(key)
                session.updated_at = time.time()
                return True, session.makeup_cards

        with self._storage.transaction() as conn:
            already = conn.execute(
                "SELECT 1 FROM checkin_days WHERE session_id = ? AND day = ?",
                (session_id, key),
            ).fetchone()
            if already is not None:
                row = conn.execute(
                    "SELECT cards FROM checkin_cards WHERE session_id = ?", (session_id,)
                ).fetchone()
                return False, int(row["cards"]) if row is not None else 0

            cursor = conn.execute(
                "UPDATE checkin_cards SET cards = cards - 1, updated_at = ?"
                " WHERE session_id = ? AND cards > 0",
                (_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise NoMakeupCard(NO_CARD_MESSAGE)

            conn.execute(
                "INSERT OR IGNORE INTO checkin_days (session_id, day, created_at)"
                " VALUES (?, ?, ?)",
                (session_id, key, _now()),
            )
            conn.execute(
                "DELETE FROM checkin_days WHERE session_id = ? AND day NOT IN ("
                "  SELECT day FROM checkin_days WHERE session_id = ?"
                "  ORDER BY day DESC LIMIT ?"
                ")",
                (session_id, session_id, self._max_records),
            )
            row = conn.execute(
                "SELECT cards FROM checkin_cards WHERE session_id = ?", (session_id,)
            ).fetchone()
            return True, int(row["cards"]) if row is not None else 0

    def sign(self, session_id: str, day: date) -> bool:
        """签一天，返回**这次是不是新签的**。

        重复点同一天不算错（用户手抖再点一下不该报错），返回 False，
        界面据此说"这天已经签过了"，而不是假装又签了一次。
        """
        key = day.isoformat()

        if self._storage is None:
            session = self.ensure(session_id)
            with self._lock:
                if key in session.days:
                    return False
                session.days.add(key)
                # 防呆上限：真到了这个量级（十年每天不落），丢最早的几条
                if len(session.days) > self._max_records:
                    for old in sorted(session.days)[: len(session.days) - self._max_records]:
                        session.days.discard(old)
                session.updated_at = time.time()
            return True

        # INSERT OR IGNORE + 主键 (session_id, day)：同一天重复签由数据库保证不重复，
        # rowcount 直接告诉我们"这次是不是真插进去了"。
        with self._storage.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO checkin_days (session_id, day, created_at)"
                " VALUES (?, ?, ?)",
                (session_id, key, _now()),
            )
            fresh = cursor.rowcount > 0
            if fresh:
                # 防呆上限：只留最近的 N 天（数据库版，一条 DELETE 搞定）
                conn.execute(
                    "DELETE FROM checkin_days WHERE session_id = ? AND day NOT IN ("
                    "  SELECT day FROM checkin_days WHERE session_id = ?"
                    "  ORDER BY day DESC LIMIT ?"
                    ")",
                    (session_id, session_id, self._max_records),
                )
        return fresh

    def reset(self, session_id: str) -> bool:
        if self._storage is None:
            with self._lock:
                return self._sessions.pop(session_id, None) is not None
        with self._storage.transaction() as conn:
            removed = conn.execute(
                "DELETE FROM checkin_days WHERE session_id = ?", (session_id,)
            ).rowcount
            conn.execute(
                "DELETE FROM checkin_cards WHERE session_id = ?", (session_id,)
            )
        return bool(removed)

    def clear(self) -> None:
        if self._storage is None:
            with self._lock:
                self._sessions.clear()
            return
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM checkin_days")
            conn.execute("DELETE FROM checkin_cards")


# --------------------------------------------------------------- 日期工具


def parse_iso_date(text: str, *, field_name: str = "日期") -> date:
    """严格解析 `YYYY-MM-DD`。解析不了就抛 CheckinError（接口层转 400）。

    为什么要自己卡一遍格式，而不是直接 `date.fromisoformat()`：
    **Python 3.11 起 fromisoformat 也认紧凑写法**（"20260318"），
    于是同一天会有两个不同的键（"2026-03-18" 与 "20260318"），
    用户点两次就能把一天签成两条——这种"看起来能用、数据却脏了"的坑最难查。
    所以先把形状卡死（10 位、位置上是数字或横杠），再交给 fromisoformat
    做真正的合法性校验（月份 13、2 月 30 日这类它管）。
    """
    raw = (text or "").strip()
    shaped = (
        len(raw) == 10
        and raw[4] == "-"
        and raw[7] == "-"
        and all(char.isdigit() for index, char in enumerate(raw) if index not in (4, 7))
    )
    if shaped:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    raise CheckinError(f"{field_name}格式不对（应为 YYYY-MM-DD）：{raw or '空'}")


def parse_month(text: str, *, today: date) -> date:
    """把 `YYYY-MM` 解析成那个月的 1 号；空值或格式不对就回落到"今天这个月"。

    这里刻意**不报错**：month 参数是翻月按钮给的，一个翻页动作
    不值得弹一个红色错误——回落到当月，界面照常能用。
    """
    raw = (text or "").strip()
    if not raw:
        return today.replace(day=1)
    try:
        year_text, month_text = raw.split("-", 1)
        return date(int(year_text), int(month_text), 1)
    except (ValueError, TypeError):
        return today.replace(day=1)


def day_status(day: date, *, today: date, checked: set[str]) -> str:
    """一天的三种状态之一。顺序很重要：先判"还没到"，再判签没签。"""
    if day > today:
        return STATUS_FUTURE
    return STATUS_DONE if day.isoformat() in checked else STATUS_TODO


def day_calendar_info(day: date) -> dict[str, str]:
    """这一天的"日历信息"：农历、节日、休/班。

    单独抽成函数是因为这三个字段是**纯日历事实**，与签到状态无关：
    签没签是用户数据，农历/节日/调休是历法数据，混在一起以后很难复用
    （比如「状态」「任务」两个窗口将来也要显示日期）。
    """
    lunar_date = lunar.lunar_of(day)
    festivals = lunar.festivals_of(day, lunar_date)
    status = holidays.status_of(day)
    holiday = holidays.holiday_name(day)

    # 提示里把"所有节日 + 农历全称 + 假期名"拼起来（格子里放不下），并且去重：
    # 国庆节既是"节日"也是"假期名"，不去重会写成"国庆节 八月廿一 国庆节"。
    parts: list[str] = []
    for part in (*festivals, lunar_date.month_name + lunar_date.day_name if lunar_date else "", holiday):
        if part and part not in parts:
            parts.append(part)

    return {
        # 第二行优先显示节日名，没有节日才显示农历（见前端渲染）
        "lunar": lunar.lunar_label(day, lunar_date),
        "festival": festivals[0] if festivals else "",
        # 悬停提示：把所有节日、农历全称都写出来，格子里放不下
        "calendar_tip": " ".join(parts),
        # 休 / 班
        "rest": status,
        "rest_label": holidays.label_of(status),
    }


def _month_first(day: date) -> date:
    return day.replace(day=1)


def _month_last(day: date) -> date:
    """这个月的最后一天（用下个月月初减一天算，闰年二月也不会错）。"""
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def streak_of(checked: set[str], *, today: date) -> int:
    """连续签到天数。

    规则（写成明文，免得将来"为什么是 3 不是 4"说不清）：
    - 今天签了 → 从今天往回数；
    - 今天还没签 → 从昨天往回数（今天没签不该立刻把之前的连续清零，
      用户晚上才想起来签也很正常）。

    再往前的断档就重新计数。
    """
    if not checked:
        return 0
    cursor = today if today.isoformat() in checked else today - timedelta(days=1)
    count = 0
    while cursor.isoformat() in checked:
        count += 1
        cursor -= timedelta(days=1)
    return count


def month_grid(
    view_month: date,
    *,
    today: date,
    checked: set[str],
) -> list[dict[str, object]]:
    """排好版的当月网格：必要时**补上个月末尾的空格**，凑成整周。

    空格的 `date` 为空串、`status` 为 None——前端据此画一个占位格，
    而不是把 1 号画到周日的列上（错一格是日历最常见的错）。
    """
    leading = view_month.weekday()                 # 周一 = 0
    rows: list[dict[str, object]] = [
        {"date": "", "day": 0, "status": None, "note": "", "clickable": False,
         "is_today": False, "past": False, "lunar": "", "festival": "",
         "calendar_tip": "", "rest": "", "rest_label": ""}
        for _ in range(leading)
    ]

    total = monthrange(view_month.year, view_month.month)[1]
    for number in range(1, total + 1):
        day = view_month.replace(day=number)
        status = day_status(day, today=today, checked=checked)
        rows.append(
            {
                "date": day.isoformat(),
                "day": number,
                "status": status,
                "note": STATUS_NOTES[status],
                # 过去与今天都可点（过去的是补签），还没到的不可点。
                "clickable": status != STATUS_FUTURE,
                "is_today": day == today,
                # 早于今天 = 点它就是补签（要消耗道具卡）。前端据此先问一句，
                # 但**卡够不够由后端说了算**，这个字段只用于"要不要弹确认"。
                "past": day < today,
                # 农历 / 节日 / 休班：纯历法事实（见 day_calendar_info）
                **day_calendar_info(day),
            }
        )
    return rows


def month_view(
    view_month: date,
    *,
    today: date,
    checked: set[str],
    cards: int = 0,
) -> dict[str, object]:
    """一整月的可渲染视图。接口层原样塞进响应模型，不做二次加工。"""
    days = month_grid(view_month, today=today, checked=checked)
    prev_month = (view_month - timedelta(days=1)).replace(day=1)
    next_month = (view_month + timedelta(days=31)).replace(day=1)

    # 翻月按钮的可用范围（第 146 步按用户要求放宽）：
    #   - 下限 = MIN_DATE（2020-01）；
    #   - 上限 = MAX_DATE（2050-12），**不再卡在"本月"** —— 用户要能往后翻，
    #     只是未来的日子不给签到状态、不可点。
    #
    # 原先是"最早一年前、最晚本月"。那两个限制对应的需求（"别让人翻太远走丢"）
    # 已经被"自由翻到 2050"取代；而且农历/调休数据本身有范围，
    # 把可翻范围与数据范围对齐，界面就不会出现"翻了 20 年但一格农历都没有"。
    can_prev = view_month > _month_first(MIN_DATE)
    can_next = view_month < _month_first(MAX_DATE)

    # 刻意**不返回**本月已签/未签天数与"今天"日期：用户第 3 条说不要那行统计，
    # 第 2 条说不要日历上那个小时间提醒。少返回三个字段，前端也就少三处可漂移。
    return {
        "month": view_month.strftime("%Y-%m"),
        "month_label": f"{view_month.year} 年 {view_month.month} 月",
        "leading": sum(1 for row in days if not row["date"]),
        "days": days,
        "weekdays": list(WEEKDAY_NAMES),
        "streak": streak_of(checked, today=today),
        "prev_month": prev_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "can_prev": can_prev,
        "can_next": can_next,
        # 道具卡的名字也放在月视图里：点补签时**先弹确认框再发请求**，
        # 那一刻前端手里只有月视图，没有名字就只能拼出一句"「」"。
        "card_name": MAKEUP_CARD_NAME,
        # 还剩几张卡（评价15 T1-5）：同上，前端要在弹框**之前**就知道有没有卡，
        # 一张都没有时直接在格子上说"没有补签卡了"，别弹一个注定失败的确认框。
        "cards": cards,
        # 没卡时那句话也由后端给（评价15 T1-5）：前端不硬编码中文，
        # 和补签失败时挂到格子上的那句保持同一份文案。
        "card_note": NO_CARD_MESSAGE,
        # 休/班 的显示文字（"休"/"班"）由后端给；没数据的年份为空
        "rest_labels": {"off": holidays.label_of(holidays.REST),
                        "work": holidays.label_of(holidays.WORK)},
        "status_labels": {
            STATUS_DONE: STATUS_NOTES[STATUS_DONE],
            STATUS_TODO: STATUS_NOTES[STATUS_TODO],
        },
    }
