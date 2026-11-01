"""买菜清单：数量原样保留、只按名字归组 —— 食材文本里的数字和单位没法做算术。
"""

from __future__ import annotations

import asyncio

from core.shopping_list import (
    build_shopping_list,
    normalize_name,
    render_text,
    split_ingredient,
)


def test_splits_real_ingredients_from_the_library():
    """真实数据（本机库 527 条里挑的典型写法）。"""
    cases = {
        "三黄鸡半只（约 750g）": ("三黄鸡", "半只"),
        "姜 1 大块": ("姜", "1 大块"),
        "葱 2 根": ("葱", "2 根"),
        "料酒 1 勺": ("料酒", "1 勺"),
        "姜丝 适量": ("姜丝", "适量"),
        "冰水一盆": ("冰水", "一盆"),
        "香油几滴": ("香油", "几滴"),
        "红曲粉半茶匙（增色）": ("红曲粉", "半茶匙"),
        "猪梅花肉 500g": ("猪梅花肉", "500g"),
        "鲈鱼 1 条（约 600g）": ("鲈鱼", "1 条"),
    }

    for text, expected in cases.items():
        assert split_ingredient(text) == expected, text


def test_parentheses_are_not_mistaken_for_the_amount():
    """`三黄鸡半只（约 750g）` 的 750g 是**说明**，不是要买 750 克鸡肉。"""
    name, amount = split_ingredient("三黄鸡半只（约 750g）")

    assert name == "三黄鸡"
    assert "750" not in amount


def test_amounts_are_kept_verbatim_and_never_added():
    """同名不同单位**不合并数字**：这是整个模块的核心承诺。"""
    items = build_shopping_list(
        [
            ("白切鸡", ["姜 1 大块", "葱 2 根"]),
            ("清蒸鲈鱼", ["姜 1 块", "葱 2 段"]),
        ]
    )

    by_name = {item.name: item for item in items}
    assert by_name["姜"].amounts == ["1 大块", "1 块"], by_name["姜"].amounts
    assert by_name["葱"].amounts == ["2 根", "2 段"], by_name["葱"].amounts
    # 没有"3 块姜"或"4 根葱"这种编出来的数字
    assert all("3" not in amount for amount in by_name["姜"].amounts)
    assert by_name["姜"].recipes == ["白切鸡", "清蒸鲈鱼"]


def test_identical_amounts_are_only_listed_once():
    items = build_shopping_list(
        [
            ("白切鸡", ["料酒 1 勺"]),
            ("白灼虾", ["料酒 1 勺"]),
        ]
    )

    assert len(items) == 1
    assert items[0].amounts == ["1 勺"]
    assert items[0].recipes == ["白切鸡", "白灼虾"]


def test_ingredients_keep_the_order_they_were_first_seen():
    items = build_shopping_list([("菜", ["猪梅花肉 500g", "蜂蜜 2 勺", "生抽 1 勺"])])

    assert [item.name for item in items] == ["猪梅花肉", "蜂蜜", "生抽"]


def test_vague_amounts_survive():
    items = build_shopping_list([("菜", ["姜丝 适量", "香油少许", "盐"])])

    assert {item.name for item in items} == {"姜丝", "香油", "盐"}
    amounts = {item.name: item.amounts for item in items}
    assert amounts["姜丝"] == ["适量"]
    assert amounts["香油"] == ["少许"]
    assert amounts["盐"] == [], "没写数量就是没写，不要补一个假的"


def test_names_are_normalized_for_grouping_but_display_keeps_the_first_form():
    """`姜` 与 `姜 `（多个空格）该归一组；显示用第一次出现的写法。"""
    assert normalize_name(" 姜 ") == normalize_name("姜") == "姜"

    items = build_shopping_list([("菜", ["姜 1 块", "姜  2 块"])])

    assert len(items) == 1
    assert items[0].name == "姜"
    assert items[0].amounts == ["1 块", "2 块"]


def test_render_text_is_pasteable():
    items = build_shopping_list([("白切鸡", ["三黄鸡半只（约 750g）", "姜 1 大块"])])
    text = render_text(items)

    assert text.startswith("买菜清单")
    assert "三黄鸡" in text and "半只" in text
    assert "白切鸡" in text, "每项要能看出是哪道菜要的"


# --------------------------------------------------------------- 接口


def _client(storage):
    import httpx

    from api.recipe_library_routes import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.state.storage = storage
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_the_endpoint_merges_picked_recipes():
    from core.recipe_library import create_recipe, ensure_schema
    from core.storage import Storage

    db = Storage(":memory:")
    ensure_schema(db)
    first = create_recipe(
        db, name="甲菜", tags=[], ingredients=["姜 1 大块", "葱 2 根"], steps=["做"]
    )
    second = create_recipe(
        db, name="乙菜", tags=[], ingredients=["姜 1 块"], steps=["做"]
    )

    async def run() -> dict:
        async with _client(db) as client:
            response = await client.post(
                "/api/recipe/library/shopping-list",
                json={"recipe_ids": [first.id, second.id]},
            )
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    names = [item["name"] for item in payload["items"]]
    assert names == ["姜", "葱"], names
    ginger = next(item for item in payload["items"] if item["name"] == "姜")
    assert ginger["amounts"] == ["1 大块", "1 块"]
    assert payload["text"].startswith("买菜清单")
    assert payload["missing"] == []


def test_the_endpoint_reports_ids_that_no_longer_exist():
    from core.recipe_library import ensure_schema
    from core.storage import Storage

    db = Storage(":memory:")
    ensure_schema(db)

    async def run() -> dict:
        async with _client(db) as client:
            response = await client.post(
                "/api/recipe/library/shopping-list", json={"recipe_ids": [9999]}
            )
            assert response.status_code == 200, response.text
            return response.json()

    payload = asyncio.run(run())
    assert payload["missing"] == [9999], payload
    assert payload["items"] == []
    assert payload["text"] == ""


def test_a_quantity_inside_parentheses_is_kept():
    """`鸡蛋（2个）` 的数量在括号里（第 145 步 / 评价9 P2）。

    原来先摘括号再找数量，于是这条只剩名字、数量丢了 —— 清单上会写"鸡蛋：按需"，
    而用户明明写了 2 个。
    """
    assert split_ingredient("鸡蛋（2个）") == ("鸡蛋", "2个")
    assert split_ingredient("葱（两根）") == ("葱", "两根")
    # 括号外已经有数量时，括号里的说明不许顶掉它
    assert split_ingredient("三黄鸡半只（约 750g）") == ("三黄鸡", "半只")
    # 括号里不是数量时，别把"增色"当数量
    assert split_ingredient("红曲粉半茶匙（增色）") == ("红曲粉", "半茶匙")
