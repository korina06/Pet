"""把行程渲染成可粘贴的文本，导出用。

只写行程里真有的字段：没有数据源的东西（门票、人均、星级）一个字都不出现。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

#: 一天最多写几个降级说明（避免导出的文本被告警淹没）
MAX_NOTES = 6

#: 高德的坐标是 GCJ-02，所以地图链接要写 `coordinate=gaode`：
#: 不写的话，Amap 会按 WGS-84 解释，落点偏几百米。
AMAP_MARKER = "https://uri.amap.com/marker?position={lon},{lat}&name={name}&coordinate=gaode&src=pet"


def _amap_link(place: dict[str, Any]) -> str:
    """给一个地点拼高德地图链接；没有坐标就返回空串（**不编坐标**）。"""
    location = place.get("location")
    if not isinstance(location, (list, tuple)) or len(location) != 2:
        return ""
    try:
        lat, lon = float(location[0]), float(location[1])
    except (TypeError, ValueError):
        return ""
    name = str(place.get("name") or "").strip()
    if not name:
        return ""
    return AMAP_MARKER.format(lon=lon, lat=lat, name=quote(name))


def _md_text(text: str) -> str:
    """Markdown 里的链接文字不能带 `[`/`]`，否则会把方括号吃进格式里。"""
    return text.replace("[", "［").replace("]", "］")


def _md_spot_line(spot: dict[str, Any]) -> str:
    name = _md_text(str(spot.get("name") or "（未命名）"))
    link = _amap_link(spot)
    head = f"[{name}]({link})" if link else name
    bits: list[str] = []
    opentime = str(spot.get("opentime") or "").strip()
    if opentime:
        bits.append(opentime)
    minutes = spot.get("duration_min")
    if isinstance(minutes, int) and minutes > 0:
        bits.append(f"约 {minutes // 60} 小时" if minutes >= 60 else f"约 {minutes} 分钟")
    return f"- {head}" + (f"（{'，'.join(bits)}）" if bits else "")


def _line(prefix: str, text: str) -> str:
    return f"{prefix}{text}" if text else prefix.rstrip()


def _spot_line(spot: dict[str, Any]) -> str:
    bits: list[str] = []
    opentime = str(spot.get("opentime") or "").strip()
    if opentime:
        bits.append(opentime)
    minutes = spot.get("duration_min")
    if isinstance(minutes, int) and minutes > 0:
        bits.append(f"约 {minutes // 60} 小时" if minutes >= 60 else f"约 {minutes} 分钟")
    suffix = f"（{'，'.join(bits)}）" if bits else ""
    return f"· {spot.get('name') or '（未命名）'}{suffix}"


def _meal_line(meals: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for meal in meals:
        slot = str(meal.get("slot") or "").strip()
        name = str(meal.get("name") or "").strip()
        if not name:
            continue
        parts.append(f"{slot} {name}" if slot else name)
    return " / ".join(parts)


def _num(value: Any) -> str:
    """温度这类数值：整数就不带小数点，否则保留一位。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(number - round(number)) < 0.05:
        return str(int(round(number)))
    return f"{number:.1f}"


def _forecast_line(forecast: Any) -> str:
    """一天的逐日预报。**开头写「天气（预报）」**：导出的文字同样不能冒充实况。"""
    if not isinstance(forecast, dict):
        return ""
    bits: list[str] = []
    text_day = str(forecast.get("text_day") or "").strip()
    text_night = str(forecast.get("text_night") or "").strip()
    if text_day:
        bits.append(text_day)
    if text_night and text_night != text_day:
        bits.append(f"夜间{text_night}")
    unit = str(forecast.get("temp_unit") or "°C")
    high = _num(forecast.get("temp_max"))
    low = _num(forecast.get("temp_min"))
    if high and low:
        bits.append(f"{low}~{high}{unit}")
    elif high:
        bits.append(f"最高 {high}{unit}")
    elif low:
        bits.append(f"最低 {low}{unit}")
    probability = forecast.get("precip_prob")
    if isinstance(probability, (int, float)) and not isinstance(probability, bool):
        percent = round(float(probability) * 100)
        if percent >= 30:
            bits.append(f"降水概率 {percent}%")
    if not bits:
        return ""
    return "天气（预报）：" + "，".join(bits)


def _leg_line(leg: dict[str, Any]) -> str:
    """两个景点之间的通行段。过路费只在**驾车且 > 0** 时出现（第 239 步）。

    和界面的 `legText` 同一套规则：0 元不写、没查到不写、不估数。
    """
    parts: list[str] = []
    distance = leg.get("distance_m")
    if isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance > 0:
        parts.append(f"{distance / 1000:.1f} 公里")
    mode = str(leg.get("mode") or "driving")
    mode_name = _MODE_NAMES.get(mode, "驾车")
    minutes = leg.get("duration_min")
    if isinstance(minutes, int) and minutes > 0:
        parts.append(f"{mode_name}约 {minutes} 分钟")
    else:
        walk = leg.get("walk_estimate_min")
        if isinstance(walk, int) and walk > 0:
            parts.append(f"步行约 {walk} 分钟")
    tolls = leg.get("tolls")
    if (
        mode == "driving"
        and isinstance(tolls, (int, float))
        and not isinstance(tolls, bool)
        and tolls > 0
    ):
        parts.append(f"过路费约 {round(tolls)} 元")
    detail = "，".join(parts) or "距离未知"
    source = str(leg.get("source") or "")
    suffix = f"（高德{mode_name}）" if source.startswith("amap_") else "（直线估算）"
    from_name = str(leg.get("from_name") or "").strip()
    to_name = str(leg.get("to_name") or "").strip()
    route = f"{from_name} → {to_name}：" if from_name and to_name else ""
    return f"· 通行 {route}{detail}{suffix}"


#: 通行方式的短名称（与界面的 legText 保持一致）。
_MODE_NAMES = {"driving": "驾车", "walking": "步行", "transit": "公交", "bicycling": "骑行"}


def render_itinerary(trip: dict[str, Any]) -> str:
    """把 `/api/trip` 返回的 ``itinerary`` 渲染成纯文本。

    失败（`status == "failed"`）时**不写行程**，只写原因 ——
    与界面上"失败就不渲染"保持一致。
    """
    # 空字典 = 没有可导出的东西：返回空串，让界面显示"没有可导出的内容"，
    # 而不是拼出一行"行程（（未填城市））"这种噪音。
    if not isinstance(trip, dict) or not trip:
        return ""

    city = str(trip.get("city") or "").strip() or "（未填城市）"
    start = str(trip.get("start_date") or "").strip()
    end = str(trip.get("end_date") or "").strip()
    days = trip.get("days") or []
    status = str(trip.get("status") or "").strip()

    header_bits = [f"{city}"]
    if start and end:
        header_bits.append(f"{start} ~ {end}" if start != end else start)
    elif start:
        header_bits.append(start)
    if days:
        header_bits.append(f"{len(days)} 天")

    lines = ["行程（" + "，".join(header_bits) + "）"]

    transport = str(trip.get("transport") or "").strip()
    stay = str(trip.get("stay") or "").strip()
    if transport or stay:
        lines.append("｜".join(part for part in (transport, stay) if part))

    preferences = [str(item).strip() for item in (trip.get("preferences") or []) if str(item).strip()]
    if preferences:
        lines.append("偏好：" + "、".join(preferences))

    note = str(trip.get("note") or "").strip()
    if note:
        lines.append("额外要求：" + note)

    if status == "failed":
        reasons = [str(item).strip() for item in (trip.get("degraded_reasons") or []) if str(item).strip()]
        lines.append("")
        lines.append("这次没能生成行程：" + ("；".join(reasons) or "没有拿到真实数据。"))
        return "\n".join(lines).strip()

    for day in days:
        if not isinstance(day, dict):
            continue
        date = str(day.get("date") or "").strip()
        index = day.get("index")
        title = f"第 {index} 天" if index else "一天"
        lines.append("")
        lines.append(f"{title}（{date}）" if date else title)

        # 第 239 步：预报跟界面一样逐日给，并标明「预报」。
        forecast_line = _forecast_line(day.get("forecast"))
        if forecast_line:
            lines.append(forecast_line)

        spots = [spot for spot in (day.get("spots") or []) if isinstance(spot, dict)]
        legs = [leg for leg in (day.get("legs") or []) if isinstance(leg, dict)]
        if spots:
            for position, spot in enumerate(spots):
                lines.append(_spot_line(spot))
                # 通行段接在「到达的那一站」下面，和界面的 day.legs[i-1] 对齐
                if position > 0 and position - 1 < len(legs):
                    lines.append(_leg_line(legs[position - 1]))
        else:
            lines.append("· 这一天没有排到景点")

        meal_text = _meal_line([meal for meal in (day.get("meals") or []) if isinstance(meal, dict)])
        if meal_text:
            lines.append("餐饮（建议时段）：" + meal_text)

        hotels = [hotel for hotel in (day.get("hotels") or []) if isinstance(hotel, dict)]
        if hotels:
            names = [str(hotel.get("name") or "").strip() for hotel in hotels[:3]]
            lines.append("住宿候选：" + " / ".join(name for name in names if name))

        notes = [str(item).strip() for item in (day.get("issues") or []) if str(item).strip()]
        for item in notes[:MAX_NOTES]:
            lines.append(f"⚠ {item}")

    issues = [str(item).strip() for item in (trip.get("issues") or []) if str(item).strip()]
    reasons = [str(item).strip() for item in (trip.get("degraded_reasons") or []) if str(item).strip()]
    if issues or reasons:
        lines.append("")
        lines.append("提醒：")
        for item in (issues + reasons)[:MAX_NOTES]:
            lines.append(f"· {item}")

    return "\n".join(lines).strip()


#: 文件名里不能出现的字符（Windows 最严）：\ / : * ? " < > |
_BAD_FILENAME_CHARS = '\\/:*?"<>|'


def markdown_filename(trip: dict[str, Any]) -> str:
    """给导出文件起个名字：城市 + 开始日期，非法字符换成 `-`。

    名字只由"用户自己填的字段"拼成，所以不用怕注入；但 `city` 可能是
    `苏州/杭州` 这种，不换掉的话在 Windows 上存不下来。
    """
    city = str((trip or {}).get("city") or "").strip() or "行程"
    start = str((trip or {}).get("start_date") or "").strip()
    stem = f"{city}-{start}" if start else city
    for char in _BAD_FILENAME_CHARS:
        stem = stem.replace(char, "-")
    # 连续被替换出来的 `-` 压成一个：`a:b/c` 不该变成 `a-b-c` 还是 `a--b--c` 全看运气
    stem = re.sub(r"-{2,}", "-", stem)
    stem = " ".join(stem.split()).strip("-") or "行程"
    return f"{stem}.md"


def render_itinerary_markdown(trip: dict[str, Any]) -> str:
    """把行程渲染成 Markdown（第 218 步）。

    和 `render_itinerary` 是**同一份数据的两种呈现**，所以都放在这个模块里：
    分成两个模块的话，字段一改就会只改一边。

    和纯文本版的区别只有一个：**每个地点带高德地图链接**（有坐标才带）。
    没有坐标的照样只写名字 —— 编一个坐标出来比不显示更糟。
    """
    if not isinstance(trip, dict) or not trip:
        return ""

    city = str(trip.get("city") or "").strip() or "（未填城市）"
    start = str(trip.get("start_date") or "").strip()
    end = str(trip.get("end_date") or "").strip()
    days = [day for day in (trip.get("days") or []) if isinstance(day, dict)]
    status = str(trip.get("status") or "").strip()

    header_bits = [city]
    if start and end:
        header_bits.append(f"{start} ~ {end}" if start != end else start)
    elif start:
        header_bits.append(start)
    if days:
        header_bits.append(f"{len(days)} 天")

    lines = [f"# {city} 行程", ""]
    if len(header_bits) > 1:
        lines.append("- " + "，".join(header_bits[1:]))
        lines.append("")

    transport = str(trip.get("transport") or "").strip()
    stay = str(trip.get("stay") or "").strip()
    if transport or stay:
        lines.append("- " + "｜".join(part for part in (transport, stay) if part))
    preferences = [
        str(item).strip() for item in (trip.get("preferences") or []) if str(item).strip()
    ]
    if preferences:
        lines.append("- 偏好：" + "、".join(preferences))
    note = str(trip.get("note") or "").strip()
    if note:
        lines.append("- 额外要求：" + note)
    if transport or stay or preferences or note:
        lines.append("")

    if status == "failed":
        reasons = [
            str(item).strip()
            for item in (trip.get("degraded_reasons") or [])
            if str(item).strip()
        ]
        lines.append("> 这次没能生成行程：" + ("；".join(reasons) or "没有拿到真实数据。"))
        return "\n".join(lines).strip()

    for day in days:
        date = str(day.get("date") or "").strip()
        index = day.get("index")
        title = f"第 {index} 天" if index else "一天"
        lines.append(f"## {title}（{date}）" if date else f"## {title}")
        lines.append("")

        # 第 239 步：Markdown 也带逐日预报，措辞与纯文本一致。
        forecast_line = _forecast_line(day.get("forecast"))
        if forecast_line:
            lines.append(f"- {forecast_line}")

        spots = [spot for spot in (day.get("spots") or []) if isinstance(spot, dict)]
        legs = [leg for leg in (day.get("legs") or []) if isinstance(leg, dict)]
        if spots:
            for position, spot in enumerate(spots):
                lines.append(_md_spot_line(spot))
                if position > 0 and position - 1 < len(legs):
                    lines.append(f"  {_leg_line(legs[position - 1])}")
        else:
            lines.append("- 这一天没有排到景点")

        meals = [meal for meal in (day.get("meals") or []) if isinstance(meal, dict)]
        for meal in meals:
            name = str(meal.get("name") or "").strip()
            if not name:
                continue
            slot = str(meal.get("slot") or "").strip()
            link = _amap_link(meal)
            label = f"[{_md_text(name)}]({link})" if link else _md_text(name)
            lines.append(f"- 餐饮（建议时段）{'：' + slot if slot else ''} {label}")

        hotels = [hotel for hotel in (day.get("hotels") or []) if isinstance(hotel, dict)]
        if hotels:
            names = [
                _md_text(str(hotel.get("name") or "").strip())
                for hotel in hotels[:3]
                if str(hotel.get("name") or "").strip()
            ]
            if names:
                lines.append("- 住宿候选：" + " / ".join(names))

        day_notes = [str(item).strip() for item in (day.get("issues") or []) if str(item).strip()]
        lines.append("")
        for item in day_notes[:MAX_NOTES]:
            lines.append(f"> ⚠ {item}")
        if day_notes[:MAX_NOTES]:
            lines.append("")

    issues = [str(item).strip() for item in (trip.get("issues") or []) if str(item).strip()]
    reasons = [str(item).strip() for item in (trip.get("degraded_reasons") or []) if str(item).strip()]
    if issues or reasons:
        lines.append("## 提醒")
        lines.append("")
        for item in (issues + reasons)[:MAX_NOTES]:
            lines.append(f"- {item}")

    return "\n".join(lines).strip()
