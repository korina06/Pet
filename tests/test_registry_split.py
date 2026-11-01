"""按功能装配工具面：三套 allow-list 显式、少一个工具就报错、新工具不会漏到所有功能。
"""

from __future__ import annotations

import pytest

from tools.registry import (
    CHAT_TOOLS,
    RECIPE_TOOLS,
    TRAVEL_TOOLS,
    ToolRegistry,
    build_default_registry,
    build_feature_registry,
)


def test_chat_gets_no_tools_at_all():
    """聊天**一个工具都不给**——给了它就会去答出行/菜谱的问题。

    这是第 85 步那条决定的直接后果，也是最容易被"顺手加回来"的地方。
    """
    full = build_default_registry()
    chat = build_feature_registry(full, CHAT_TOOLS, feature="聊天")

    assert chat.names() == [], f"聊天不该有任何工具，实际 {chat.names()}"
    assert CHAT_TOOLS == ()


def test_recipe_gets_search_and_weather_but_not_nearby_places():
    """食谱：检索 + 天气 + 地理编码；**不包含**周边推荐。

    天气是用户明确允许的（"默默查、不做表现"）；周边推荐属于出行/聊天，
    放在食谱里只会让模型在菜谱场景推荐景点。
    """
    full = build_default_registry()
    recipe = build_feature_registry(full, RECIPE_TOOLS, feature="食谱")

    assert "search_recipes" in recipe.names()
    assert "get_weather" in recipe.names(), "天气是用户允许食谱使用的"
    assert "find_nearby_places" not in recipe.names(), "周边推荐不该出现在食谱里"


def test_travel_keeps_the_four_tools_it_actually_uses():
    """出行：地理编码 + 天气 + 周边 + 路线（第 223 步加上的，聊天里要比交通方式）。"""
    full = build_default_registry()
    travel = build_feature_registry(full, TRAVEL_TOOLS, feature="出行")

    assert set(travel.names()) == {
        "geocode_city",
        "get_weather",
        "find_nearby_places",
        "driving_route",
    }


def test_a_missing_tool_is_an_error_not_silent_degradation():
    """allow-list 里写了不存在的工具 → **报错**。

    静默少一个工具会变成"模型想调却调不到"：用户只看到回答变差，
    日志里也什么线索都没有。宁可启动就炸。
    """
    full = build_default_registry()

    with pytest.raises(ValueError) as excinfo:
        build_feature_registry(full, ("不存在的工具",), feature="演示")

    assert "不存在的工具" in str(excinfo.value)
    assert "演示" in str(excinfo.value), "错误里要指出是哪个功能要的"


def test_the_split_registries_share_tool_instances():
    """三套 registry 共用**同一批工具实例**（连接池、缓存才是共享的）。

    如果每个功能各建一套工具实例，httpx 连接池和 TTL 缓存都会被拆成三份 ——
    那是一次没人会注意到的性能回退。
    """
    full = build_default_registry()
    travel = build_feature_registry(full, TRAVEL_TOOLS, feature="出行")
    recipe = build_feature_registry(full, RECIPE_TOOLS, feature="食谱")

    assert travel.get("get_weather") is recipe.get("get_weather")
    assert travel.get("geocode_city") is recipe.get("geocode_city")


def test_a_new_tool_does_not_leak_into_every_feature():
    """新增工具默认**不会**进任何功能（allow-list 的意义所在）。

    用"从全量里删几个"的写法时，新工具会默认出现在聊天、食谱、出行里；
    这里验证相反的行为：不加进 allow-list，就没有任何功能看得见它。
    """

    class _Extra:
        name = "brand_new_tool"

    full = build_default_registry()
    full.register(_Extra())          # 模拟"以后加的新工具"
    assert "brand_new_tool" in full.names()

    for names, feature in (
        (CHAT_TOOLS, "聊天"),
        (RECIPE_TOOLS, "食谱"),
        (TRAVEL_TOOLS, "出行"),
    ):
        picked = build_feature_registry(full, names, feature=feature)
        assert "brand_new_tool" not in picked.names(), (
            f"{feature} 不该自动看见新工具（要显式加进 allow-list）"
        )


def test_an_empty_allow_list_is_a_real_registry_not_none():
    """空的 allow-list 也要返回一个**可用的空 registry**，不能返回 None。

    聊天就是空的那一套：返回 None 会让下游 `registry.names()` 直接崩。
    """
    picked = build_feature_registry(build_default_registry(), (), feature="聊天")

    assert isinstance(picked, ToolRegistry)
    assert picked.names() == []
    assert picked.schemas() == []
