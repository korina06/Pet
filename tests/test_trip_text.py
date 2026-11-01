"""行程导出成文本：该有的都有，**不该有的都没有** —— 没有数据源的字段一个字也不能出现。
"""

from __future__ import annotations

import asyncio

from core.trip_text import render_itinerary

OK_TRIP = {
    "city": "杭州",
    "start_date": "2026-11-01",
    "end_date": "2026-11-02",
    "transport": "公共交通",
    "stay": "不住宿",
    "preferences": ["历史文化"],
    "status": "ok",
    "issues": ["有 9 个候选景点没排进行程"],
    "days": [
        {
            "index": 1,
            "date": "2026-11-01",
            "issues": [],
            "spots": [
                {"name": "吴山景区-有美堂", "opentime": "09:00-17:00", "duration_min": 120}
            ],
            "meals": [
                {"name": "钱席", "slot": "早餐"},
                {"name": "九月生活", "slot": "午餐"},
            ],
            "hotels": [{"name": "某酒店"}],
        },
        {
            "index": 2,
            "date": "2026-11-02",
            "issues": ["「鼓楼」当天不开放（营业时间 周一闭馆）"],
            "spots": [{"name": "鼓楼", "opentime": "周一闭馆", "duration_min": 60}],
            "meals": [],
        },
    ],
}

FAILED_TRIP = {
    "city": "北京",
    "start_date": "2026-11-01",
    "end_date": "2026-11-02",
    "transport": "公共交通",
    "stay": "不住宿",
    "status": "failed",
    "degraded_reasons": ["地理编码服务不可用"],
    "days": [],
    "issues": [],
}


def test_a_normal_trip_renders_everything_it_has():
    text = render_itinerary(OK_TRIP)

    assert "杭州" in text
    assert "2026-11-01" in text and "2026-11-02" in text
    assert "第 1 天" in text and "第 2 天" in text
    assert "吴山景区-有美堂" in text
    assert "09:00-17:00" in text, "营业时间要给出来"
    assert "约 2 小时" in text, "时长要标成估算"
    assert "餐饮（建议时段）" in text, "餐次是我们建议的，必须写明"
    assert "早餐 钱席" in text
    assert "住宿候选" in text
    assert "当天不开放" in text, "这一天的问题要带出来"
    assert "没排进行程" in text, "整体提醒也要带出来"


def test_the_text_never_mentions_fields_we_do_not_have():
    text = render_itinerary(OK_TRIP)

    for forbidden in ("人均", "门票", "星级", "房价", "元/人"):
        assert forbidden not in text, f"文本里不该出现没有数据源的字段：{forbidden}"


def test_a_failed_trip_renders_only_the_reason():
    text = render_itinerary(FAILED_TRIP)

    assert "这次没能生成行程" in text
    assert "地理编码服务不可用" in text
    assert "第 1 天" not in text, "失败了就不该出现行程"


def test_empty_input_is_not_a_crash():
    assert render_itinerary({}) == ""
    assert render_itinerary(None) == ""  # type: ignore[arg-type]


def test_an_empty_day_says_so():
    trip = {
        "city": "北京",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "status": "ok",
        "days": [{"index": 1, "date": "2026-11-01", "spots": [], "meals": []}],
    }
    text = render_itinerary(trip)

    assert "这一天没有排到景点" in text


# ------------------------------------------------ 逐日预报与通行段（第 239 步）

FORECAST_TRIP = {
    "city": "杭州",
    "start_date": "2026-11-01",
    "end_date": "2026-11-02",
    "status": "ok",
    "days": [
        {
            "index": 1,
            "date": "2026-11-01",
            "spots": [{"name": "吴山景区-有美堂"}],
            "meals": [],
            "forecast": {
                "date": "2026-11-01",
                "text_day": "阴",
                "text_night": "小雨",
                "temp_max": 21.34,
                "temp_min": 11.8,
                "temp_unit": "°C",
                "precip_prob": 0.64,
            },
        },
        {
            "index": 2,
            "date": "2026-11-02",
            "spots": [{"name": "鼓楼"}],
            "meals": [],
        },
    ],
}


def test_the_text_carries_each_days_forecast_labelled_as_forecast():
    text = render_itinerary(FORECAST_TRIP)

    assert "天气（预报）：阴，夜间小雨，11.8~21.3°C，降水概率 64%" in text
    assert text.count("天气（预报）") == 1, "没有预报的那一天不该凭空多一行"


def test_the_markdown_also_carries_the_forecast():
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(FORECAST_TRIP)

    assert "- 天气（预报）：阴，夜间小雨，11.8~21.3°C，降水概率 64%" in text


LEG_TRIP = {
    "city": "杭州",
    "start_date": "2026-11-01",
    "end_date": "2026-11-01",
    "status": "ok",
    "days": [
        {
            "index": 1,
            "date": "2026-11-01",
            "spots": [{"name": "吴山景区-有美堂"}, {"name": "鼓楼"}],
            "legs": [
                {
                    "from_name": "吴山景区-有美堂",
                    "to_name": "鼓楼",
                    "distance_m": 12050,
                    "duration_min": 135,
                    "mode": "driving",
                    "tolls": 30.0,
                    "source": "amap_driving",
                }
            ],
            "meals": [],
        }
    ],
}


def test_the_text_carries_the_leg_and_the_toll():
    text = render_itinerary(LEG_TRIP)

    assert (
        "通行 吴山景区-有美堂 → 鼓楼：12.1 公里，驾车约 135 分钟，过路费约 30 元（高德驾车）"
        in text
    )


def test_the_markdown_carries_the_leg_and_the_toll():
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(LEG_TRIP)

    assert "12.1 公里" in text and "过路费约 30 元" in text
    assert "（高德驾车）" in text


def test_zero_or_missing_tolls_are_not_written_in_the_export():
    """0 元与没查到都不该出现在导出的文字里（与界面同一条规则）。"""
    for tolls in (0.0, None):
        leg = dict(LEG_TRIP["days"][0]["legs"][0])
        leg["tolls"] = tolls
        trip = dict(LEG_TRIP)
        trip["days"] = [dict(LEG_TRIP["days"][0], legs=[leg])]

        text = render_itinerary(trip)
        assert "过路费" not in text, f"tolls={tolls!r} 时不该出现过路费"


# --------------------------------------------------------------- 接口


def test_the_endpoint_returns_the_rendered_text():
    import httpx
    from fastapi import FastAPI

    from api.trip_routes import router

    app = FastAPI()
    app.include_router(router)

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post("/api/trip/text", json={"itinerary": OK_TRIP})
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    assert payload["text"].startswith("行程（杭州")
    assert "吴山景区-有美堂" in payload["text"]


def test_the_endpoint_tolerates_an_empty_body():
    import httpx
    from fastapi import FastAPI

    from api.trip_routes import router

    app = FastAPI()
    app.include_router(router)

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post("/api/trip/text", json={})
            assert response.status_code == 200, response.text
            return response.json()

    assert asyncio.run(run())["text"] == ""

# ------------------------------------------------- 第 218 步：导出成 .md

MD_TRIP = {
    "city": "苏州/杭州",
    "start_date": "2026-11-01",
    "end_date": "2026-11-02",
    "transport": "火车",
    "stay": "民宿",
    "preferences": ["亲子"],
    "note": "第三天下午想留空",
    "status": "ok",
    "days": [
        {
            "index": 1,
            "date": "2026-11-01",
            "spots": [
                {"name": "拙政园", "opentime": "07:30-17:30", "duration_min": 120,
                 "location": [31.3247, 120.6296]},
                {"name": "没有坐标的塔"},
            ],
            "meals": [{"name": "同得兴", "slot": "午餐", "location": [31.31, 120.62]}],
            "hotels": [{"name": "某民宿"}],
            "issues": ["当天预报有雨"],
        }
    ],
}


def test_markdown_has_the_title_and_the_days():
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(MD_TRIP)

    assert text.startswith("# 苏州/杭州 行程"), text[:40]
    assert "## 第 1 天（2026-11-01）" in text
    assert "- 火车｜民宿" in text
    assert "- 偏好：亲子" in text


def test_markdown_links_only_places_that_really_have_coordinates():
    """没有坐标就不给链接 —— 编一个坐标出来比不显示更糟。"""
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(MD_TRIP)

    # 有坐标：变成链接，且经度在前（Amap 的顺序）
    assert "[拙政园](https://uri.amap.com/marker?position=120.6296,31.3247" in text
    assert "coordinate=gaode" in text, "不写 gaode 会被当 WGS-84，落点偏几百米"
    # 没坐标：只能是纯文字
    assert "- 没有坐标的塔" in text
    assert "没有坐标的塔](" not in text


def test_markdown_never_invents_a_price():
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(MD_TRIP)

    assert "人均" not in text and "门票" not in text and "星级" not in text


def test_markdown_of_an_empty_trip_is_empty():
    from core.trip_text import render_itinerary_markdown

    assert render_itinerary_markdown({}) == ""


def test_markdown_of_a_failed_trip_only_says_why():
    from core.trip_text import render_itinerary_markdown

    text = render_itinerary_markdown(
        {"city": "苏州", "status": "failed", "days": [{"index": 1, "spots": []}],
         "degraded_reasons": ["高德没给坐标"]}
    )

    assert "这次没能生成行程：高德没给坐标" in text
    assert "## 第 1 天" not in text, "失败了就不该有行程段落"


def test_markdown_filename_is_safe_on_windows():
    from core.trip_text import markdown_filename

    assert markdown_filename(MD_TRIP) == "苏州-杭州-2026-11-01.md"
    assert markdown_filename({"city": 'a:b*c?"d"', "start_date": "2026-01-01"}) == "a-b-c-d-2026-01-01.md"
    assert markdown_filename({}) == "行程.md"


def test_markdown_endpoint_returns_filename_and_content():
    import httpx
    from fastapi import FastAPI

    from api.trip_routes import router

    app = FastAPI()
    app.include_router(router)

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post("/api/trip/markdown", json={"itinerary": MD_TRIP})
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    assert payload["filename"] == "苏州-杭州-2026-11-01.md"
    assert payload["markdown"].startswith("# 苏州/杭州 行程")
