"""MCP server 的协议层：schema 与工具定义同源、只导出收窄名单、握手与各类错误路径各就各位。
"""

from __future__ import annotations

import asyncio
import ast
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recipe_library import create_recipe, ensure_schema  # noqa: E402
from core.storage import Storage  # noqa: E402
from scripts.mcp_server import (  # noqa: E402
    ALLOWED_TOOLS,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcError,
    ToolHost,
    call_tool,
    handle_message,
    serve,
)
from tools.recipe_search_tool import RecipeSearchTool  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _db() -> Storage:
    """内存库 + 两道能命中『辣 + 鸡肉』的菜。"""
    db = Storage(":memory:")
    ensure_schema(db)
    create_recipe(
        db,
        name="宫保鸡丁",
        ingredients=["鸡腿肉 300g", "花生米 50g", "干辣椒 8 个"],
        steps=["鸡腿肉切丁上浆", "炒香料后下鸡丁", "收汁撒花生米"],
        tags=["辣", "川菜", "下饭"],
        time_minutes=25,
        source="seed",
    )
    create_recipe(
        db,
        name="清蒸鲈鱼",
        ingredients=["鲈鱼 1 条", "姜 3 片"],
        steps=["抹盐腌 10 分钟", "上锅蒸 8 分钟", "淋豉油"],
        tags=["清淡", "蒸"],
        time_minutes=20,
        source="seed",
    )
    return db


def _call(host: ToolHost, message: dict) -> dict | None:
    return asyncio.run(handle_message(host, message))


# ------------------------------------------------------------------ 暴露面
def test_only_the_narrow_allow_list_is_exposed():
    """MCP 默认"有什么就能看见什么"，所以收窄必须显式写、并且被钉住。"""
    host = ToolHost(_db())
    names = [row["name"] for row in host.list_definitions()]

    assert names == list(ALLOWED_TOOLS), names
    assert names == ["search_recipes"], "多导出任何一个工具都要先改这条断言"


def test_input_schema_is_the_tool_class_own_dict():
    """schema 只有一个真相：拿类上那份，不复制。改参数时两边不会漂。"""
    host = ToolHost(_db())
    schema = host.list_definitions()[0]["inputSchema"]

    assert schema == RecipeSearchTool.parameters
    assert set(schema["properties"]) >= {"keyword", "tags", "ingredients", "max_minutes"}
    # `query_text` 是给向量召回用的入口参数（第 193 步），必须在 schema 里
    assert "query_text" in schema["properties"], "向量入口参数从 schema 里掉了"


# ------------------------------------------------------------------ 协议
def test_initialize_echoes_a_supported_version():
    host = ToolHost(_db())
    response = _call(
        host,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    )

    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2024-11-05", "客户端给的是支持版本，就该照它回"
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False
    assert response["result"]["serverInfo"]["name"] == "pet-recipes"


def test_initialize_falls_back_when_the_client_version_is_unknown():
    host = ToolHost(_db())
    response = _call(
        host,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
    )

    assert response["result"]["protocolVersion"] == "2025-06-18", "不支持就别硬回，回自己最新的"


def test_notification_gets_no_response():
    """`notifications/initialized` 没有 id —— 回了就是协议错误。"""
    host = ToolHost(_db())
    assert _call(host, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_method_not_found():
    host = ToolHost(_db())
    response = _call(host, {"jsonrpc": "2.0", "id": 7, "method": "shutdown"})

    assert response["error"]["code"] == METHOD_NOT_FOUND
    assert "shutdown" in response["error"]["message"]


def test_tools_list_goes_through_the_wire_shape():
    host = ToolHost(_db())
    response = _call(host, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    tools = response["result"]["tools"]
    assert len(tools) == 1
    assert set(tools[0]) == {"name", "description", "inputSchema"}, "MCP 的字段名不能自创"


# ------------------------------------------------------------------ 调用
def test_tools_call_returns_the_real_search_result():
    host = ToolHost(_db())
    response = _call(
        host,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_recipes",
                "arguments": {"tags": ["辣"], "ingredients": ["鸡肉"], "max_minutes": 30},
            },
        },
    )

    result = response["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["count"] >= 1
    assert any(item["name"] == "宫保鸡丁" for item in payload["items"]), payload


def test_unknown_tool_is_invalid_params():
    host = ToolHost(_db())
    response = _call(
        host,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}},
    )

    assert response["error"]["code"] == INVALID_PARAMS
    assert "rm_rf" in response["error"]["message"]


def test_unknown_argument_keys_are_dropped_not_fatal():
    """client 会塞 `_meta` 之类的东西；把它 `**` 进工具就是 TypeError —— 那不是工具失败。"""
    host = ToolHost(_db())
    response = _call(
        host,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "search_recipes",
                "arguments": {"tags": ["辣"], "_meta": {"progressToken": 1}, "nonsense": True},
            },
        },
    )

    assert "result" in response, response
    assert response["result"]["isError"] is False


def test_missing_storage_reports_a_tool_error_not_a_crash():
    """没有库时工具本来就返回 ok=False；MCP 层要把它翻成 `isError`，而不是 JSON-RPC 错误。"""
    response = asyncio.run(
        call_tool(
            ToolHost(None),
            "search_recipes",
            {"tags": ["辣"]},
        )
    )

    assert response["isError"] is True
    assert response["content"][0]["text"].startswith("工具执行失败：")
    assert "菜谱库" in response["content"][0]["text"]


def test_bad_arguments_type_is_rejected():
    host = ToolHost(_db())
    try:
        asyncio.run(call_tool(host, "search_recipes", ["not", "an", "object"]))
    except RpcError as error:
        assert error.code == INVALID_PARAMS
    else:  # pragma: no cover - 走到这里就是没拦住
        raise AssertionError("arguments 不是对象时必须报 -32602")


# ------------------------------------------------------------------ stdio 循环
def test_serve_reads_lines_and_writes_one_response_each():
    host = ToolHost(_db())
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                "  ",
                "{ 这不是 json",
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    code = serve(host, stdin, stdout)

    assert code == 0
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3, lines  # 通知不产生响应；空行不算
    parsed = [json.loads(line) for line in lines]
    assert [row.get("id") for row in parsed] == [1, 2, None]
    assert parsed[2]["error"]["code"] == PARSE_ERROR


# ------------------------------------------------------------------ 边界面
def test_server_stays_an_outsider_script():
    """它是外挂脚本，不是应用的一部分：主应用不该 import 它。

    依赖口径在第 215 步变过：那时我们开始**当客户端**连别人的 MCP 服务器
    （`tools/mcp_remote.py`），`mcp` 因此进了 requirements。原来那句"清单里不该有
    mcp"守的其实是"别为了跑这个脚本偷偷装 SDK"——这层意思改由更精确的断言承接：
    **除了客户端那一个文件，谁都不许 import mcp**；而服务器脚本本身依旧零第三方
    依赖（见下面 `test_no_third_party_imports_in_the_server`）。
    """
    for relative in ("main.py", "api/routes.py", "core/storage.py", "tools/registry.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "mcp_server" not in source, f"{relative} 不该依赖这个脚本"

    def imports_mcp(path: Path) -> bool:
        """用 ast 看真的 import 节点 —— 脚本文首的 SDK 示例是注释，grep 会误判。"""
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == "mcp" for alias in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] == "mcp":
                    return True
        return False

    importers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.py")
        if ".testtmp" not in path.as_posix() and imports_mcp(path)
    )
    assert importers == ["tools/mcp_remote.py"], (
        f"只有客户端该 import mcp，实际还有：{importers}"
    )


def test_no_third_party_imports_in_the_server():
    """零依赖是它的卖点之一：真正被 import 的只有标准库 + 本项目模块。

    用 `ast` 看 import 节点，而不是 grep 文本 —— 脚本文首那段 SDK 示例是**注释**，
    grep 会把"我告诉你换成 SDK 该写什么"误判成"我已经依赖了 SDK"。
    """
    tree = ast.parse((ROOT / "scripts/mcp_server.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    allowed_third_party: set[str] = {"core", "tools", "agents"}
    stdlib = {"__future__", "argparse", "asyncio", "json", "sys", "pathlib", "typing"}
    unexpected = imported - stdlib - allowed_third_party - {"mcp", "fastmcp"}
    assert not unexpected, f"多出来的依赖：{sorted(unexpected)}"

    assert "mcp" not in imported and "fastmcp" not in imported, "SDK 只该出现在注释里"

# ---------------------------------------------------------------- probe（第 208 步）
def test_probe_rejects_bad_json_params():
    """`scripts/mcp_probe.py` 的第一道闸：参数不是 JSON 对象要立刻失败，别去起子进程。"""
    from scripts.mcp_probe import main as probe_main

    assert probe_main(["--params", "不是 json"]) == 1
    assert probe_main(["--params", "[1, 2]"]) == 1


def test_probe_and_server_agree_on_what_a_client_sends():
    """握手用的协议版本必须是服务端支持列表里的一个 —— 否则真 client 连上就报版本不符。"""
    from scripts import mcp_probe
    from scripts.mcp_server import SUPPORTED_VERSIONS

    assert mcp_probe.PROTOCOL_VERSION in SUPPORTED_VERSIONS
