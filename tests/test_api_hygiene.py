"""守住「恶意网页改不动本机数据」这条防线：变异端点必须收 JSON 体、Host 必须是本机，
以及一份需要人来签字确认的「无请求体也可以触发」接口清单。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 不读任何输入的变异端点。加进这个集合前请回答：跨站能不能直接触发它？触发了会怎样？
#: - DELETE /api/sessions/{id}、DELETE /api/recipe/library/{id}
#:   → DELETE 不是简单请求，浏览器必发预检 → 打不进来；路径里的 id 也无从伪造。
#: - POST /api/model/probe → 不读输入，效果是"探一次模型可不可用"（可能计费，但很便宜）。
#: - POST /api/recipe/library/seed → 不读输入，而且它自己拒绝覆盖已有数据。
BODYLESS_MUTATIONS = frozenset(
    {
        ("DELETE", "/api/sessions/{session_id}"),
        ("DELETE", "/api/recipe/library/{recipe_id}"),
        ("POST", "/api/model/probe"),
        ("POST", "/api/recipe/library/seed"),
    }
)

BODYLESS_PATHS = frozenset(path for _, path in BODYLESS_MUTATIONS)


def _main_module():
    """按路径加载真的 `main.py`（runner 里 `main` 是只有 STATIC_DIR 的假模块）。"""
    spec = importlib.util.spec_from_file_location(
        "pet_desktop_main_for_hygiene", PROJECT_ROOT / "main.py"
    )
    assert spec is not None and spec.loader is not None, "找不到 main.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _routes():
    return _main_module().app.routes


def _mutating_routes():
    for route in _routes():
        methods = getattr(route, "methods", None) or set()
        hit = methods & MUTATING
        if not hit:
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:                     # StaticFiles 之类的挂载点
            continue
        yield route, dependant, hit


def test_no_mutating_endpoint_takes_query_parameters():
    """变异端点不许从 query 里拿参数 —— 那是跨站能直接构造出来的唯一输入位。"""
    offenders = [
        f"{'/'.join(sorted(hit))} {route.path} → query 参数 {param.name}"
        for route, dependant, hit in _mutating_routes()
        for param in dependant.query_params
    ]
    assert not offenders, (
        "这些变异端点用 query 参数收参数，跨站简单请求（form/plain）不用预检就能打到它们：\n  "
        + "\n  ".join(offenders)
        + "\n改成 Pydantic 请求体（`payload: SomeRequest`）：浏览器就会先发预检，而本服务不给 CORS 头。"
    )


def test_no_endpoint_accepts_form_or_upload_bodies():
    """任何端点都不许用 Form / File 收 body：multipart 是简单请求的 content-type。"""
    from fastapi.params import File, Form

    offenders = []
    for route in _routes():
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for param in dependant.body_params:
            info = getattr(param, "field_info", None)
            if isinstance(info, (Form, File)):
                methods = "/".join(sorted(getattr(route, "methods", None) or {"?"}))
                offenders.append(f"{methods} {route.path} → {param.name}")
    assert not offenders, (
        "这些端点用 Form/File 收参数 —— multipart 属于「简单请求」，跨站页面可以直接提交、"
        "不需要预检，JSON-only 这道防线就绕过去了：\n  " + "\n  ".join(offenders)
    )


def test_post_endpoints_read_their_input_from_a_json_body():
    """POST 若有输入，输入必须来自 JSON 体（路径参数除外）。"""
    offenders = [
        route.path
        for route, dependant, hit in _mutating_routes()
        if "POST" in hit
        and not dependant.body_params
        and not dependant.path_params
        and route.path not in BODYLESS_PATHS
    ]
    assert not offenders, (
        "这些 POST 没有任何输入参数，跨站简单请求可以不打预检直接触发：\n  "
        + "\n  ".join(offenders)
        + "\n如果确实无害，把它们登记进 BODYLESS_MUTATIONS 并写清理由。"
    )


def test_bodyless_mutation_endpoints_are_a_reviewed_allowlist():
    """无体变异端点必须逐条签字，新增一条就得重新想一遍跨站影响。"""
    actual = {
        (method, route.path)
        for route, dependant, hit in _mutating_routes()
        if not dependant.body_params
        for method in hit
    }
    unexpected = actual - BODYLESS_MUTATIONS
    assert not unexpected, (
        "出现了没登记过的「不读输入就能变异」的端点：\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(unexpected))
        + "\n跨站简单请求可以不打预检直接打它。确认无害后加进 BODYLESS_MUTATIONS，"
        "并写上「为什么跨站触发了也没关系」。"
    )


# ---------------------------------------------------------------- Host 与测试夹具的一致性
def test_test_clients_that_hit_the_real_app_use_a_whitelisted_host():
    """任何**直接打 main.app** 的测试客户端，Host 必须在白名单里（第 202 步 / 评价16）。

    这条红过：`tests/test_app_assembly.py` 的夹具用了 `base_url="http://test"`，
    而第 192 步的 Host 白名单只放行回环地址 + `testserver` → 该文件 5 条全 403。
    更麻烦的是它**只在 pytest 下红**：项目自己的 runner 用的是桩 `main`（没有中间件），
    所以 `check_frontend.py` 全绿、提交信息里写「全量通过」，而 pytest 那边安静地烂着。

    断言方式：扫所有 tests/*.py，凡是既 `import main`/`from main import ...`
    又建了 `AsyncClient`/`TestClient` 的文件，其 `base_url` 的 host 必须 ∈ 白名单。
    """
    # 必须按路径加载真的 main.py：runner 里 `main` 是只有 STATIC_DIR 的桩模块。
    allowed = _main_module().DEFAULT_ALLOWED_HOSTS

    assert "testserver" in allowed, "白名单得留 testserver 给 TestClient 用"

    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
        if path.name == Path(__file__).name:  # 本文件里的示例文本不该被自己判红
            continue
        source = path.read_text(encoding="utf-8")
        if "from main import" not in source and "import main" not in source:
            continue
        if "AsyncClient(" not in source and "TestClient(" not in source:
            continue
        for match in re.finditer(r'base_url\s*=\s*["\']http://([^"\'/:]+)', source):
            host = match.group(1).lower()
            if host not in allowed:
                offenders.append(f"{path.name}: base_url 的 host={host!r}")

    assert not offenders, (
        "这些测试客户端会被 Host 白名单 403（改夹具，别改白名单）：\n  "
        + "\n  ".join(offenders)
    )
