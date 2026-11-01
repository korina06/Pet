"""签到接口层：服务端说了算的"今天"、三种状态透出、补签与重复签、跨月回整月视图、会话隔离。

用 `httpx.ASGITransport` 直接打进应用，不启服务器；"今天"通过替换模块里的函数钉死。
"""

from __future__ import annotations

from datetime import date

import httpx
from fastapi import FastAPI

from api import checkin_routes
from api.checkin_routes import router
from core.checkin import CheckinArchive

TODAY = date(2026, 3, 18)          # 2026-03-18 是周三


def build_app(*, today: date = TODAY, cards: int = 0) -> FastAPI:
    """建一个只挂签到路由的应用，并把"今天"与"补签卡余量"钉死。

    cards 默认 0 = `core/checkin.py` 的 MAKEUP_CARDS_START，
    也就是"道具系统还没做"时的真实状态：补签会被拒。
    """
    app = FastAPI()
    app.include_router(router)
    app.state.checkin_store = CheckinArchive()
    #: 测试自己记一份"今天"：抢在应用状态里，比读模块全局变量可靠
    #: （模块变量是全局的，上一个测试留下的值会串到下一个测试里）
    app.state.test_today = today
    app.state.test_cards = cards
    checkin_routes._today = lambda: today
    app.state.checkin_store.set_cards("s1", cards)
    return app


def grant_cards(app: FastAPI, session_id: str, count: int) -> None:
    """给某个会话直接发几张补签卡（道具系统还没做，测试里手动发）。"""
    app.state.checkin_store.set_cards(session_id, count)


async def get_month(
    app: FastAPI,
    *,
    session_id: str = "s1",
    month: str | None = None,
    today: date | None = None,
) -> dict:
    """取某个月的视图。

    两个细节都是为了测试不被"真实的今天"影响：

    - `today` 默认取 `build_app` 时钉死的那个日期（每次请求都重新钉一遍，
      并还原模块里原来的值——模块变量是全局的，会串场）；
    - `month` 不传时**显式传当月**（后端对空 month 的回落用的是服务端真实日期，
      那是产品行为，不该让测试跟着跑测试那天漂）。
    """
    pin = today or app.state.test_today
    target = month if month is not None else pin.strftime("%Y-%m")
    previous = checkin_routes._today
    checkin_routes._today = lambda: pin
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/checkin/month", params={"session_id": session_id, "month": target}
            )
        assert response.status_code == 200, response.text
        return response.json()
    finally:
        checkin_routes._today = previous


async def post_checkin(
    app: FastAPI,
    day: str,
    *,
    session_id: str = "s1",
    today: date | None = None,
) -> httpx.Response:
    pin = today or app.state.test_today
    previous = checkin_routes._today
    checkin_routes._today = lambda: pin
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/api/checkin", json={"session_id": session_id, "date": day}
            )
    finally:
        checkin_routes._today = previous


def find_day(payload: dict, iso: str) -> dict:
    for row in payload["days"]:
        if row["date"] == iso:
            return row
    raise AssertionError(f"{iso} 不在返回的网格里")


# --------------------------------------------------------------- 整月视图


async def test_month_view_lists_every_day_with_a_status():
    app = build_app()
    payload = await get_month(app)

    assert payload["month"] == "2026-03"
    assert payload["month_label"] == "2026 年 3 月"
    assert payload["weekdays"] == ["一", "二", "三", "四", "五", "六", "日"]
    assert payload["leading"] == 6
    assert payload["status_labels"] == {"done": "已签", "todo": "未签"}
    assert payload["card_name"] == "补签卡", "补签确认框要用到卡名"
    # 用户第 2、3 条去掉的两处：不返回"今天"，也不算"本月已签/未签"
    for removed in ("today", "done_count", "todo_count", "total_count"):
        assert removed not in payload, f"{removed} 已按用户要求去掉"

    # 空会话：今天与过去都是未签，未来是"还没到"
    assert find_day(payload, "2026-03-01")["status"] == "todo"
    assert find_day(payload, "2026-03-18")["status"] == "todo"
    assert find_day(payload, "2026-03-19")["status"] == "future"

    # 网格里除了日期格还有月初的空格（没有日期）
    blanks = [row for row in payload["days"] if not row["date"]]
    assert len(blanks) == 6


async def test_past_days_are_flagged_for_makeup():
    """过去的日子带 past 标记（前端据此弹"要消耗一张卡"的确认）。"""
    app = build_app()
    payload = await get_month(app)
    assert find_day(payload, "2026-03-17")["past"] is True
    assert find_day(payload, "2026-03-18")["past"] is False     # 今天不是补签
    assert find_day(payload, "2026-03-25")["past"] is False     # 还没到


async def test_future_days_have_no_note_and_are_not_clickable():
    """用户原话："还没到的日期不算"——不写字，也不可点。"""
    app = build_app()
    payload = await get_month(app)
    future = find_day(payload, "2026-03-25")

    assert future["status"] == "future"
    assert future["note"] == ""
    assert future["clickable"] is False
    # 过去与今天都可点（补签）
    assert find_day(payload, "2026-03-17")["clickable"] is True
    assert find_day(payload, "2026-03-18")["clickable"] is True


async def test_month_view_returns_the_requested_month():
    app = build_app()
    payload = await get_month(app, month="2026-02")
    assert payload["month"] == "2026-02"
    assert payload["month_label"] == "2026 年 2 月"
    assert len([row for row in payload["days"] if row["date"]]) == 28


async def test_month_falls_back_to_this_month_and_clamps_to_the_range():
    """翻月动作不报错：空值/格式不对回落到当月；超出范围夹回边界月份。

    第 146 步起**未来月份允许查看**（用户要能翻到 2050），
    所以 2030-01 不再被夹回当月，而是正常返回那一月（日子为 future）。
    """
    app = build_app()
    # 刻意**不传**具体月份来测"回落"：空值落到哪一月由服务端的今天决定
    assert (await get_month(app, month=""))["month"] == "2026-03"
    assert (await get_month(app, month="不是月份"))["month"] == "2026-03"

    # 未来月份：正常返回，不再夹回
    assert (await get_month(app, month="2030-01"))["month"] == "2030-01"

    # 超出范围：夹回 2020-01 / 2050-12（保护服务端，不报错）
    assert (await get_month(app, month="1900-01"))["month"] == "2020-01"
    assert (await get_month(app, month="2099-01"))["month"] == "2050-12"


async def test_month_requires_a_session_id():
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/checkin/month")
    assert response.status_code == 422


# --------------------------------------------------------------- 签到


async def test_sign_today_turns_the_day_into_done():
    app = build_app()
    response = await post_checkin(app, "2026-03-18")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["date"] == "2026-03-18"
    assert payload["fresh"] is True
    assert "签到成功" in payload["message"]
    assert payload["makeup"] is False, "签今天不是补签，不消耗道具卡"
    assert find_day(payload["month"], "2026-03-18")["status"] == "done"
    assert find_day(payload["month"], "2026-03-18")["note"] == "已签"
    assert payload["month"]["streak"] == 1

    # 再取一次也记得（真值在后端，不是前端画上去的）
    again = await get_month(app)
    assert find_day(again, "2026-03-18")["status"] == "done"


async def test_signing_the_same_day_twice_is_not_an_error():
    app = build_app()
    first = (await post_checkin(app, "2026-03-18")).json()
    second = (await post_checkin(app, "2026-03-18")).json()

    assert first["fresh"] is True
    assert second["fresh"] is False
    assert "已经签过" in second["message"]
    assert second["makeup"] is False, "重复点不该算补签、也不该扣卡"


async def test_signing_a_past_day_needs_a_makeup_card():
    """用户第 4 条：补签要消耗道具卡。没有卡就明确提示，且不留下记录。

    失败时**连整月视图一起回**（不是只回一个 detail）：前端要把这句提示
    画到出问题的那一格下面（用户第 2 轮的要求），没有月视图就画不出来。
    """
    app = build_app(cards=0)
    response = await post_checkin(app, "2026-03-05")

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["detail"] == "未持有补签卡"
    assert payload["ok"] is False
    assert payload["date"] == "2026-03-05"
    assert find_day(payload["month"], "2026-03-05")["status"] == "todo"
    assert payload["month"]["card_name"] == "补签卡"

    # 被拒之后不该留下任何记录
    month = await get_month(app)
    assert find_day(month, "2026-03-05")["status"] == "todo"


async def test_makeup_card_is_spent_and_counted():
    app = build_app(cards=1)
    response = await post_checkin(app, "2026-03-05")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["makeup"] is True
    assert payload["cards"] == 0, "补签要扣一张"
    assert payload["card_name"] == "补签卡"
    assert payload["card_note"] == "未持有补签卡"
    assert find_day(payload["month"], "2026-03-05")["status"] == "done"

    # 卡用完了 → 再补另一天就被拒
    second = await post_checkin(app, "2026-03-04")
    assert second.status_code == 400
    assert second.json()["detail"] == "未持有补签卡"
    month = await get_month(app)
    assert find_day(month, "2026-03-04")["status"] == "todo"


async def test_signing_an_already_done_past_day_does_not_spend_a_card():
    """已经签过的过去日子再点一次是"重复"，不该白扣一张卡。"""
    app = build_app(cards=2)
    first = (await post_checkin(app, "2026-03-05")).json()
    assert first["cards"] == 1

    second = (await post_checkin(app, "2026-03-05")).json()
    assert second["fresh"] is False
    assert second["makeup"] is False
    assert second["cards"] == 1, "重复点不该再扣卡"


async def test_cards_are_per_session():
    app = build_app(cards=0)
    grant_cards(app, "alice", 1)

    alice = await post_checkin(app, "2026-03-05", session_id="alice")
    bob = await post_checkin(app, "2026-03-05", session_id="bob")
    assert alice.status_code == 200
    assert bob.status_code == 400, "bob 没有卡，不该因为 alice 有卡就能补签"


async def test_signing_a_future_day_is_rejected():
    """服务端时间是唯一裁判：改客户端时钟也签不了明天。

    这条也带月视图（同一套 _fail），这样前端能把提示挂到那一格下面。
    """
    app = build_app(cards=3)
    response = await post_checkin(app, "2026-03-19")
    assert response.status_code == 400
    payload = response.json()
    assert "还没到" in payload["detail"]
    assert payload["ok"] is False
    assert find_day(payload["month"], "2026-03-19")["status"] == "future"

    # 被拒之后不该留下任何记录，也不该扣卡
    month = await get_month(app)
    assert find_day(month, "2026-03-19")["status"] == "future"
    assert (await post_checkin(app, "2026-03-19")).status_code == 400


async def test_signing_a_malformed_day_is_rejected():
    app = build_app()
    response = await post_checkin(app, "2026/03/18")
    assert response.status_code == 400
    assert "格式" in response.json()["detail"]


async def test_compact_iso_date_is_rejected():
    """Python 3.11 起 fromisoformat 认紧凑写法，会把同一天变成两个键。

    真发生的话用户点两次就能把一天签成两条记录，所以这里必须有防线。
    """
    app = build_app()
    response = await post_checkin(app, "20260318")
    assert response.status_code == 400, response.text
    assert "格式" in response.json()["detail"]


async def test_post_returns_the_month_of_the_signed_day():
    """补签上个月时，回来的必须是**上个月**的视图（前端据此重画，不用自己判断）。"""
    app = build_app(cards=1)
    response = await post_checkin(app, "2026-02-14")
    payload = response.json()

    assert payload["month"]["month"] == "2026-02"
    assert find_day(payload["month"], "2026-02-14")["status"] == "done"


async def test_each_month_view_only_reflects_its_own_days():
    """2 月签的那天不会串到 3 月的网格里（各自按自己的月份画）。"""
    app = build_app(cards=2)
    await post_checkin(app, "2026-02-28")
    await post_checkin(app, "2026-03-02")

    march = await get_month(app)
    assert find_day(march, "2026-03-02")["status"] == "done"
    assert find_day(march, "2026-03-01")["status"] == "todo"

    february = await get_month(app, month="2026-02")
    assert find_day(february, "2026-02-28")["status"] == "done"


# --------------------------------------------------------------- 连续天数


async def test_streak_is_reported_from_the_backend():
    # 补签要卡：16、17 是过去的日子，各花一张
    app = build_app(cards=2)
    for day in ("2026-03-16", "2026-03-17", "2026-03-18"):
        assert (await post_checkin(app, day)).status_code == 200

    payload = await get_month(app)
    assert payload["streak"] == 3


async def test_streak_survives_a_missing_today():
    """今天还没签，之前攒的连续不该清零（晚上才想起来签很正常）。"""
    app = build_app(cards=3)
    for day in ("2026-03-15", "2026-03-16", "2026-03-17"):
        assert (await post_checkin(app, day)).status_code == 200

    payload = await get_month(app)
    assert payload["streak"] == 3


# --------------------------------------------------------------- 翻月范围


async def test_can_page_freely_from_2020_to_2050():
    """用户第 5 条：支持自由翻到 2050 年（上限不再卡在"本月"）。

    下限仍是 2020-01（数据起点），上限 2050-12（农历表到头）。
    """
    app = build_app()
    payload = await get_month(app)
    assert payload["can_prev"] is True
    assert payload["can_next"] is True, "当月也该能往后翻（未来可以看，只是不能签）"

    future = await get_month(app, month="2031-07")
    assert future["month"] == "2031-07"
    assert future["can_prev"] is True
    assert future["can_next"] is True

    last = await get_month(app, month="2050-12")
    assert last["month"] == "2050-12"
    assert last["can_next"] is False, "2050-12 之后翻不动"

    floor = await get_month(app, month="2020-01")
    assert floor["can_prev"] is False, "2020-01 之前翻不动"


async def test_records_do_not_affect_the_paging_range():
    """记录与实际可翻范围**无关**了：范围只由 MIN_DATE / MAX_DATE 决定。

    （原先"有更早记录时下限跟着记录走"的逻辑已随"自由翻到 2050"一起删掉；
    这条防止它被顺手写回来。）
    """
    app = build_app(cards=1)
    await post_checkin(app, "2024-05-20")

    at_record = await get_month(app, month="2024-05")
    assert at_record["can_prev"] is True, "记录所在月不是下限"
    assert at_record["can_next"] is True

    floor = await get_month(app, month="2020-01")
    assert floor["can_prev"] is False


async def test_future_months_have_no_signin_state():
    """未来的月份能看：每天 status 都是 future、不可点、没有已签/未签字样。"""
    app = build_app()
    payload = await get_month(app, month="2031-07")

    assert all(row["status"] == "future" for row in payload["days"] if row["date"])
    assert all(row["clickable"] is False for row in payload["days"] if row["date"])
    assert all(row["note"] == "" for row in payload["days"] if row["date"])
    # 但农历/休班照常给（日历本身是真实的）
    assert any(row["lunar"] for row in payload["days"])


async def test_backward_range_never_goes_before_the_absolute_floor():
    """绝对下限（core.checkin.MIN_DATE = 2020-01）：再早也没有意义。

    2020-01 与今天（2026-03）相距 6 年，远超"一年前"那条线，
    所以能不能翻到这里靠的正是 MIN_DATE 这道绝对下限。
    """
    app = build_app()
    oldest = await get_month(app, month="2020-01")
    assert oldest["month"] == "2020-01", "六年前的月份还是应该能翻到的"
    assert oldest["can_prev"] is False, "不该越过绝对下限继续往前翻"


# --------------------------------------------------------------- 会话隔离


async def test_sessions_do_not_share_records():
    app = build_app()
    await post_checkin(app, "2026-03-18", session_id="alice")

    alice = await get_month(app, session_id="alice")
    bob = await get_month(app, session_id="bob")
    assert find_day(alice, "2026-03-18")["status"] == "done"
    assert find_day(bob, "2026-03-18")["status"] == "todo"


async def test_without_storage_the_records_are_memory_only():
    """没有 storage 时仍然是内存模式（换一个 store = 重启 = 清空）。

    这是 `open_storage()` 打不开库文件时的降级语义：界面照常，数据重启即清空。
    **生产路径不这样**——main.py 会把 storage 传进来，见下一条。
    """
    app = build_app()
    await post_checkin(app, "2026-03-18")
    assert find_day(await get_month(app), "2026-03-18")["status"] == "done"

    app.state.checkin_store = CheckinArchive()      # 重启（内存模式）
    assert find_day(await get_month(app), "2026-03-18")["status"] == "todo"


async def test_signed_days_survive_a_real_restart(tmp_path):
    """用户实测的 bug：重启服务后签过的日期全变回未签。

    这条用**真的库文件**跑：写一次请求 → 关掉连接 → 用同一个文件重新开库
    重挂一个 archive（等于重启进程）→ 已签的日期还在。
    """
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    db_path = tmp_path / "pet.db"

    first = Storage(db_path)
    ensure_schema(first)
    app = build_app()
    app.state.checkin_store = CheckinArchive(storage=first)
    signed = await post_checkin(app, "2026-03-18")
    assert signed.status_code == 200, signed.text
    first.close()                                   # 进程结束

    second = Storage(db_path)
    ensure_schema(second)                           # 相当于下次启动
    app.state.checkin_store = CheckinArchive(storage=second)
    try:
        payload = await get_month(app)
        assert find_day(payload, "2026-03-18")["status"] == "done", "重启后签过的日期不该消失"
        assert payload["streak"] == 1
    finally:
        second.close()


async def test_store_missing_is_a_clear_503():
    """没挂 store 的应用（有人直接构造函数建 app）要给一句能读懂的话。"""
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/checkin/month", params={"session_id": "s1"})
    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]


# --------------------------------------------------- 与前端桩的一致性


async def test_leading_blank_matches_the_weekday_of_the_first_day():
    """月首偏移必须与 date.weekday() 一致（前端完全按这个顺序铺格子）。

    这是"日历错一格"最容易发生的地方，所以各种月首都要对一遍。

    ⚠️ 只取**被钉死的今天（2026-03-18）当天与之前**的月份：未来月份会被接口
    夹回当月，测出来的是"夹取"而不是"偏移"（第一版写了 2026-04，就撞在这里）。
    """
    app = build_app()
    for month_text, expected_leading in (
        ("2026-03", 6),      # 3/1 周日
        ("2026-02", 6),      # 2/1 周日
        ("2026-01", 3),      # 1/1 周四
        ("2025-12", 0),      # 12/1 周一（"一格都不用补"的对照）
        ("2024-02", 3),      # 闰年 2/1 周四
    ):
        payload = await get_month(app, month=month_text)
        assert payload["month"] == month_text, month_text
        assert payload["leading"] == expected_leading, month_text
        first = payload["days"][expected_leading]
        assert first["date"] == f"{month_text}-01"


async def test_grid_length_is_leading_plus_days_in_month():
    app = build_app()
    march = await get_month(app)
    assert len(march["days"]) == march["leading"] + 31

    january = await get_month(app, month="2026-01")
    assert len(january["days"]) == january["leading"] + 31

    february = await get_month(app, month="2026-02")
    assert len(february["days"]) == february["leading"] + 28

    # 闰年二月也按真实天数来（不许写死 28）
    leap = await get_month(app, month="2024-02")
    assert len(leap["days"]) == leap["leading"] + 29


async def test_grid_pads_only_before_the_first_day():
    """空格只出现在月首，而且顺序就是"空格在前、日期在后"。"""
    app = build_app()
    payload = await get_month(app)
    statuses = [row["status"] for row in payload["days"]]
    blanks = statuses[: payload["leading"]]
    dates = statuses[payload["leading"]:]

    assert set(blanks) == {None}, "月首的空格不该有状态"
    assert None not in dates, "日期格里不该出现空格（没有状态的格子）"


async def test_a_date_out_of_the_calendar_range_is_rejected_without_crashing():
    """范围外的日期一律 400，不许 500（第 147 步 / 评价10 抓到的真实 bug）。

    1900-01-01 以前会一路走到生成月视图，`solar_term_qingming()` 抛
    ValueError（公式只支持 2000–2099）→ 生产环境就是 500。
    """
    app = build_app()

    for raw in ("1900-01-01", "2019-12-31", "2100-01-01", "9999-12-31"):
        response = await post_checkin(app, raw, session_id="range")
        assert response.status_code == 400, (
            f"{raw} 应当 400，实际 {response.status_code}"
        )
        assert "日历范围" in response.json()["detail"]


async def test_an_out_of_range_date_does_not_touch_the_archive():
    """2019-12-31 不许签进去，也不许扣卡 ——

    否则就是"扣了卡但界面永远翻不回去"的孤儿记录（评价10 原话）。
    """
    app = build_app()
    archive = app.state.checkin_store
    cards_before = archive.cards("ghost")

    response = await post_checkin(app, "2019-12-31", session_id="ghost")

    assert response.status_code == 400
    assert "2019-12-31" not in archive.checked_days("ghost")
    assert archive.cards("ghost") == cards_before, "范围外的请求不该扣补签卡"


async def test_festivals_do_not_blow_up_outside_the_qingming_formula():
    """清明的近似公式只覆盖 2000–2099；范围外**跳过**清明，不抛异常。

    评价10 的 500 根因就是这里：一个脏日期把整个月视图打死。
    """
    from core.lunar import festivals_of

    for raw in (date(1900, 4, 5), date(2100, 4, 5), date(9999, 4, 5)):
        names = festivals_of(raw)          # 不抛就算过
        assert isinstance(names, list)


def test_month_view_carries_remaining_cards():
    """月视图带上剩余卡数（第 191 步 / 评价15 T1-5）。

    为什么把卡数塞进月视图：补签的确认框是在**发请求之前**弹的，
    那一刻前端手里只有这份月视图。没有卡数，前端就没法判断
    "这一下注定失败"，只能先弹框、点确定、被拒，再解释。

    所以这条测试守的是"前端能不能提前判断"，不是"卡数显示得对不对"。
    """
    import asyncio

    app = build_app(cards=2)
    view = asyncio.run(get_month(app))

    assert view["cards"] == 2, f"月视图里的卡数是 {view.get('cards')}"
    assert view["card_name"], "卡名也要在，不然弹框里会写出一对空引号"
    assert view["card_note"], "没卡时那句话也由后端给，前端不硬编码中文"


def test_makeup_decrements_the_card_count_in_the_next_view():
    """补签成功后，下一份月视图里的卡数要少一张。"""
    import asyncio

    from datetime import date as _date

    app = build_app(cards=2, today=_date(2026, 3, 18))
    before = asyncio.run(get_month(app))["cards"]

    async def _sign() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/checkin", json={"session_id": "s1", "date": "2026-03-05", "makeup": True}
            )
            assert response.status_code == 200, response.text
            return response.json()

    body = asyncio.run(_sign())
    assert body["makeup"] is True and body["fresh"] is True
    assert body["cards"] == before - 1, f"扣卡后应剩 {before - 1}，实际 {body['cards']}"
    assert body["month"]["cards"] == before - 1, "整月视图里的卡数也要跟着变"


def test_supported_years_are_derived_from_the_data():
    """SUPPORTED_YEARS 必须与 ARRANGEMENTS 一致（第 147 步）。

    评价10 抓到过手写版是 `(2024, 2026)` —— 漏了 2025，而 2025 的数据明明在。
    现在由数据派生，这类漂移不会再发生。
    """
    from core.holidays import ARRANGEMENTS, SUPPORTED_YEARS

    assert SUPPORTED_YEARS == tuple(sorted(ARRANGEMENTS))
    assert 2025 in SUPPORTED_YEARS
