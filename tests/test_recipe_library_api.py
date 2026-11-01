"""菜谱库接口：撞名 409 带已存在 id、局部更新、数组字段校验，库用内存 SQLite 不碰真实数据库。
"""

from __future__ import annotations

from pathlib import Path

import json

import httpx
from fastapi import FastAPI

import core.recipe_seed as seed_module
from api.recipe_library_routes import router
from core.recipe_library import create_recipe, ensure_schema, list_recipes
from core.storage import Storage

#: 这一组自己的迷你种子（含一对别名：番茄/西红柿）
MINI_SEED = {
    "recipes": [
        {
            "name": "西红柿炒蛋",
            "ingredients": ["西红柿 2 个", "鸡蛋 3 个"],
            "steps": ["西红柿切块，鸡蛋打散加盐拌匀",
                      "热锅下油把鸡蛋炒到刚凝固就盛出",
                      "下西红柿炒出汁，倒回鸡蛋翻匀"],
            "tags": ["下饭", "炒"],
            "time_minutes": 15,
            "note": "糖能压酸",
        },
        {
            "name": "番茄牛腩",
            "ingredients": ["番茄 3 个", "牛腩 500g"],
            "steps": ["牛腩冷水下锅焯去血沫后捞出", "加番茄和热水小火炖 90 分钟"],
            "tags": ["汤", "高蛋白"],
            "time_minutes": 100,
            "note": "",
        },
        {
            "name": "紫菜蛋花汤",
            "ingredients": ["紫菜 5g", "鸡蛋 1 个"],
            "steps": ["水开后下紫菜煮一分钟", "淋入蛋液搅成蛋花，加盐调味"],
            "tags": ["汤", "下饭"],
            "time_minutes": 10,
            "note": "",
        },
    ]
}


def build_app(storage: Storage | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if storage is not None:
        app.state.storage = storage
    return app


def memory_db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    return db


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def mini_seed(tmp_path, monkeypatch) -> Path:
    """把种子路径指到临时文件，返回那个路径。

    **刻意写成普通辅助函数而不是 `@pytest.fixture`**：`check_frontend.py` 的
    pytest 替身只支持 `tmp_path` / `monkeypatch` 这类内置 fixture，
    自定义 fixture 会被明确列成"跳过"（而不是静默放过）。
    本组有 10 个用例要靠种子，用自定义 fixture 就等于让它们在这里不跑——
    那正是项目最忌讳的"跳过当真绿"。
    """
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(MINI_SEED, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(seed_module, "SEED_PATH", path)
    return path


def seeded_app(tmp_path, monkeypatch) -> FastAPI:
    mini_seed(tmp_path, monkeypatch)
    return build_app(memory_db())


# ------------------------------------------------------------------ 接线


def test_all_library_routes_are_registered():
    paths = {route.path for route in router.routes}
    assert paths == {
        "/api/recipe/library",
        "/api/recipe/library/stats",
        # 第 137 步：按 id 批量取（恢复会话时补卡片）
        "/api/recipe/library/by-ids",
        "/api/recipe/library/preferences",
        "/api/recipe/library/{recipe_id}",
        # 第 58 步：实拍图（独立图片库，按菜名关联）
        "/api/recipe/library/{recipe_id}/image",
        "/api/recipe/library/seed",
        # 第 126 步：买菜清单（按勾选的菜合并食材）
        "/api/recipe/library/shopping-list",
    }


async def test_missing_storage_is_a_readable_503():
    """没挂 storage 时明说"未初始化"，而不是抛一个 500 栈。"""
    async with make_client(build_app(None)) as client:
        response = await client.get("/api/recipe/library")
    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]


# ------------------------------------------------------------------ 空库与读取


async def test_empty_library_returns_zero_not_error():
    app = build_app(memory_db())
    async with make_client(app) as client:
        response = await client.get("/api/recipe/library")
    assert response.status_code == 200
    body = response.json()
    assert body["recipes"] == [] and body["total"] == 0
    assert body["source_counts"] == {}


async def test_stats_endpoint_on_empty_library():
    app = build_app(memory_db())
    async with make_client(app) as client:
        response = await client.get("/api/recipe/library/stats")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "by_source": {}, "by_tag": {}}


async def test_get_one_and_404():
    app = build_app(memory_db())
    async with make_client(app) as client:
        created = (await client.post("/api/recipe/library", json={"name": "测试菜"})).json()
        ok = await client.get(f"/api/recipe/library/{created['id']}")
        missing = await client.get("/api/recipe/library/424242")

    assert ok.status_code == 200 and ok.json()["name"] == "测试菜"
    assert missing.status_code == 404
    assert "424242" in missing.json()["detail"]


# ------------------------------------------------------------------ 新增


async def test_create_roundtrips_all_fields():
    app = build_app(memory_db())
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/library",
            json={
                "name": "香菇滑鸡",
                "ingredients": ["鸡腿肉 200g", "香菇 4 朵"],
                "steps": ["鸡肉切块腌 10 分钟", "下锅炒到变色再加香菇"],
                "tags": ["炒", "下饭"],
                "time_minutes": 25,
                "note": "香菇先煸出香味",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "香菇滑鸡"
    assert body["ingredients"] == ["鸡腿肉 200g", "香菇 4 朵"]
    assert body["steps"] == ["鸡肉切块腌 10 分钟", "下锅炒到变色再加香菇"]
    assert body["tags"] == ["炒", "下饭"]
    assert body["time_minutes"] == 25
    assert body["note"] == "香菇先煸出香味"
    assert body["source"] == "user", "手动录入默认就是用户自己的菜"
    assert body["origin_url"] == ""


async def test_create_rejects_bare_string_arrays():
    """数组字段必须是列表：裸字符串会被 core 拒绝，接口层用 422 挡住。

    （`steps="第一步"` 会被逐字拆成三个"步"，是 core 明确防的安静灾难。）
    """
    app = build_app(memory_db())
    async with make_client(app) as client:
        response = await client.post(
            "/api/recipe/library", json={"name": "测试", "steps": "第一步"}
        )
    assert response.status_code == 422


async def test_create_rejects_empty_name_and_bad_source():
    app = build_app(memory_db())
    async with make_client(app) as client:
        empty = await client.post("/api/recipe/library", json={"name": ""})
        bad_source = await client.post(
            "/api/recipe/library", json={"name": "测试", "source": "我编的来源"}
        )
    assert empty.status_code == 422, "空菜名该被 pydantic 挡下"
    assert bad_source.status_code == 400
    assert "来源只能是" in bad_source.json()["detail"]


async def test_duplicate_name_is_409_with_existing_id():
    """撞名是"资源已存在"（409），且必须告诉前端已存在那条的 id。"""
    app = build_app(memory_db())
    async with make_client(app) as client:
        first = (await client.post("/api/recipe/library", json={"name": "西红柿炒蛋"})).json()
        again = await client.post(
            "/api/recipe/library", json={"name": "西红柿 炒蛋"}   # 归一化后同名
        )

    assert again.status_code == 409
    assert "西红柿" in again.json()["detail"]
    assert again.headers.get("x-existing-recipe-id") == str(first["id"])


# ------------------------------------------------------------------ 列表 / 检索


async def test_list_pagination_and_total(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        page = await client.get("/api/recipe/library", params={"limit": 2, "offset": 0})
        page2 = await client.get("/api/recipe/library", params={"limit": 2, "offset": 2})

    body = page.json()
    assert body["total"] == 3, "total 应是库里的总数，不是本页条数"
    assert len(body["recipes"]) == 2
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(page2.json()["recipes"]) == 1
    # 两页不该重复
    ids = {row["id"] for row in body["recipes"]}
    assert ids.isdisjoint({row["id"] for row in page2.json()["recipes"]})


async def test_shuffle_returns_a_different_batch_but_the_same_library(tmp_path, monkeypatch):
    """首页「换一批」靠的就是 `shuffle=1`。

    钉两件事：
    1. 它只改**顺序**，不改库存（limit 给足时，集合与不 shuffle 完全一致）；
    2. 它确实会换出别的菜——否则「换一批」点了没变化，等于骗用户。
       （单条取样的重复是随机的，所以这里用"多抽几次的并集"来判定，
       概率上几乎不可能误报：3 道菜抽 limit=1 抽 18 次还只见过 1 道。）
    """
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")

        plain = (await client.get("/api/recipe/library", params={"limit": 50})).json()
        shuffled = (await client.get(
            "/api/recipe/library", params={"limit": 50, "shuffle": 1}
        )).json()
        assert {row["id"] for row in shuffled["recipes"]} == {
            row["id"] for row in plain["recipes"]
        }, "shuffle 只该改顺序，不该让菜消失或多出来"

        page = (await client.get("/api/recipe/library", params={"limit": 2, "shuffle": 1})).json()
        assert len(page["recipes"]) == 2, "shuffle 也必须尊重 limit"

        seen: set[int] = set()
        for _ in range(18):
            one = (await client.get(
                "/api/recipe/library", params={"limit": 1, "shuffle": 1}
            )).json()
            seen.add(one["recipes"][0]["id"])
        assert len(seen) > 1, f"连抽 18 次都只有同一道菜，shuffle 没生效：{seen}"


async def test_list_filters_by_source_and_tag(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        await client.post("/api/recipe/library", json={"name": "我自己的菜", "tags": ["炒"]})

        only_mine = await client.get("/api/recipe/library", params={"source": "user"})
        only_seed = await client.get("/api/recipe/library", params={"source": "seed"})
        tagged = await client.get("/api/recipe/library", params={"tag": "汤"})
        bad = await client.get("/api/recipe/library", params={"source": "瞎写"})

    assert [r["name"] for r in only_mine.json()["recipes"]] == ["我自己的菜"]
    assert only_seed.json()["total"] == 3
    assert {r["name"] for r in tagged.json()["recipes"]} == {"番茄牛腩", "紫菜蛋花汤"}
    assert bad.status_code == 400


async def test_search_hits_name_ingredients_and_alias(tmp_path, monkeypatch):
    """检索要点：命中菜名/食材，且**别名双向展开**（番茄 ↔ 西红柿）。"""
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")

        by_name = await client.get("/api/recipe/library", params={"q": "蛋花"})
        # 别名：种子里"西红柿炒蛋"，用"番茄"也该搜到
        by_alias = await client.get("/api/recipe/library", params={"q": "番茄"})
        # 食材字段命中："牛腩"只在 ingredients 里
        by_ingredient = await client.get("/api/recipe/library", params={"q": "牛腩"})
        nothing = await client.get("/api/recipe/library", params={"q": "外星食材"})

    assert [r["name"] for r in by_name.json()["recipes"]] == ["紫菜蛋花汤"]
    alias_names = {r["name"] for r in by_alias.json()["recipes"]}
    assert "西红柿炒蛋" in alias_names, f"别名没生效：{alias_names}"
    assert [r["name"] for r in by_ingredient.json()["recipes"]] == ["番茄牛腩"]
    assert nothing.json()["recipes"] == [] and nothing.json()["total"] == 0


async def test_search_total_reflects_hits_not_library_size(tmp_path, monkeypatch):
    """回归：`total` 必须是**命中数**。

    以前只能拿"库里一共多少道"当 total，界面会出现"共 30 道"却列出 0 条的怪象。
    """
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        hit = await client.get("/api/recipe/library", params={"q": "蛋花"})
        miss = await client.get("/api/recipe/library", params={"q": "外星食材"})

    assert hit.json()["total"] == 1, "命中 1 条就该报 1"
    assert miss.json()["total"] == 0
    # 而全库总数仍由 source_counts 之外的 total 表达——两个字段语义不同，不能混：
    # total = 本次查询命中数；source_counts = 全库按来源分布（与查询无关）
    assert hit.json()["source_counts"] == {"seed": 3}


async def test_search_pagination_does_not_lie_about_total(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        first = await client.get("/api/recipe/library", params={"q": "汤", "limit": 1})
    body = first.json()
    assert len(body["recipes"]) == 1
    assert body["total"] >= 2, "命中多条时 total 应报命中总数，便于前端翻页"


# ------------------------------------------------------------------ 局部更新


async def test_patch_only_touches_given_fields():
    app = build_app(memory_db())
    async with make_client(app) as client:
        created = (
            await client.post(
                "/api/recipe/library",
                json={"name": "测试菜", "ingredients": ["鸡蛋 1 个"], "time_minutes": 9},
            )
        ).json()
        response = await client.patch(
            f"/api/recipe/library/{created['id']}", json={"note": "只改备注"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["note"] == "只改备注"
    assert body["name"] == "测试菜", "没传的字段不该被动"
    assert body["ingredients"] == ["鸡蛋 1 个"]
    assert body["time_minutes"] == 9


async def test_patch_can_clear_time_but_rejects_null_name():
    """`time_minutes: null` = 清掉时间；`name: null` = 必须拒绝。

    放行 name=null 的话，core 的 `str(value).strip()` 会把菜名写成 "None"。
    """
    app = build_app(memory_db())
    async with make_client(app) as client:
        created = (
            await client.post("/api/recipe/library", json={"name": "测试菜", "time_minutes": 9})
        ).json()
        cleared = await client.patch(
            f"/api/recipe/library/{created['id']}", json={"time_minutes": None}
        )
        bad = await client.patch(
            f"/api/recipe/library/{created['id']}", json={"name": None}
        )
        still = await client.get(f"/api/recipe/library/{created['id']}")

    assert cleared.status_code == 200 and cleared.json()["time_minutes"] is None
    assert bad.status_code == 400 and "不能是 null" in bad.json()["detail"]
    assert still.json()["name"] == "测试菜", "被拒的请求不该改坏菜名"


async def test_patch_rename_to_existing_name_is_409():
    app = build_app(memory_db())
    async with make_client(app) as client:
        first = (await client.post("/api/recipe/library", json={"name": "菜甲"})).json()
        second = (await client.post("/api/recipe/library", json={"name": "菜乙"})).json()
        response = await client.patch(
            f"/api/recipe/library/{second['id']}", json={"name": "菜甲"}
        )
        unchanged = await client.get(f"/api/recipe/library/{second['id']}")

    assert response.status_code == 409
    assert response.headers.get("x-existing-recipe-id") == str(first["id"])
    assert unchanged.json()["name"] == "菜乙", "改名失败不该动原记录"


async def test_patch_and_delete_missing_id_are_404():
    app = build_app(memory_db())
    async with make_client(app) as client:
        patched = await client.patch("/api/recipe/library/999", json={"note": "x"})
        deleted = await client.delete("/api/recipe/library/999")
    assert patched.status_code == 404
    assert deleted.status_code == 404


async def test_delete_then_list_is_empty(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        rows = (await client.get("/api/recipe/library")).json()["recipes"]
        for row in rows:
            assert (await client.delete(f"/api/recipe/library/{row['id']}")).status_code == 200
        after = await client.get("/api/recipe/library")

    assert after.json()["total"] == 0
    assert after.json()["recipes"] == []


# ------------------------------------------------------------------ 种子导入


async def test_seed_endpoint_imports_then_skips(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        first = await client.post("/api/recipe/library/seed")
        second = await client.post("/api/recipe/library/seed")

    assert first.status_code == 200
    body = first.json()
    assert body == {"added": 3, "skipped": 0, "total": 3, "library_total": 3}

    assert second.json()["added"] == 0
    assert second.json()["skipped"] == 3
    assert second.json()["library_total"] == 3, "重复导入不该让总数翻倍"


async def test_seed_import_never_overwrites_user_recipe(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        mine = (
            await client.post(
                "/api/recipe/library",
                json={"name": "西红柿炒蛋", "ingredients": ["我的配方 1 份"], "steps": ["我的做法"]},
            )
        ).json()
        result = await client.post("/api/recipe/library/seed")
        after = await client.get(f"/api/recipe/library/{mine['id']}")

    assert result.json()["skipped"] == 1
    assert result.json()["added"] == 2
    assert after.json()["source"] == "user"
    assert after.json()["ingredients"] == ["我的配方 1 份"], "用户录的被示例数据盖掉了"


async def test_seed_endpoint_reports_broken_seed_as_500(tmp_path, monkeypatch):
    """种子坏了要明说是"内置数据有问题"，别让用户以为是自己操作错。"""
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"recipes": [{"name": "缺东西的菜"}]}, ensure_ascii=False),
                    encoding="utf-8")
    monkeypatch.setattr(seed_module, "SEED_PATH", path)

    app = build_app(memory_db())
    async with make_client(app) as client:
        response = await client.post("/api/recipe/library/seed")

    assert response.status_code == 500
    assert "内置示例菜谱有问题" in response.json()["detail"]


async def test_preferences_returns_avoid_options_and_want_groups(tmp_path, monkeypatch):
    """右栏那个"装标签的框"要的数据，一次取回。

    选项文案由**后端**定（前端不硬编码中文），而且 `want_groups` 里的标签
    必须是菜谱库里真实存在的 tag——否则点了"没菜"。
    """
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        response = await client.get("/api/recipe/library/preferences")

    assert response.status_code == 200
    body = response.json()
    assert body["aversions"] == [], "还没点过任何忌口"
    assert "香菜" in body["avoid_options"] and "海鲜" in body["avoid_options"]
    labels = [group["label"] for group in body["want_groups"]]
    # 50f 换库后重新对过：工具组在 96 道地方菜里没有对应标签，换成了菜系/场景
    assert labels == ["口味", "菜系", "场景", "时间", "怎么吃"], labels
    # 分组里的 tag 要能在库里找到（种子里确实有这些 tag）
    tags = {tag for group in body["want_groups"] for tag in group["tags"]}
    assert {"清淡", "下饭"} <= tags

    # 第 52 步：时间组多了「30 分钟以上」，且只有「时间」「怎么吃」是互斥组
    by_label = {group["label"]: group for group in body["want_groups"]}
    assert "30 分钟以上" in by_label["时间"]["tags"]
    assert by_label["时间"]["exclusive"] is True
    assert by_label["怎么吃"]["exclusive"] is True
    assert by_label["口味"]["exclusive"] is False, "口味要能多选（又辣又下饭）"
    assert by_label["菜系"]["exclusive"] is False


async def test_aversions_are_saved_and_returned(tmp_path, monkeypatch):
    """忌口词条：整份替换、幂等、去重、空串被丢掉。"""
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        saved = await client.put(
            "/api/recipe/library/preferences",
            json={"aversions": ["香菜", "海鲜", "香菜", "  "]},
        )
        again = (await client.get("/api/recipe/library/preferences")).json()

    assert saved.status_code == 200
    assert saved.json()["aversions"] == ["香菜", "海鲜"], "去重 + 丢掉空白项"
    assert again["aversions"] == ["香菜", "海鲜"], "再读一次还在（真的落库了）"

    async with make_client(app) as client:
        cleared = await client.put(
            "/api/recipe/library/preferences", json={"aversions": []}
        )
    assert cleared.json()["aversions"] == []


async def test_aversions_survive_a_reopen(tmp_path, monkeypatch):
    """关库再开（模拟重启）后，忌口词条还在——它存在 SQLite 里，不是前端状态。"""
    from core.recipe_library import list_aversions, set_aversions

    db_file = tmp_path / "pet.db"
    first = Storage(str(db_file))
    ensure_schema(first)
    set_aversions(first, ["内脏", "牛羊肉"])
    first.close()

    second = Storage(str(db_file))
    try:
        ensure_schema(second)
        assert list_aversions(second) == ["内脏", "牛羊肉"]
    finally:
        second.close()


def test_old_v2_table_shape_still_works():
    """回归：**已经发布过的迁移不能改内容**。

    真事：第 50 步中途把 v2 从 `term TEXT PRIMARY KEY` 改成带 `id` 的版本，
    而用户的库早已升到 v2、不会重跑迁移 → 代码里 `ORDER BY id` 撞上"没有 id 列"，
    `/preferences` 返回 500，右栏标签一片空白（用户截图里就是空的）。

    这条用例手工造出**最初发布的那个形状**（无 id 列），确认读写都正常——
    以后再有人想改已发布的迁移，会先撞到它。
    """
    from core.recipe_library import list_aversions, set_aversions

    db = Storage(":memory:")
    ensure_schema(db)                     # 正常升到 v2（新形状）
    # 把 v2 那张表换回**最初发布的老形状**，但版本号仍是 2 —— 这才是用户库的真实样子
    db.execute("DROP TABLE recipe_aversions")
    db.execute(
        "CREATE TABLE recipe_aversions ("
        "term TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    # 版本号也要**退回 2**：否则下一次 ensure_schema 会认为"已经是最新"，
    # 拿不到"老库 + 新代码"这个真实组合。第 91 步加了 v3（chat_messages），
    # 这条用例的价值恰恰在于：后面的迁移可以继续加，但 v2 的形状永远不许动。
    db.execute("PRAGMA user_version = 2")
    assert db.user_version() == 2

    set_aversions(db, ["香菜", "海鲜"])
    assert list_aversions(db) == ["香菜", "海鲜"], "老形状的表也要能按点选顺序读回来"

    # 再跑一次迁移：已经 v2 了应当什么都不做，更不该因为 SCHEMA_V2 内容变化去重建表
    ensure_schema(db)
    assert list_aversions(db) == ["香菜", "海鲜"]
    columns = {row["name"] for row in db.query("PRAGMA table_info(recipe_aversions)")}
    assert columns == {"term", "created_at"}, "不该擅自给老表加列"


# ------------------------------------------------------------------ 统计


async def test_stats_counts_by_source_and_tag(tmp_path, monkeypatch):
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        await client.post("/api/recipe/library", json={"name": "我自己的菜", "tags": ["汤"]})
        response = await client.get("/api/recipe/library/stats")

    body = response.json()
    assert body["total"] == 4
    assert body["by_source"] == {"seed": 3, "user": 1}
    assert body["by_tag"]["汤"] == 3, "种子里两道汤 + 我的一道"
    # 次数降序：汤(3) 应排在下饭(2) 前面
    tags = list(body["by_tag"])
    assert tags[0] == "汤", f"标签应按次数降序：{tags}"


async def test_list_response_carries_source_counts_for_the_header(tmp_path, monkeypatch):
    """列表响应自带按来源计数：界面顶部那行"共 N 道（其中 M 道示例）"一次拿到。

    字段叫 `source_counts` 而不是 `stats`：`GET /stats` 里那个 `stats`
    是 `{total, by_source, by_tag}`，同名不同形状会坑前端（自查时发现并改掉）。
    """
    app = seeded_app(tmp_path, monkeypatch)
    async with make_client(app) as client:
        await client.post("/api/recipe/library/seed")
        body = (await client.get("/api/recipe/library")).json()

    assert body["total"] == 3
    assert body["source_counts"] == {"seed": 3}
    assert "stats" not in body, "别再用与 /stats 同名却不同形状的字段"


# ------------------------------------------- 实拍图（第 58 步，独立图片库）


def _app_with_images(storage: Storage, tmp_path, rows: dict[str, bytes]) -> FastAPI:
    """造一个最小图片库并挂到 app 上（按菜名关联，与菜谱库分开）。"""
    import sqlite3

    from core.recipe_images import ImageStore
    from core.recipe_library import normalize_name_key

    path = tmp_path / "images.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE image_meta (name_key TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "format TEXT NOT NULL, mime TEXT NOT NULL, width INTEGER, height INTEGER, "
        "bytes INTEGER NOT NULL, source_url TEXT NOT NULL DEFAULT '', "
        "imported_at TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE image_blob (name_key TEXT PRIMARY KEY, data BLOB NOT NULL)")
    for name, data in rows.items():
        key = normalize_name_key(name)
        conn.execute(
            "INSERT INTO image_meta VALUES (?, ?, 'png', 'image/png', 1, 1, ?, '', 'x')",
            (key, name, len(data)),
        )
        conn.execute("INSERT INTO image_blob VALUES (?, ?)", (key, data))
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.storage = storage
    app.state.image_store = ImageStore(path)
    return app


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


async def test_recipe_image_is_served_with_cache_headers(tmp_path):
    """有图 → 200 + 正确 Content-Type + ETag；带上 If-None-Match 走 304。"""
    db = memory_db()
    create_recipe(
        db, name="白切鸡", ingredients=["鸡 1 只"], steps=["煮 20 分钟后浸冰水切件"],
        tags=["清淡"], time_minutes=30, source="seed",
    )
    recipe_id = list_recipes(db)[0].id
    app = _app_with_images(db, tmp_path, {"白切鸡": PNG_BYTES})

    async with make_client(app) as client:
        first = await client.get(f"/api/recipe/library/{recipe_id}/image")
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/png"
        assert first.content == PNG_BYTES
        etag = first.headers["etag"]
        assert "max-age" in first.headers["cache-control"]

        again = await client.get(
            f"/api/recipe/library/{recipe_id}/image",
            headers={"If-None-Match": etag},
        )
        assert again.status_code == 304, "同一张图不该重复下载"
        assert again.content == b""


async def test_recipe_without_image_is_404_not_500(tmp_path):
    """用户自己录的菜没有图：404，前端据此静默隐藏图片位。"""
    db = memory_db()
    create_recipe(
        db, name="我自创的菜", ingredients=["土豆 2 个"], steps=["土豆切丝下锅炒熟"],
        tags=[], time_minutes=10, source="user",
    )
    recipe_id = list_recipes(db)[0].id
    app = _app_with_images(db, tmp_path, {"别的菜": PNG_BYTES})

    async with make_client(app) as client:
        response = await client.get(f"/api/recipe/library/{recipe_id}/image")

    assert response.status_code == 404
    assert "还没有图片" in response.json()["detail"]


async def test_no_image_store_at_all_is_also_404(tmp_path):
    """连图片库文件都没有（少一套资产）：也是 404，应用照常工作。"""
    db = memory_db()
    create_recipe(
        db, name="白切鸡", ingredients=["鸡 1 只"], steps=["煮 20 分钟后浸冰水切件"],
        tags=["清淡"], time_minutes=30, source="seed",
    )
    recipe_id = list_recipes(db)[0].id
    app = FastAPI()
    app.include_router(router)
    app.state.storage = db            # 故意不挂 image_store

    async with make_client(app) as client:
        response = await client.get(f"/api/recipe/library/{recipe_id}/image")

    assert response.status_code == 404
