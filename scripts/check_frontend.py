"""不装 pytest / fastapi 也能跑"不依赖第三方库"的那几组测试。

直接 import 纯测试模块；对同一个文件里混着 ASGI 接线与前端结构的模块，只抽出前端那一节执行
（抽取靠源码里的分节注释，不复制断言）。第三方库只给替身、不打桩，靠打桩变绿是假绿。
"""

from __future__ import annotations

import ast
import asyncio
import functools
import importlib
import importlib.util
import inspect
import os
import re
import shutil
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
STATIC_DIR = ROOT / "static"

# 直接 import 即可的模块：只读 static/ 下的文件（行为验证另外起 Node 子进程），
# 或者只用标准库（test_mailbox 的 IMAP 客户端是注入的假对象，
# test_mail_imap_wire 用 socket 起假服务器走真 imaplib）。
MODULES = (
    "test_frontend_wiring",
    # 打印不许把全绿变成 exit 1（第 214 步）：只依赖标准库
    "test_console_output",
    # 聊天历史恢复的行为验证（第 108 步）：共享 DOM 桩 + 可控延迟 fetch
    "test_chat_history_ui",
    # 聊天流式的行为验证（第 117 步）：假 ReadableStream 喂 SSE
    "test_chat_stream_ui",
    # 功能栏「›」折叠的行为验证（第 146 步）：缺 Node 时自己跳过
    "test_chat_tools_ui",
    # 桌宠动作菜单的行为验证（第 150 步）：缺 Node 时自己跳过
    "test_pet_actions_ui",
    # 邮件到货提醒的行为验证（第 198 步）：气泡 → 点开 → 定位那封
    "test_mail_notify_ui",
    # 待机动画的行为验证（第 151 步）：缺 Node 时自己跳过
    "test_pet_idle_ui",
    # 出行质量（第 125 步）：聚类 / 餐次 / 到达时间
    "test_trip_quality",
    "test_mail_ui",
    "test_clamp_math",
    "test_mailbox",
    # 用真 imaplib 走真协议（socket + threading 起假服务器），只用标准库
    "test_mail_imap_wire",
    # 食谱的纯逻辑层（第 37 步）：core/recipes.py 不 import httpx/fastapi，
    # 所以这组在这里能真跑——包括用假 HTTP 客户端测各工具的请求形状。
    "test_recipes",
    # 食谱窗口的结构与行为（第 37 步）：结构读 HTML/CSS，行为交给 verify_recipe.js
    "test_recipe_ui",
    # 签到的纯逻辑层（第 40 步）：core/checkin.py 只依赖标准库，
    # 所以"三种状态 / 日历补格 / 连续天数 / 日期解析"在这里能真跑。
    "test_checkin",
    # 签到窗口的结构与行为（第 40 步）：结构读 HTML/CSS，行为交给 verify_checkin.js
    "test_checkin_ui",
    # 农历 / 节日 / 放假调休的数据锚点（第 146 步）：纯标准库，全部离线
    "test_calendar_data",
    # Lint 配置与 CI 接线（第 121 步）：纯标准库，本机装了 ruff 会顺手真跑
    "test_lint",
    # runner 自身两份清单的格式（第 145 步）：连着踩过三次
    "test_runner_manifest",
    # 仓库卫生（第 121 步补）：用户的未跟踪文件不许被 add 进去
    "test_repo_hygiene",
    # 用量 CLI 的回归（第 123 步补）：第 114 步起它一直是崩的
    "test_usage_cli",
    # 出行的领域模型（第 41 步）：core/itinerary.py 只用标准库，
    # 所以"时长映射 / 日期推算 / 可行性校验 / 缺数据返回 None"这些能在这里真跑。
    "test_itinerary",
    # 出行流水线（第 41 步）：工具是注入的，所以整条流水线能完全离线跑，
    # 包括"拿不到坐标就不产出任何行程""驾车查不到必须标成估算"这些失败语义。
    "test_trip_planner",
    # 出行对话（第 216 步）：提示词约束、事后核对（编造名字 / 简称不算编造）、
    # 以及"模型挂了也要说人话"这类失败语义，都能用假模型 + 注入工具离线跑。
    "test_trip_chat",
    # 出行「聊聊」页的结构与行为（第 216 步）：默认页、隐藏的表单页、
    # 以及 scripts/verify_travel_chat.js 那九个场景
    "test_travel_chat_ui",
    # 鉴权前端（第 254 步）：源码里所有 /api 调用走同一个包装；行为交给
    # scripts/verify_auth_ui.js（缺 Node 时只跑结构断言）
    "test_auth_ui",
    # 行程地图的接线（第 224 步）：卡片默认隐藏、高德脚本点开才加载、
    # 概览渲染两次不会把已打开的地图收起来，行为交给 scripts/verify_travel_map.js
    "test_travel_map_ui",
    # 路由遮挡（第 229 步）：任何字面路径被前面的参数路由完整命中，这条接口就永远
    # 走不到（地图配置接口就这样"消失"过一次）。用 Starlette 自己的 matches 判，
    # 所以不用起服务器；另有一条真发请求的检查。
    "test_route_shadowing",
    # 行程存储（第 41 步）：淘汰语义属于业务规则，不该只有起了 ASGI 才能验。
    "test_trip_store",
    # 存储基建（第 47 步）：core/storage.py 只用标准库，
    # 所以"迁移/事务/降级/WAL/FTS5 探测/并发写"这些能在这里真跑——
    # 含真临时库文件（tmp_path fixture，本脚本第 47 步补上）。
    "test_storage",
    # 坐标转换（第 65 步的修复就靠它守）：纯标准库，直接跑
    "test_coords",
    # 向量索引三方法接口（第 253 步）：纯标准库（内存库 + 余弦）
    "test_vector_index",
    # 评测集自身的完整性（第 75 步）：holdout 与 dev 是否真分开、
    # P@5 的分母有没有写错。纯标准库（只读 json 与源码）。
    "test_eval_golden",
    # CI 配置自身的回归保护（第 80 步）：只读 YAML，纯标准库
    "test_ci_workflow",
    # 容器化产物（第 195 步）：只读 Dockerfile / .dockerignore，纯标准库
    "test_dockerfile",
    # 备份脚本与运维三件套（第 88 步）：纯标准库（sqlite3/zipfile）
    "test_backup_and_ops",
    # 对话消息落库（第 91 步 / A3）：纯标准库（sqlite3）
    "test_history",
    # 用量账本（第 111 步）：纯标准库
    "test_usage_ledger",
    # 本地菜谱库（第 47 步）：core/recipe_library.py 只用标准库，
    # CRUD/去重/别名展开/FTS 与 LIKE 双路径全部真跑。
    "test_recipe_library",
    # 内置种子菜谱（第 48 步）：core/recipe_seed.py 只用标准库，
    # 所以"数据质量（用量/步骤/标签词表）+ 导入幂等 + 不覆盖用户菜"在这里真跑。
    "test_recipe_seed",
    # 菜谱检索工具的**逻辑**（第 50 步）：tools/recipe_search_tool.py 只依赖
    # 标准库 + core.recipe_library，所以"只给 id / 查不到要明说 / 忌口展开过滤"
    # 在这里真跑。**注册表装配**那几条需要 httpx，放在 OPTIONAL 的 test_tool_registry。
    "test_recipe_search_tool",
    # markdown 解析器（第 50 步 50f）：把用户给的《中国菜谱大全》变成种子数据。
    # 纯标准库，所以能在这里真跑——它错了不会报错，只会灌一堆残缺菜谱进库。
    "test_recipe_md",
    # 检索质量 golden set（第 54 步 P0-1）：把"变好了"从主观判断变成数字。
    # 纯标准库（内存库 + 种子），跑一遍约 1 秒。
    "test_retrieval_quality",
    # 菜谱图片库（第 58 步）：独立只读库 + 按菜名关联。纯标准库，能真跑。
    "test_recipe_images",
    # 上传图片识菜（第 60 步）：校验 + 名称解析 + 流程编排（假客户端，不联网）。
    "test_recipe_photo",
)

#: 需要第三方库的模块：**本环境真装得上就跑，装不上就明确跳过**。
#: 这里绝不给这些库打桩——打桩跑出来的绿是假绿（见文件头的说明）；
#: 要么真装上真跑，要么明说"这次没跑"。
#:
#: 判据是"这些库能不能 import"，不是"我在哪个解释器下"：
#: 项目 .venv 里没有 fastapi 时，用另一个装了 fastapi 的解释器跑这个脚本，
#: 这几组就会真的执行；否则会打印一行"跳过"并计入跳过数（**不算通过**）。
OPTIONAL_MODULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # Agent 主干（第 2 步起）：这一整份以前**没进任何清单**，等于从没在这条路径上跑过。
    # 它 import 了 tools.registry → httpx，所以归这一组。
    (("httpx",), "test_agent"),
    # 三个联网工具的测试（第 67 步发现：它们也一直没进清单）。
    # 只依赖 httpx（MockTransport），不需要 fastapi。
    (("httpx",), "test_route_tool"),
    # 按功能装配工具面（第 85 步）：谁能看见什么必须是显式的
    (("httpx",), "test_registry_split"),
    # 注意：test_deepseek_llm **没接**。它的 17 条全部依赖一个 `func` fixture
    # （本脚本不支持，会诚实地报"跳过"），接进来只会得到 17 条永久跳过。
    # 它由 pytest 那条路覆盖 —— CI 两个都跑（见 .github/workflows/checks.yml）。
    (("httpx",), "test_poi_tool"),
    (("httpx",), "test_geocode_tool"),
    # 配置层（第 70 步接）：它 import core.config -> pydantic_settings。
    # 顺带把此前一直没被收集的配置测试也跑起来。
    (("pydantic_settings",), "test_config"),
    # 天气工具（第 236 步接入）：实况/逐日、end_date 口径、缓存与失败语义。
    # 以前以为它依赖 `func` fixture 而没接，实际上现在全是 MockTransport，离线可跑。
    (("httpx", "pydantic_settings"), "test_weather_tool"),
    (("fastapi", "httpx", "pydantic"), "test_mail_api"),
    # 最小可交付性（第 251 步）：Bearer、会话归属、429、Origin 校验
    (("fastapi", "httpx", "pydantic"), "test_auth_ownership"),
    (("fastapi", "httpx", "pydantic"), "test_recipe_api"),
    # 聊天接口（第 93 步接）：历史恢复、limit 夹取、重启后仍能拿回
    (("fastapi", "httpx", "pydantic"), "test_api"),
    # token 用量记录（第 96 步）：只记录不拦截
    (("httpx", "pydantic"), "test_llm_usage"),
    # 模型切换（第 98–99 步）：提供方目录、路由转发、接口
    (("fastapi", "httpx", "pydantic"), "test_model_switch"),
    # 用量账本接口（第 111 步）
    (("fastapi", "httpx", "pydantic"), "test_usage_api"),
    # 流式取消语义（第 116 步）
    (("fastapi", "httpx", "pydantic"), "test_stream_cancel"),
    # 聊天流式接口（第 117 步）
    (("fastapi", "httpx", "pydantic"), "test_chat_stream"),
    # Agent 场景评测（第 118 步）：脚本化模型，30 秒内跑完
    (("fastapi", "httpx", "pydantic"), "test_eval_agent"),
    # request_id 与本地诊断（第 119 步）
    (("fastapi", "httpx", "pydantic"), "test_diagnostics"),
    # 真浏览器烟测的服务端一半（第 122 步）：要 fastapi/httpx
    (("fastapi", "httpx", "pydantic"), "test_smoke_browser_app"),
    # 提示词版本化（第 123 步）：要 agents/fastapi
    (("fastapi", "httpx", "pydantic"), "test_prompt_version"),
    # 买菜清单（第 126 步）：合并但不做算术
    (("fastapi", "httpx", "pydantic"), "test_shopping_list"),
    # 模型调用的网络重试（第 127 步）：瞬时断网不该毁掉整轮
    (("fastapi", "httpx", "pydantic"), "test_llm_retry"),
    # 报错要带对的提供方名字（第 128 步）：切到 GLM 却报 DeepSeek
    (("fastapi", "httpx", "pydantic"), "test_provider_labels"),
    # 行程导出成文本（第 130 步）
    (("fastapi", "httpx", "pydantic"), "test_trip_text"),
    # 食谱新建会话（第 131 步）：清对话，不动忌口
    (("fastapi", "httpx", "pydantic"), "test_recipe_new_session"),
    # Prompt injection（第 133 步）：约束在工具边界、卡片只来自库
    (("fastapi", "httpx", "pydantic"), "test_prompt_injection"),
    # 负载测试脚本（第 134 步）：几秒跑完，只验报告可信
    (("fastapi", "httpx", "pydantic"), "test_load_script"),
    # 幂等键（第 135 步）：同 key 只干一次，流式不受影响
    (("fastapi", "httpx", "pydantic"), "test_idempotency"),
    # Agent 状态事件（第 136 步）：只观测、不进 SSE
    (("fastapi", "httpx", "pydantic"), "test_state_events"),
    # 真的跑一次 lifespan（第 144 步）：干净 HEAD 起不来的那类问题
    (("fastapi", "httpx", "pydantic"), "test_startup"),
    # .env.example 必须能被 dotenv 干净解析（第 145 步）：要 python-dotenv
    (("dotenv",), "test_env_example"),
    # 动作素材的元数据检查（第 166 步）：纯 json，无第三方依赖
    ((), "test_pet_assets"),
    # app.log 的 handler 去重与轮转容错（第 214 步）：要真 import main.py
    (("fastapi", "httpx", "pydantic"), "test_log_handler"),
    # 评价12 那批前端修复的定点断言（第 170 步）：只读源码，无依赖
    ((), "test_review12_fixes"),
    # 向量存储/融合（第 182 步）：纯函数 + sqlite，无第三方依赖
    ((), "test_recipe_vectors"),
    # 场景词表（第 184 步）：纯数据 + 工具，无第三方依赖
    ((), "test_scene_terms"),
    # 语义裁决者（第 188 步）：纯函数 + 假模型，无第三方依赖
    ((), "test_semantic_judge"),
    # holdout v3 守卫（第 189 步）：只读 JSON + 纯函数，无第三方依赖
    ((), "test_eval_holdout_v3"),
    # holdout v4 守卫（第 248 步）：30 条盲测集，同样只读 JSON
    ((), "test_eval_holdout_v4"),
    # 候选池边界（第 190 步）：内存库 + 工具，无第三方依赖
    ((), "test_recipe_search_pool"),
    # MCP server（第 205 步）：手写 JSON-RPC，只用标准库 + 本项目模块
    ((), "test_mcp_server"),
    # 外部 MCP 客户端（第 215 步）：`tools/mcp_remote.py` 会 import mcp SDK，归可选组
    (("mcp", "pydantic"), "test_mcp_remote"),
    # 外部 MCP 目录层（第 217 步）：配置解析 + 缓存 + 造工具，不联网、无 mcp SDK
    ((), "test_mcp_catalog"),
    # 黄历工具（第 221 步）：按区间查、一天压成 3 行；远端调用被伪造，不联网
    ((), "test_almanac_tool"),
    # 联网搜索（第 222 步）：10 条压 5 条、摘 uuid；远端调用被伪造，不联网
    ((), "test_web_search_tool"),
    # 性能文档守卫（第 206 步）：只读 markdown + 脚本源码，无依赖
    ((), "test_perf_doc"),
    # 接口卫生（第 192 步）：只读路由声明，JSON-only 那条不变量的守卫
    (("fastapi", "httpx", "pydantic"), "test_api_hygiene"),
    # 嵌入客户端（第 182 步）：用 httpx.MockTransport，归可选组
    (("httpx", "pydantic"), "test_embeddings"),
    # 动作目录接口（第 172 步）：要 fastapi + httpx，归可选组
    (("fastapi", "httpx", "pydantic"), "test_pet_actions_api"),
    # 评价11 那批修复的回归测试（第 156 步）：用到 fastapi，归可选组
    (("fastapi", "httpx", "pydantic"), "test_review11_fixes"),
    # 本地基准（第 120 步）：几秒跑完，只验报告可信
    (("fastapi", "httpx", "pydantic"), "test_bench_local"),
    # 对话式检索（第 50 步 50d）：钉住"卡片只来自库、模型只决定选哪几道"
    (("fastapi", "httpx", "pydantic"), "test_recipe_ask"),
    (("fastapi", "httpx", "pydantic"), "test_recipe_library_api"),
    # 注册表装配（第 50 步）：只需要 httpx（注册表会拉起三个联网工具），
    # 不需要 fastapi。判据照旧是"能不能 import"。
    (("httpx",), "test_tool_registry"),
    (("fastapi", "httpx", "pydantic"), "test_checkin_api"),
    # 出行接口（第 41 步）：钉住"输入不合规要 400""拿不到真数据要 failed
    # 而不是 200+假行程"这两条主线。
    (("fastapi", "httpx", "pydantic"), "test_trip_api"),
    # 景点配图接口（第 29 步起，第 44 步加多张候选）。它用的是注入的假 HTTP 客户端、
    # 完全离线，但 import 的是 api/routes.py（会拉起 fastapi/httpx/pydantic），所以归这一组。
    # ⚠️ 这一条以前**漏了**：test_photo_api.py 一直没进这份清单，
    # 于是它在 check_frontend 这条路径上从来没跑过（只有装了 pytest 才跑得起来）。
    (("fastapi", "httpx", "pydantic"), "test_photo_api"),
)

# 需要"按节抽取"的模块：整份 import 会拉起 httpx/fastapi
# 每条 = (模块文件名, 起始注释前缀, 结束注释前缀)；结束为 None 表示到文件末尾
SOURCES: tuple[tuple[str, str, str | None], ...] = (
    ("test_app_assembly.py", "桌宠形象（第 6 步）", "侧边栏已删除（第 209 步"),
    ("test_app_assembly.py", "侧边栏已删除（第 209 步", "快捷胶囊（第 23 步起"),
    ("test_app_assembly.py", "快捷胶囊（第 23 步起", None),
)

# 这些虽然落在同一节里，但要靠 ASGI 应用或配置对象，属于"接线测试"，
# 本脚本的目标是前端结构与行为，所以跳过（它们在装了依赖的 pytest 里照常跑）。
NEEDS_BACKEND = ("test_api_router_registered", "test_settings_are_readable_without_crashing")


class _Raises:
    """`pytest.raises` 的真替身。

    它**真执行**被包裹的代码，并**真检查**异常类型——不是"遇到 with 就当通过"。
    `__exit__` 返回 False 时异常会原样抛出去，所以"抛错了类型"照样是失败。

    `match=` 也**真查**（`re.search(消息)`）：不支持它的话，像
    `pytest.raises(ValueError, match="重复")` 这种用例会以 TypeError 假失败
    —— 那会把"替身缺功能"伪装成"产品坏了"（第 66 步接 test_agent 时撞到）。
    """

    def __init__(self, expected, match: str | None = None):
        self.expected = expected
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"预期抛出 {self.expected!r}，但什么都没抛")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        if self.match is not None:
            message = str(exc)
            if re.search(self.match, message) is None:
                raise AssertionError(
                    f"异常消息不含 {self.match!r}，实际：{message!r}"
                )
        return True


def _mark_stub(module) -> None:
    """给替身模块打个记号。

    没有这个记号就没法区分"真的有这个库"和"本脚本装了个替身"——
    `importlib.util.find_spec()` 碰到替身会直接抛 `httpx.__spec__ is None`。
    """
    module._dsh_stub = True


def _stub_pytest() -> None:
    """给测试模块一个够用的 pytest 替身。

    补的是**测试运行机制**，不是被测行为：
      - `pytest.mark.skipif`：原样返回函数（跳不跳过由本脚本自己判断）
      - `pytest.raises`：真执行真断言（见上）
      - `pytest.mark.parametrize`：把参数记在函数上，由本脚本展开成多个用例
      - `pytest.fixture(autouse=True)`：**真跑**（`_setup_autouse` / `_teardown_autouse`），
        因为像"每个用例前后清一次缓存"这种事，不做就等于让用例互相污染
    除此之外不做任何伪造。
    """

    class _Mark:
        def skipif(self, condition=False, *, reason=""):
            """**真的**按条件跳过（第 104 步）。

            以前这里是空桩（"跳不跳过由本脚本自己判断"），于是
            `@pytest.mark.skipif(NODE is None)` 在没装 Node 的机器上照样执行，
            最后炸在 subprocess 里 —— CI 上就是这么红的。
            """

            def decorator(func):
                if not condition:
                    return func

                @functools.wraps(func)
                def skipped(*args, **kwargs):
                    raise _Skip(reason or "skipif 条件成立")

                return skipped

            return decorator

        def parametrize(self, argnames, argvalues, **kwargs):
            """记下参数，展开交给 `_cases()`。

            真的 pytest 会在这里生成多个用例；替身不做那么重的事，
            只把 (参数名, 取值) 挂到函数上，运行器读到就逐个跑。
            """
            names = (
                [n.strip() for n in argnames.split(",")]
                if isinstance(argnames, str)
                else list(argnames)
            )

            def decorator(func):
                specs = getattr(func, "_parametrize", None)
                if specs is None:
                    specs = []
                    func._parametrize = specs
                specs.append((names, list(argvalues)))
                return func

            return decorator

        def __getattr__(self, name):
            def decorator(*args, **kwargs):
                # 直接当装饰器用：`@pytest.mark.asyncio`（不带括号）。
                # 真 pytest 两种写法都支持；替身以前只认带括号的那种，
                # 结果 test_weather_tool 里所有用例都被换成了 wrapper 本身，
                # 到运行器那里表现为「需要 func fixture」而整组跳过（第 236 步修）。
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]

                def wrapper(func):
                    return func

                return wrapper

            return decorator

    module = type(sys)("pytest")
    module.mark = _Mark()

    def _pytest_skip(reason: str = "") -> None:
        """`pytest.skip(...)` 的真替身：抛 `_Skip`，由 `_run_module` 记成跳过。

        跳过 ≠ 通过。以前没实现它，需要 Node 的用例在缺 Node 的机器上只能
        要么硬跑（炸在 subprocess 里）、要么静默 return（假绿）。
        """
        raise _Skip(reason or "用例主动跳过")

    module.skip = _pytest_skip

    def fixture(*args, **kwargs):
        """`pytest.fixture` 的最小替身。

        autouse 的会被记下来、由运行器在**每个用例前后真跑**（见 `_setup_autouse`）。
        需要按参数名注入的 fixture（`monkeypatch` / `tmp_path`）由 FIXTURES 提供；
        没实现的会明确报"跳过"，绝不静默放过（否则用例会假装通过）。
        """
        autouse = bool(kwargs.get("autouse"))

        def decorate(func):
            if autouse:
                func._check_frontend_autouse = True
            return func

        # 也支持直接当装饰器用：@pytest.fixture
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return decorate(args[0])
        return decorate

    module.fixture = fixture
    module.raises = _Raises
    _mark_stub(module)
    sys.modules.setdefault("pytest", module)


def _cases(func) -> list[tuple[str, dict]]:
    """把测试函数展开成 ``[(标签, 关键字参数)]``。

    没有 `parametrize` 就是一个空参用例；有的话做笛卡尔积
    （测试里最多叠两层，够用了）。
    """
    specs = getattr(func, "_parametrize", None)
    if not specs:
        return [("", {})]

    out: list[tuple[str, dict]] = []

    def build(index: int, kwargs: dict) -> None:
        if index == len(specs):
            out.append((", ".join(f"{k}={v!r}" for k, v in kwargs.items()), dict(kwargs)))
            return
        names, values = specs[index]
        for value in values:
            merged = dict(kwargs)
            if len(names) == 1:
                merged[names[0]] = value
            else:
                merged.update(dict(zip(names, value)))
            # 值里有不可哈希的东西也不怕：这里只用 dict，不做集合运算
            build(index + 1, merged)

    build(0, {})
    return out


_MISSING = object()


class _MonkeyPatch:
    """够用的 `monkeypatch` 替身：**真改真还原**。

    实现了本仓库测试用到的三种形态：
      - `setattr(target, name, value)`（以及 pytest 也支持的
        `setattr("模块路径", name, value)` 字符串形式）；
      - `setenv(name, value[, prepend])` —— 改的是真环境变量；
      - `delenv(name[, raising])`。

    为什么 setenv/delenv 要单独走 `_saved_env`：`os.environ` 是映射，
    属性赋值（setattr）不会落到环境里，必须 `os.environ[name] = ...`，
    还原时也要按映射来还原。用完必须 undo——
    否则一个测试的改动会污染后面所有测试，比不支持还糟。
    """

    def __init__(self):
        self._saved: list[tuple[object, str, object, bool]] = []
        self._saved_env: list[tuple[str, str | None]] = []

    def setattr(self, target, name, value=_MISSING, raising=True):
        if value is _MISSING and isinstance(target, str):
            # pytest 的字符串形式：setattr("pkg.mod", "name", value)
            module_path, _, attr = target.rpartition(".")
            value = name
            name = attr
            target = importlib.import_module(module_path)

        existed = hasattr(target, name)
        self._saved.append((target, name, getattr(target, name, None), existed))
        setattr(target, name, value)

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        if prepend is not None and name in os.environ:
            value = str(value) + prepend + os.environ[name]
        self._saved_env.append((name, os.environ.get(name)))
        os.environ[name] = str(value)

    def delenv(self, name: str, raising: bool = True) -> None:
        if name in os.environ:
            self._saved_env.append((name, os.environ[name]))
            del os.environ[name]
        elif raising:
            raise AssertionError(f"环境变量 {name} 不存在，无法删除")

    def undo(self) -> None:
        for target, name, old, existed in reversed(self._saved):
            if existed:
                setattr(target, name, old)
            else:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
        for name, old in reversed(self._saved_env):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._saved.clear()
        self._saved_env.clear()


def _new_monkeypatch():
    patch = _MonkeyPatch()
    return patch, patch.undo


def _new_tmp_path():
    """`tmp_path` 的真替身：每个用例一个全新空目录，用完就删。

    刻意放项目内 `.testtmp/`（已被 .gitignore 覆盖），而不是系统临时目录——
    本机沙箱对系统临时目录的删除不友好（WinError 5，见 test_clamp_math.py 的注记）。
    删不掉时 `ignore_errors` 放过：残留目录是垃圾，不是用例失败。

    **目录必须自己 `mkdir`，不能用 `tempfile.mkdtemp`（第 47 步踩到的坑）**：
    本机沙箱下 mkdtemp 建出的目录拿不到可用的目录句柄——
    `os.listdir()` 就 `PermissionError: [WinError 5]`，于是
    `sqlite3.connect(dir/pet.db)` 报 `unable to open database file`，
    连 `shutil.rmtree` 都删不掉（目录会残留）。
    实测对照（同一台机器、同一个 .testtmp 父目录）：

        mkdtemp(dir=base)  → listdir FAIL / Storage FAIL / rmtree 后仍在
        base/uuid + mkdir  → listdir [] / Storage OK / rmtree 后已删除

    注意这不是被测代码的问题：`Storage` 走的是合法路径，
    坏的是"用 mkdtemp 造临时目录"这件事本身。
    """
    base = ROOT / ".testtmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"tmp{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path, lambda: shutil.rmtree(path, ignore_errors=True)


#: 本脚本支持的 fixture：名字 → 工厂。
#: 工厂返回 `(值, 清场函数 | None)`，清场函数在用例结束后**真跑**（逆序）。
#: **不认识的绝不静默放过**——那会让用例假装通过。
FIXTURES = {"monkeypatch": _new_monkeypatch, "tmp_path": _new_tmp_path}


def _invoke(func, kwargs: dict) -> None:
    """调用一个测试函数：补 fixture、跑 async、跑完清场。

    `pytest-asyncio` 的 auto 模式会让 async 测试自动跑，替身这边
    直接 `asyncio.run()` —— 语义一样：真等它跑完，异常照常往上抛。
    """
    provided = dict(kwargs)
    teardowns: list = []
    for name in inspect.signature(func).parameters:
        if name in provided:
            continue
        factory = FIXTURES.get(name)
        if factory is None:
            # 交给调用方报"跳过"，不要伪装成通过
            raise _UnsupportedFixture(name)
        value, teardown = factory()
        provided[name] = value
        if teardown is not None:
            teardowns.append(teardown)

    try:
        result = func(**provided)
        if inspect.iscoroutine(result):
            asyncio.run(result)
    finally:
        for teardown in reversed(teardowns):
            teardown()


class _Skip(Exception):
    """用例主动跳过（`pytest.skip(...)` / `skipif` 条件成立）。"""


class _UnsupportedFixture(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _run_module(module, label: str) -> tuple[int, list[tuple[str, str]], list[str]]:
    """跑一个模块里的全部 test_* 函数。

    返回 ``(通过数, 失败列表, 跳过列表)``。**跳过不是通过**：
    需要本脚本不支持的 fixture 的用例会被明确列出来。
    """
    tests = [
        (attr, getattr(module, attr))
        for attr in sorted(dir(module))
        if attr.startswith("test_") and callable(getattr(module, attr))
    ]
    passed = 0
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    cases = sum(len(_cases(func)) for _, func in tests)
    _safe_print(f"\n=== {label}（{len(tests)} 个函数 / {cases} 个用例）===")

    for attr, func in tests:
        for tag, kwargs in _cases(func):
            name = f"{attr}[{tag}]" if tag else attr
            teardowns = _setup_autouse(module)
            try:
                _invoke(func, kwargs)
            except _Skip as exc:
                skipped.append(f"{label}::{name}（{exc}）")
                _safe_print(f"  skip {name}（{exc}）")
            except _UnsupportedFixture as exc:
                skipped.append(f"{label}::{name}（需要 {exc.name} fixture）")
                _safe_print(f"  skip {name}（需要 {exc.name} fixture，本脚本没实现）")
            except AssertionError as exc:
                failures.append((f"{label}::{name}", str(exc) or "断言失败（无描述）"))
                _safe_print(f"  FAIL {name}\n       {exc}")
            except Exception as exc:  # noqa: BLE001
                failures.append((f"{label}::{name}", f"{type(exc).__name__}: {exc}"))
                _safe_print(f"  ERROR {name}\n       {traceback.format_exc(limit=3)}")
            else:
                passed += 1
                _safe_print(f"  ok   {name}")
            finally:
                _teardown_autouse(teardowns)
    return passed, failures, skipped


def _autouse_fixtures(module) -> list:
    """模块里标了 autouse 的 fixture 函数（顺序按定义先后）。"""
    found = [
        (name, value)
        for name, value in vars(module).items()
        if callable(value) and getattr(value, "_check_frontend_autouse", False)
    ]
    return [value for _, value in sorted(found, key=lambda item: item[1].__code__.co_firstlineno)]


def _setup_autouse(module) -> list:
    """跑 autouse fixture 的 setup 段：generator 跑到 yield 为止。

    这是**真执行**，不是打桩——fixture 里的清缓存等动作照样发生，
    否则"每个用例之间互不影响"就成了假设而不是事实。
    """
    generators = []
    for fixture_func in _autouse_fixtures(module):
        result = fixture_func()
        if inspect.isgenerator(result):
            next(result)          # 跑到 yield = setup 完成
            generators.append(result)
    return generators


def _teardown_autouse(generators: list) -> None:
    for generator in reversed(generators):
        try:
            next(generator)
        except StopIteration:
            pass


def _missing_libs(names: tuple[str, ...]) -> list[str]:
    """哪些库**真的**不在。

    判据不是"能不能 import"这么简单：
      - 本脚本自己装过替身（打 `_dsh_stub`），那不算有；
      - 替身没有 `__spec__`，直接 `find_spec()` 会抛 `ValueError`。
    所以先看 sys.modules 里的记号，再问 find_spec，异常一律当"没有"。
    """
    missing = []
    for name in names:
        module = sys.modules.get(name)
        if module is not None and getattr(module, "_dsh_stub", False):
            missing.append(name)
            continue
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _stub_main() -> None:
    """测试文件从 main 里取 STATIC_DIR；不 import 真 main（那会拉起 fastapi）。"""
    module = type(sys)("main")
    module.STATIC_DIR = STATIC_DIR
    _mark_stub(module)
    sys.modules.setdefault("main", module)


def _stub_httpx() -> None:
    """只补齐"异常类型"级的 httpx 替身（备用）。

    个别测试用假客户端测逻辑却需要 httpx 的异常类才能 import。
    这里只提供类型，**不伪造任何 HTTP 行为**。

    **真的 httpx 在时不装替身。** 否则 `test_mail_api` 里的
    `import httpx` + `httpx.ASGITransport` 会拿到这个只有几个常量名的替身，
    测出来的是一堆莫名其妙的 AttributeError——那比"跳过"糟得多。
    """
    try:
        if importlib.util.find_spec("httpx") is not None:
            return
    except (ImportError, ValueError):
        pass

    class _HTTPError(Exception):
        pass

    class HTTPStatusError(_HTTPError):
        def __init__(self, message="", request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    module = type(sys)("httpx")
    module.HTTPError = _HTTPError
    module.HTTPStatusError = HTTPStatusError
    module.ConnectError = type("ConnectError", (_HTTPError,), {})
    module.AsyncClient = object          # 只做类型占位，不会被真正实例化
    module.Timeout = object
    module.MockTransport = object
    module.Response = object
    _mark_stub(module)
    sys.modules.setdefault("httpx", module)


def _section_source(source: str, start_marker: str, end_marker: str | None) -> str:
    """截出分节注释之间的源码。

    起始标记是注释里的片段，所以要从它所在**行的行首**开始截，
    否则会截出 `---- 侧边栏` 这样的半个注释，编译直接报错。
    """
    start = source.find(start_marker)
    if start < 0:
        raise ValueError(f"找不到分节注释：{start_marker}")
    line_start = source.rfind("\n", 0, start) + 1

    end = source.find(end_marker, start + len(start_marker)) if end_marker else -1
    if end > 0:
        end = source.rfind("\n", 0, end) + 1      # 结束标记同样取整行
    return source[line_start:end if end > 0 else len(source)]


def _functions_in(section: str) -> list[str]:
    """列出这一节里的顶层**测试**函数名。

    只保留名为 test_ 且不需要参数（fixture/参数化）的函数；
    这样子节末尾的 `_rule()` / `_visible_text()` 这类工具不会被当成测试去调用。
    """
    tree = ast.parse(section)
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        has_required_args = node.args.args or node.args.posonlyargs or node.args.kwonlyargs
        if not has_required_args:
            names.append(node.name)
    return names


def _module_helpers(source: str) -> tuple[str, list[str]]:
    """抽出文件级的辅助函数与类，以及"安全"的 import。

    被抽取的测试函数会用到文件里的 `_index_html()` / `_rule()` / `_TextCollector`
    这类工具，所以它们必须一起带过来；否则测出来的就是"名字找不到"而不是真实结论。
    这里刻意只搬运**不依赖第三方库**的东西（用名字下划线开头判断），
    以免把 httpx / fastapi 那套又拉进来。
    """
    tree = ast.parse(source)
    lines = source.splitlines()

    chunks: list[str] = []
    names: list[str] = []

    def take(node) -> None:
        chunks.append("\n".join(lines[node.lineno - 1:node.end_lineno]))
        names.append(node.name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name.startswith("_"):
            take(node)
        elif isinstance(node, ast.ImportFrom) and node.module in ("html.parser", "pathlib"):
            chunks.append("\n".join(lines[node.lineno - 1:node.end_lineno]))
        elif isinstance(node, ast.Import):
            if all(alias.name in ("re", "sys", "json") for alias in node.names):
                chunks.append("\n".join(lines[node.lineno - 1:node.end_lineno]))

    return "\n\n".join(chunks), names


def _run_source_group(
    filename: str, start_marker: str, end_marker: str | None
) -> tuple[int, list[tuple[str, str]]]:
    """按分节抽取并执行；返回 (通过数, 失败列表)。"""
    path = TESTS_DIR / filename
    source = path.read_text(encoding="utf-8")

    try:
        block = _section_source(source, start_marker, end_marker)
        names = _functions_in(block)
        helpers, helper_names = _module_helpers(source)
    except (ValueError, SyntaxError) as exc:
        return 0, [(f"{filename} 抽节失败", str(exc))]

    # 只编译"这一节 + 文件的辅助工具"：文件里需要第三方库的部分一概不碰
    namespace: dict[str, object] = {
        "__name__": f"{filename}::{start_marker}",
        "re": re,
        "STATIC_DIR": STATIC_DIR,
        "Path": Path,
    }
    try:
        exec(compile(helpers, str(path), "exec"), namespace)
        exec(compile(block, str(path), "exec"), namespace)
    except Exception as exc:
        return 0, [(f"{filename} 抽节执行失败", f"{type(exc).__name__}: {exc}")]

    passed = 0
    failures: list[tuple[str, str]] = []
    skipped = 0
    label = f"{filename} · {start_marker}"
    _safe_print(f"\n=== {label}（{len(names)} 条）===")
    for name in names:
        if name in NEEDS_BACKEND:
            skipped += 1
            _safe_print(f"  skip {name}（需要 ASGI 应用，交给装了依赖的 pytest）")
            continue
        func = namespace.get(name)
        if not callable(func):
            failures.append((f"{label}::{name}", "抽取后找不到函数"))
            continue
        try:
            func()
        except AssertionError as exc:
            failures.append((f"{label}::{name}", str(exc)))
            _safe_print(f"  FAIL {name}\n       {exc}")
        except Exception as exc:
            failures.append((f"{label}::{name}", f"{type(exc).__name__}: {exc}"))
            _safe_print(f"  ERROR {name}\n       {traceback.format_exc(limit=3)}")
        else:
            passed += 1
            _safe_print(f"  ok   {name}")
    return passed, failures



def _safe_print(*args: object, **kwargs: object) -> None:
    """打印一行，撞上控制台编码问题也不崩（第 214 步）。

    为什么不直接 import：`core` 这个包要等 `main()` 里把项目根塞进
    `sys.path` 之后才导入得到（`sys.path[0]` 是 `scripts/`）。
    所以这里惰性导入，只导一次。
    """
    from core.console import safe_print

    safe_print(*args, **kwargs)


def main() -> int:
    # 测试模块要 import core.mailbox / core.xxx 这类项目内模块。
    # 直接跑 `python scripts/check_frontend.py` 时 sys.path[0] 是 scripts/，
    # 项目根不在路径上，所以这里显式补进去。
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TESTS_DIR))

    # 第 103 步：GitHub 的 Windows runner 控制台是 cp1252，一打印「（」就
    # UnicodeEncodeError。脚本自己声明 UTF-8，别指望调用方的控制台能编码中文。
    from core.console import force_utf8

    force_utf8()
    _stub_pytest()
    _stub_main()
    _stub_httpx()

    passed = 0
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []

    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # 连 import 都失败：不是环境问题就是语法错误
            failures.append((f"{name} (import)", f"{type(exc).__name__}: {exc}"))
            _safe_print(f"[跳过] {name}：{type(exc).__name__}: {exc}")
            continue
        module_passed, module_failures, module_skipped = _run_module(module, name)
        passed += module_passed
        failures.extend(module_failures)
        skipped.extend(module_skipped)

    # 需要第三方库的模块：装得上就真跑，装不上就明确跳过（**不计入通过**）
    for libs, name in OPTIONAL_MODULES:
        missing = _missing_libs(libs)
        if missing:
            label = f"{name}（缺 {'、'.join(missing)}）"
            skipped.append(label)
            _safe_print(f"\n[跳过] {name}：本解释器没有 {'、'.join(missing)}，这组**没有跑过**")
            continue
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failures.append((f"{name} (import)", f"{type(exc).__name__}: {exc}"))
            _safe_print(f"\n[失败] {name} 导入不了：{type(exc).__name__}: {exc}")
            continue
        module_passed, module_failures, module_skipped = _run_module(
            # 标签不再写死"真 ASGI"：这一组里还有只依赖 httpx 的（如 test_tool_registry），
        # 叫"真 ASGI"会误导人以为它在测接口。
        module, f"{name}（依赖库已装上，真跑）"
        )
        passed += module_passed
        failures.extend(module_failures)
        skipped.extend(module_skipped)

    for filename, start_marker, end_marker in SOURCES:
        group_passed, group_failures = _run_source_group(filename, start_marker, end_marker)
        passed += group_passed
        failures.extend(group_failures)

    _safe_print(f"\n通过 {passed} 条，失败 {len(failures)} 条，跳过 {len(skipped)} 组")
    for name in skipped:
        _safe_print(f"  ~ 跳过 {name}")
    for name, detail in failures:
        _safe_print(f"  - {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
