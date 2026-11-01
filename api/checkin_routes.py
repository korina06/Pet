"""签到接口层：取整月视图、给某一天签到（含补签），业务在 `core/checkin.py`。

POST 返回那一天所在月的整月视图，前端整体重画，不用自己判断该刷新哪一月。
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.checkin import (
    MAKEUP_CARD_NAME,
    MAX_DATE,
    MIN_DATE,
    NO_CARD_MESSAGE,
    STATUS_DONE,
    STATUS_TODO,
    CheckinArchive,
    CheckinError,
    NoMakeupCard,
    month_view,
    parse_iso_date,
    parse_month,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/checkin", tags=["checkin"])


# --------------------------------------------------------------- 请求/响应


class CheckinRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    #: 要签的那一天（YYYY-MM-DD）。过去的日子也允许，就是补签。
    date: str = Field(min_length=1, max_length=10)
    #: 这是不是补签。补签要消耗道具卡，没有卡就签不了（用户第 4 条）。
    #: 由前端按"这一天早于今天"判断后传过来，服务端再核一遍。
    makeup: bool = False


class DayOut(BaseModel):
    #: 空白格（当月 1 号之前的占位）没有日期，是空串
    date: str = ""
    #: 日期数字；空白格为 0
    day: int = 0
    #: 'done'（已签）/ 'todo'（未签）/ 'future'（还没到）/ None（空白格）
    status: str | None = None
    #: 显示在日期下面的两个字："已签" / "未签" / 空
    note: str = ""
    #: 今天与过去可点，还没到的不可点
    clickable: bool = False
    is_today: bool = False
    #: 早于今天 = 点它就是补签（要消耗道具卡）
    past: bool = False
    #: 农历文字（"廿四" / 初一显示"九月"）；历表范围外为空
    lunar: str = ""
    #: 节日名（"国庆节" / "中秋节"…）；没有节日为空
    festival: str = ""
    #: 悬停提示：所有节日 + 农历全称 + 假期名（格子里放不下的都在这）
    calendar_tip: str = ""
    #: 放假 / 调休：'off'（休）/ 'work'（班）/ ''（普通日子）
    rest: str = ""
    #: 'off' → "休"，'work' → "班"（文字由后端给，前端不硬编码）
    rest_label: str = ""


class StatusLabels(BaseModel):
    """状态码 → 中文。**由后端给**，前端不硬编码这两个词。"""

    done: str = STATUS_DONE
    todo: str = STATUS_TODO


class RestLabels(BaseModel):
    """休 / 班 两个字，同样由后端给。"""

    off: str = "休"
    work: str = "班"


class MonthResponse(BaseModel):
    session_id: str
    #: 请求里带的/生效的那个月（YYYY-MM）
    month: str
    #: 给人看的月份标题，例如 "2026 年 3 月"
    month_label: str
    #: 1 号之前要空几格（周一起算）
    leading: int
    #: 整月每一天（含前面的空格），前端顺序铺进 7 列网格即可
    days: list[DayOut]
    weekdays: list[str]
    #: 连续签到天数（月历上方那一行小结用）
    streak: int
    prev_month: str
    next_month: str
    can_prev: bool
    can_next: bool
    status_labels: StatusLabels
    #: 休 / 班 的显示文字
    rest_labels: RestLabels = RestLabels()
    #: 道具卡的名字（"补签卡"）。放在月视图里是因为补签的确认框
    #: **在发请求之前**就要弹出来，那一刻前端手里只有这份月视图。
    card_name: str = MAKEUP_CARD_NAME
    #: 还剩几张补签卡（评价15 T1-5）。同样是给"弹框之前"用的：
    #: 一张都没有时不该弹一个注定失败的确认框，直接在那一格上说明。
    cards: int = 0
    #: 没卡时那句话（"未持有补签卡"）
    card_note: str = NO_CARD_MESSAGE


class CheckinResponse(BaseModel):
    session_id: str
    date: str
    #: 这次是不是**新**签上的（重复点同一天是 False，界面据此改口风）
    fresh: bool
    message: str
    #: 失败时为 False，并在 detail 里说明原因。
    #: 为什么失败也回这个模型（而不是一个只有 detail 的错误体）：
    #: 用户要求"红字提醒放在想补签的那一天底部"，前端必须能**重画日历**
    #: 才能把提示挂到那一格下面，所以失败时同样带上整月视图。
    ok: bool = True
    detail: str = ""
    #: 这是不是补签（补签会扣掉一张道具卡）
    makeup: bool = False
    #: 扣完还剩几张补签卡
    cards: int = 0
    #: 道具卡的名字，例如 "补签卡"（前端不硬编码）
    card_name: str = MAKEUP_CARD_NAME
    #: 补签提示语里要用的那句"未持有补签卡"（同样由后端给）
    card_note: str = NO_CARD_MESSAGE
    #: 那一天所在月的完整视图（补签上个月时给的就是上个月）
    month: MonthResponse


# --------------------------------------------------------------- 依赖


def _archive(request: Request) -> CheckinArchive:
    archive = getattr(request.app.state, "checkin_store", None)
    if archive is None:
        # 正常情况下 main.py 的 lifespan 已经建好了；这里防的是"直接构造 app"的测试
        raise HTTPException(status_code=503, detail="签到功能未初始化")
    return archive


def _today() -> date:
    """服务端的今天。

    单独抽成函数是为了**测试能整体替换它**（monkeypatch `_today`），
    而不是让测试去依赖"跑测试那天正好是哪一天"。
    """
    return date.today()


def _view(
    session_id: str,
    view_month: date,
    checked: set[str],
    today: date,
    cards: int = 0,
) -> MonthResponse:
    return MonthResponse(
        session_id=session_id,
        **month_view(view_month, today=today, checked=checked, cards=cards),
    )


def _fail(
    session_id: str,
    day: date,
    reason: str,
    *,
    archive: CheckinArchive,
    today: date,
) -> JSONResponse:
    """签到失败的统一返回：**400 + 一句中文 + 当天的整月视图**。

    带上整月视图是为了让前端能把提示画到"出问题的那一格"下面（用户要求），
    而不是在窗口底部飘一行红字——那样用户还得自己找是哪天出的问题。
    """
    body = CheckinResponse(
        session_id=session_id,
        date=day.isoformat(),
        fresh=False,
        message="",
        ok=False,
        detail=reason,
        cards=archive.cards(session_id),
        month=_view(
            session_id,
            day.replace(day=1),
            archive.checked_days(session_id),
            today,
            archive.cards(session_id),
        ),
    )
    return JSONResponse(status_code=400, content=body.model_dump())


# --------------------------------------------------------------- 接口


@router.get("/month", response_model=MonthResponse)
async def month(
    request: Request,
    session_id: str = Query(min_length=1, max_length=128),
    month: str = Query(default="", max_length=7),
) -> MonthResponse:
    """整月视图：每天是已签 / 未签 / 还没到。

    month 传空或格式不对时回落到当月——翻页动作不值得报错（见 core 里的说明）。

    **未来月份不再夹回当月**（第 146 步）：用户要求能自由翻到 2050 年看日历，
    未来的日子只是不给签到状态、不可点（由 `month_grid` 的 status=future 表达）。
    这里只做范围保护：早于 MIN_DATE / 晚于 MAX_DATE 的请求夹回边界月份，
    免得有人手动构造 `month=1900-01` 让服务端算一堆没数据的格子。
    """
    archive = _archive(request)
    today = _today()
    view_month = parse_month(month, today=today)
    if view_month < MIN_DATE.replace(day=1):
        view_month = MIN_DATE.replace(day=1)
    elif view_month > MAX_DATE.replace(day=1):
        view_month = MAX_DATE.replace(day=1)

    checked = archive.checked_days(session_id)
    return _view(session_id, view_month, checked, today, archive.cards(session_id))


@router.post("", response_model=CheckinResponse)
async def checkin(payload: CheckinRequest, request: Request) -> CheckinResponse:
    """签一天。**过去的日子也允许**（用户明确要的补签），未来不行。

    服务端时间是唯一裁判：即使前端把"今天"算错了（时区、手改系统时间），
    这里也不接受比服务端今天更晚的日期——否则连续天数会被未来日期污染。
    """
    archive = _archive(request)
    today = _today()

    try:
        day = parse_iso_date(payload.date, field_name="签到日期")
    except CheckinError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 第 147 步：**范围校验**（评价10 抓到的真实接口 bug）。
    #
    # 以前只挡住"未来"，于是：
    # - `POST 1900-01-01` 一路走到生成月视图，`solar_term_qingming()` 抛
    #   ValueError（公式只覆盖 2000–2099）→ 生产环境就是 500；
    # - `POST 2019-12-31` 更阴：它签成功、扣掉一张补签卡，而 GET 把 month
    #   夹回 2020-01，这条记录成了"扣了卡但界面永远翻不回去"的孤儿数据。
    #
    # 这里直接 400，**不调 `_fail`**：`_fail` 要渲染整月日历，范围外的日期渲染不出来。
    #
    # 顺序很重要：必须在"未来"判断**之前**。
    # 否则 9999-12-31 会先被未来分支接走、又去渲染一座 9999 年的日历 —— 照样 500。
    if day < MIN_DATE or day > MAX_DATE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"只能签 {MIN_DATE.isoformat()} 到 {MAX_DATE.isoformat()} 之间的日子"
                f"（{day.isoformat()} 不在日历范围内）。"
            ),
        )

    if day > today:
        return _fail(
            payload.session_id,
            day,
            f"这一天还没到（{day.isoformat()}），到了再签。",
            archive=archive,
            today=today,
        )

    # 补签 = 签的是过去的日子（服务端自己判，不信前端那个布尔值）。
    #
    # 第 156 步（评价11 P3-3）：查重、扣卡、写签到必须**同一个事务**。
    # 以前是"先查 already → 扣卡 → sign()"三段，两个并发补签请求都能通过查重，
    # 结果各扣一张卡、只有一次签到生效（白扣一张）。现在交给 sign_with_card()：
    # 一条带条件的 UPDATE 扣卡 + INSERT OR IGNORE 写签到，要么都发生要么都不发生。
    makeup = day < today

    if makeup:
        try:
            fresh, _cards = archive.sign_with_card(payload.session_id, day)
            # 只有"这次真的是新签的"才算补签 —— 重复点同一天时
            # makeup 必须回 False（否则界面会说"补签成功"而卡根本没扣）
            makeup = makeup and fresh
        except NoMakeupCard as exc:
            # 没有卡就到此为止：400 + "未持有补签卡"（用户第 4 条指定的文案）
            return _fail(
                payload.session_id, day, str(exc), archive=archive, today=today
            )
    else:
        fresh = archive.sign(payload.session_id, day)
    message = (
        f"{day.isoformat()} 签到成功。"
        if fresh
        else f"{day.isoformat()} 已经签过了。"
    )
    logger.info(
        "会话 %s 签到 %s（新签=%s，补签=%s，余卡=%s）",
        payload.session_id, day.isoformat(), fresh, makeup,
        archive.cards(payload.session_id),
    )

    checked = archive.checked_days(payload.session_id)
    return CheckinResponse(
        session_id=payload.session_id,
        date=day.isoformat(),
        fresh=fresh,
        message=message,
        makeup=makeup,
        cards=archive.cards(payload.session_id),
        month=_view(
            payload.session_id,
            day.replace(day=1),
            checked,
            today,
            archive.cards(payload.session_id),
        ),
    )
