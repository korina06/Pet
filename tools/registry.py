"""工具注册表：Agent 只从这里拿 schema、按名字执行，不需要知道有哪些工具。

新增工具的成本因此只在「写一个类 + 装配时登记」两处。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from core.schemas import ToolResult
from tools.base import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 未定义 name")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool
        logger.debug("注册工具 %s", tool.name)
        return tool

    def replace(self, tool: BaseTool) -> BaseTool:
        """覆盖注册（用于注入共享 HTTP 客户端等依赖）。"""
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 未定义 name")
        self._tools[tool.name] = tool
        return tool

    def clone(self) -> "ToolRegistry":
        """复制一份注册表（工具实例共享，字典独立）。

        用途：单个请求需要给某个工具套上**本次才生效**的约束
        （比如右栏勾选），不能污染应用级的那个实例。
        """
        copy = ToolRegistry()
        copy._tools = dict(self._tools)
        return copy

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def menu_names(self) -> list[str]:
        """只返回作为**轮盘菜单项**的工具名（第 23 步起 /api/tools 用它区分入口）。

        与 names() 的区别很重要：
        - names()      = 全部已注册工具（模型的可用能力，也是前端全部入口的数据源）
        - menu_names() = 其中放进轮盘菜单的那些；**不在**这里的工具
          会作为对话面板里的"快捷意图"芯片出现，而不是没有入口。

        当前默认工具集（天气/周边）刻意都是 show_in_menu=False，
        所以菜单里只剩前端内置的「对话」。
        """
        return sorted(name for name, tool in self._tools.items() if tool.show_in_menu)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            # 模型可能幻觉出不存在的工具名，这也要能优雅处理
            return ToolResult(
                ok=False,
                error=f"不存在的工具：{name}。可用工具：{', '.join(self.names()) or '无'}",
                tool_name=name,
                arguments=arguments,
            )
        return await tool.safe_run(arguments)


#: 每个功能**能看见**哪些工具（第 85 步）。
#:
#: 为什么用"显式允许列表"而不是"从全量里删几个"：删法下，新增一个工具会
#: **默认泄漏**到所有功能（聊天、食谱、出行的模型都能看见它）；允许列表下，
#: 要进某个功能就必须在这里写一行 —— "谁能用什么"是可见、可审查的。
#:
#: 用户第 85 步的决定：聊天窗口只答"别的问题"，出行/菜谱各自固定在对应窗口里。
#: 所以聊天**一个工具都不给**（给了它就会去答那两类问题）。
CHAT_TOOLS: tuple[str, ...] = ()
TRAVEL_TOOLS: tuple[str, ...] = (
    "geocode_city",
    "get_weather",
    "find_nearby_places",
    # 第 223 步：路线规划（驾车/公交/步行/骑行）进出行聊天。
    # 之前它只被**填表流水线**用到（api/trip_routes.py），聊天里问「自驾还是高铁」
    # 模型根本查不了 —— 而"交通方案对比"是用户点名要的五项能力之一。
    "driving_route",
)
RECIPE_TOOLS: tuple[str, ...] = ("search_recipes", "get_weather", "geocode_city")

#: 配了 12306 的 MCP 地址之后，出行窗口**额外**能看见的工具（第 215 步）。
#: 单独列一张表而不是直接塞进 `TRAVEL_TOOLS`：后者是"没有外部依赖也必须存在"的
#: 底线能力，`build_feature_registry` 少一个就报错；而车次依赖别人家的服务，
#: 没配就不能要求它存在。
TRAVEL_OPTIONAL_TOOLS: tuple[str, ...] = ("search_trains",)


def feature_registry_for(app_state: Any, attr: str, *, feature: str) -> Any:
    """取某个功能的 registry；缺失时回退到 `registry` 并**留一条 warning**。

    为什么保留回退：测试里常常只挂一个 `app.state.registry`（并在上面替换假工具），
    强行要求三套会让每个测试都得重复装配。

    为什么必须出声：生产里 `main.py` 三套都会挂上，所以永远走第一分支；
    回退分支只该在测试出现。若哪天有人把 `main.py` 的装配删了，线上会**静默**
    退化成"所有功能共用一个工具面"——那正是第 85 步修掉的东西。
    """
    picked = getattr(app_state, attr, None)
    if picked is not None:
        return picked
    logging.getLogger(__name__).warning(
        "%s 的 %s 缺失，回退到 registry（仅应出现在测试或旧装配里）", feature, attr
    )
    return getattr(app_state, "registry", None)


def build_feature_registry(
    all_tools: ToolRegistry,
    allowed: tuple[str, ...],
    *,
    feature: str,
    optional: tuple[str, ...] = (),
) -> ToolRegistry:
    """按允许列表挑出这个功能该有的工具。

    少一个**必需**工具就报错，不静默降级：静默少工具会变成"模型想调却调不到"，
    用户只看到回答变差，日志里也什么线索都没有。

    但 `optional` 里的名字允许缺席（第 215 步）：那类工具依赖外部服务（比如某个
    没配地址的 MCP 服务器），"没有它"本来就是合法状态。
    """
    picked = ToolRegistry()
    for name in allowed:
        tool = all_tools.get(name)
        if tool is None:
            if name in optional:
                continue
            raise ValueError(
                f"{feature} 需要的工具不存在：{name}（检查 build_default_registry）"
            )
        picked.register(tool)
    return picked


def build_default_registry(
    http_client: Any = None,
    storage: Any = None,
    embedder: Any = None,
    judge: Any = None,
    train_url: str = "",
    train_timeout: float = 20.0,
    train_token: str = "",
    remote_tools: Sequence[Any] = (),
    route_tool: Any = None,
) -> ToolRegistry:
    """组装本项目默认的工具集。

    新增工具时只改这里，Agent 和 API 无需改动。
    http_client 用于把应用级共享的 httpx.AsyncClient 注入给需要联网的工具，
    这样连接池可以复用，单测也能注入 MockTransport。
    storage 是第 50 步加的参数：菜谱检索工具要读 SQLite 里的本地菜谱库。
    **打开库要排在组装注册表之前**（main.py 里已按这个顺序），否则
    工具拿不到库，只能回一句"菜谱库还没准备好"。
    """
    from tools.geocode_tool import GeocodeTool
    from tools.poi_tool import PoiTool
    from tools.recipe_search_tool import RecipeSearchTool
    from tools.weather_tool import WeatherTool

    registry = ToolRegistry()
    registry.register(WeatherTool(client=http_client))
    registry.register(PoiTool(client=http_client))
    # 地名 → 坐标：天气与周边都只吃经纬度，出行表单里填的却是城市名（第 25 步）
    registry.register(GeocodeTool(client=http_client))
    # 路线规划（第 223 步）：驾车/公交/步行/骑行。**与填表流水线共用同一个实例**，
    # 这样路线缓存只有一份（同一个 OD 对不会因为两条入口各查一次而多花额度）。
    if route_tool is None:
        from tools.route_tool import RouteTool

        route_tool = RouteTool(client=http_client)
    registry.register(route_tool)
    # 本地菜谱库检索（第 50 步）：把"今天吃什么"变成"先查库再回答"。
    # 它 show_in_menu=False——模型自己在需要时用，不是手动入口。
    # embedder / judge 缺任何一个都只走字面检索（向量路需要"召回 + 裁决"两件套）
    registry.register(
        RecipeSearchTool(storage=storage, embedder=embedder, judge=judge)
    )
    # 12306 车次（第 215 步）：**只有配了 MCP 地址才注册**。
    # 没配就是不注册，而不是"注册一个每次都说没配的工具"——
    # 后者会白占模型的一个工具位，还诱导它去调一个必然失败的工具。
    if str(train_url or "").strip():
        from tools.train_tool import TrainTicketTool

        registry.register(
            TrainTicketTool(url=train_url, timeout=train_timeout, token=train_token)
        )
    # 其余外部 MCP 服务器的工具（第 217 步）：已经由 `tools/mcp_catalog.py` 按
    # 配置和缓存造好，这里只负责登记。**不在这里做网络发现**——启动是同步的，
    # 而"问远端有哪些工具"要发请求；拆成 probe 脚本 + 缓存，启动就永远不慢。
    for tool in remote_tools:
        registry.register(tool)
    return registry
