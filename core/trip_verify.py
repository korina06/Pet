"""出行对话的事后核对：把回答里像地名的词与工具真实返回的名字比一遍，对不上的列出来。

按常见后缀抓，会漏；它只提示，不改回答，也不是安全保证。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: 被认为是"地点名"的后缀。只收**景点/场馆/自然地貌/交通枢纽**这类，
#: 不收「街」「路」这种既是地名又是普通词的后缀——误报比漏报更烦人。
PLACE_SUFFIXES: tuple[str, ...] = (
    "古镇",
    "古城",
    "公园",
    "乐园",
    "植物园",
    "动物园",
    "博物馆",
    "美术馆",
    "展览馆",
    "图书馆",
    "纪念馆",
    "科技馆",
    "大剧院",
    "音乐厅",
    "寺",
    "庙",
    "庵",
    "祠",
    "教堂",
    "塔",
    "楼",
    "阁",
    "宫",
    "殿",
    "城墙",
    "故居",
    "遗址",
    "湿地",
    "景区",
    "风景区",
    "度假区",
    "步行街",
    "老街",
    "码头",
    "火车站",
    "高铁站",
    "机场",
    "索道",
    "温泉",
    "滑雪场",
    "湖",
    "山",
    "岭",
    "峰",
    "溪",
    "瀑布",
    "溶洞",
    "沙滩",
    "海滩",
)

#: 抓「连续 2~10 个中文 + 后缀」的整段（后缀要算进名字里，否则「苏州博物馆」
#: 只会被抓成「苏州」，和白名单里的名字永远对不上）。
_CHINESE = r"[\u4e00-\u9fa5]"
_SUFFIX_GROUP = "|".join(sorted(PLACE_SUFFIXES, key=len, reverse=True))
_PATTERN = re.compile(f"({_CHINESE}{{2,10}}?(?:{_SUFFIX_GROUP}))")

#: 这些前缀后面的整句不是地名，先切掉再扫，能少一堆误报。
_NOISE = re.compile(r"例如|比如|像|如「|如\"")


def _names_from(value: Any, out: set[str]) -> None:
    """从工具返回的任意结构里捞 `name` 字段（递归，容忍 list/dict 嵌套）。"""
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            out.add(name.strip())
        for item in value.values():
            _names_from(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _names_from(item, out)


def allowed_names(invocations: Iterable[Any]) -> set[str]:
    """收集这一轮所有**成功**工具调用里出现过的地点名。

    只认 `ok=True` 的结果：失败调用里的内容不可信，拿它当白名单会放过编造。
    """
    names: set[str] = set()
    for invocation in invocations or ():
        if not getattr(invocation, "ok", False):
            continue
        _names_from(getattr(invocation, "data", None), names)
        # 车次工具把站名放在 content 里而不是 data 里，所以补一次
        content = getattr(invocation, "content", None)
        if isinstance(content, str) and content.strip():
            for match in re.finditer(r"([\u4e00-\u9fa5]{2,10})站", content):
                names.add(match.group(1))
    return names


def _strip_noise(text: str) -> str:
    return _NOISE.sub(" ", text)


#: 抓名字前先按标点切开：否则「先去苏州博物馆，再去拙政园」会被当成一个长串，
#: 抓到「先去苏州博物馆」这种带动词前缀的假名字。
_SEGMENT_SPLIT = re.compile(r"[，,。.；;！!？?、：:\s（）()【】\[\]「」“”\"'→\-—~～]+")

#: 中文里最常见的"动词/介词前缀"。名字前带了这几个字很常见
#: （「去看寒山寺」），但对不上白名单，所以要剥掉再比。
_STOP_PREFIX = "先去到在从往再又还就顺带玩逛看游打卡拍吃逛"

#: 一个地名最多算多长。超过这个长度基本是形容词堆出来的假名字，不该单独列。
_MAX_NAME = 8


def _trim(name: str) -> str:
    """剥掉开头的动词/介词，最多剥两个字。"""
    trimmed = name
    for _ in range(2):
        if len(trimmed) > 3 and trimmed[0] in _STOP_PREFIX:
            trimmed = trimmed[1:]
        else:
            break
    return trimmed


def suspect_places(text: str, allowed: Iterable[str]) -> list[str]:
    """返回回答里「像地名、但不在工具结果里」的那些名字（去重、保序）。

    判定规则（保守，宁可漏也不误报太多）：
    - 按标点切段，段内抓「2~8 个中文 + 地名后缀」的片段，并剥掉动词前缀；
    - 片段不在允许集合里；
    - 也不是某个允许名字的一部分（工具常返回「苏州博物馆（本馆）」，回答写
      「苏州博物馆」是合理的）。
    """
    allowed_set = {name.strip() for name in (allowed or ()) if str(name).strip()}
    if not allowed_set:
        # 一条成功结果都没有时，所有地名都不可信 —— 但这时更该说的是"没查到"，
        # 不是列一堆名字，所以直接返回空，由调用方看工具是否成功决定要不要提示。
        return []

    cleaned = _strip_noise(str(text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for segment in _SEGMENT_SPLIT.split(cleaned):
        for match in _PATTERN.finditer(segment):
            name = _trim(match.group(1).strip())
            if len(name) < 2 or len(name) > _MAX_NAME or name in seen:
                continue
            seen.add(name)
            if name in allowed_set:
                continue
            # 回答写简称（「苏州博物馆」）而工具返回全称（「苏州博物馆（本馆）」）
            if any(allowed.startswith(name) or allowed.endswith(name) for allowed in allowed_set):
                continue
            result.append(name)
    return result
