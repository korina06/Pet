"""注册表装配：工具真的接上了、不进轮盘、没有 storage 也能起。

工具本身的逻辑在 `tests/test_recipe_search_tool.py`，两边分工不同。
"""

from __future__ import annotations

from core.recipe_library import create_recipe, ensure_schema
from core.storage import Storage
from tools.registry import build_default_registry


def memory_db() -> Storage:
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="番茄炒蛋",
        ingredients=["番茄 2 个", "鸡蛋 3 个"],
        steps=["番茄切块", "鸡蛋炒凝固后下番茄炒匀"],
        tags=["主食", "下饭"],
        time_minutes=10,
    )
    return db


def test_registry_includes_the_recipe_search_tool():
    registry = build_default_registry(storage=memory_db())
    assert "search_recipes" in registry.names()
    # 它已经会出现在模型的 tools schema 里（说明模型看得见）
    names = [
        schema["function"]["name"]
        for schema in registry.schemas()
    ]
    assert "search_recipes" in names


def test_recipe_tool_is_not_a_wheel_entry():
    """它是"模型自己决定何时用"的能力，不是手动入口——
    进轮盘会变成第 15 步那批被删掉的工具项。"""
    registry = build_default_registry(storage=memory_db())
    assert "search_recipes" not in registry.menu_names()


def test_registry_still_builds_without_storage():
    """主程序万一没注入 storage，装配也不该炸；工具会回一句可读的失败。"""
    registry = build_default_registry()
    assert "search_recipes" in registry.names()
    tool = registry.get("search_recipes")
    assert tool is not None
    assert tool.show_in_menu is False


def test_tool_is_wired_to_the_injected_storage():
    """注入的库要真的被工具用上：查得到刚写进去的那道菜。"""
    import asyncio

    db = memory_db()
    registry = build_default_registry(storage=db)
    result = asyncio.run(registry.execute("search_recipes", {"keyword": "番茄"}))
    assert result.ok, result.error
    payload = result.as_payload()
    assert payload["count"] >= 1
    assert any(item["name"] == "番茄炒蛋" for item in payload["items"])


def test_the_fallback_helper_prefers_the_feature_registry():
    """取用顺序：有 feature registry 就用它；没有才回退，并且**出声**。

    第 137 步从 `test_eval_golden.py` 挪过来：那边属于"纯标准库"组，
    而这里 import 的 `tools.registry` 会拉到 pydantic_settings。
    """
    from tools.registry import build_default_registry, feature_registry_for

    class State:
        pass

    state = State()
    full = build_default_registry()
    state.registry = full
    state.recipe_registry = "recipe-only"

    assert feature_registry_for(state, "recipe_registry", feature="食谱") == "recipe-only"
    # 没有 feature registry 时回退到 registry（测试场景）
    assert feature_registry_for(state, "travel_registry", feature="出行") is full
