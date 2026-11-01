"""真浏览器烟测：用 Chrome DevTools Protocol（`websockets`，不引新依赖）驱动本机浏览器，
验证流式聊天分次更新、食谱卡片真的画出来、出行失败不渲染假行程。

服务端跑替身组合，所以不花钱、不联网。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.console import force_utf8  # noqa: E402

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


# --------------------------------------------------------------- 假模型


class _ScriptedLLM:
    """按"这次给没给 search_recipes"分流：

    - 给了 → 先要一次工具调用（食谱链路），再给最终回答；
    - 没给 → 直接给聊天回答（分两段文本，便于验证增量更新）。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None):
        from core.schemas import Message, ToolCall

        self.calls += 1
        # 分流依据是"这次有没有工具"：**聊天窗口的工具集是空的**（第 85 步的产品决定），
        # 而食谱窗口有一组。比认工具名稳 —— 约束代理会把检索工具包一层，名字会变。
        has_tools = bool(tools)
        if has_tools and self.calls % 2 == 1:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="search_recipes",
                        arguments={"keyword": "鱼"},
                        raw_arguments='{"keyword": "鱼"}',
                    )
                ],
            )
        return Message(role="assistant", content="查好了，挑一道吧")

    async def complete_stream(self, messages, tools=None):
        from agents.llm import StreamEvent
        from core.schemas import Message, ToolCall

        # 注意：Agent 的 run() 内部是走 run_stream 的，所以**这里**才是真入口。
        #
        # 分流改成**看上下文**而不是计数器（第 134 步）：这个假模型是全局共享实例，
        # 用 `self.calls % 2` 在并发下会互相干扰（一个请求的第二次调用可能被
        # 另一个请求的第一次调用"抢走"）。判据：上下文里已经出现过 tool 消息
        # 就说明"工具已经查过了"，该给最终回答了。
        self.calls += 1
        already_used_tool = any(
            getattr(message, "role", "") == "tool" for message in messages
        )
        if tools and not already_used_tool:
            yield StreamEvent(kind="delta", text="我查")
            yield StreamEvent(
                kind="done",
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="search_recipes",
                            arguments={"keyword": "鱼"},
                            raw_arguments='{"keyword": "鱼"}',
                        )
                    ],
                ),
            )
            return

        # 真的分几段吐，才能验证前端是"边收边画"
        for piece in ("我", "来", "看", "看"):
            yield StreamEvent(kind="delta", text=piece)
        yield StreamEvent(
            kind="done", message=Message(role="assistant", content="我看看")
        )


# --------------------------------------------------------------- 假出行工具


class _FailingGeocode:
    """永远失败的地理编码：用来触发"failed 且不给假行程"的路径。"""

    name = "geocode_city"
    description = "城市坐标"
    show_in_menu = False
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, **_kwargs: Any):
        from core.schemas import ToolResult

        return ToolResult(
            tool_name=self.name, ok=False, error="地理编码服务不可用（烟测注入）"
        )

    async def safe_run(self, **kwargs: Any):
        return await self.run(**kwargs)

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description,
                         "parameters": self.parameters},
        }


class _NamedNoop:
    """占位工具：满足路由的装配检查，但**不该被调用**（地理编码先失败）。"""

    show_in_menu = False
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"烟测占位：{name}"

    async def run(self, **_kwargs: Any):
        from core.schemas import ToolResult

        return ToolResult(tool_name=self.name, ok=True, content="烟测占位")

    async def safe_run(self, **kwargs: Any):
        return await self.run(**kwargs)

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description,
                         "parameters": self.parameters},
        }


# --------------------------------------------------------------- 组装 app


def _build_app():  # noqa: ANN201 —— 返回 FastAPI，但顶层不导入它
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    from agents.agent import Agent, build_system_prompt
    from api.recipe_routes import router as recipe_router
    from api.routes import router as chat_router
    from api.trip_routes import router as trip_router
    from core.recipe_library import create_recipe, ensure_schema
    from core.recipes import RecipeSessionStore
    from core.session import SessionStore
    from core.storage import Storage
    from core.trip_store import TripStore
    from tools.registry import ToolRegistry

    db = Storage(":memory:")
    ensure_schema(db)
    for name, tags in (("清蒸鲈鱼", ["清淡", "鱼"]), ("番茄炒蛋", ["家常", "蛋"])):
        create_recipe(db, name=name, tags=tags, ingredients=["盐"], steps=["做"])

    registry = ToolRegistry()
    from tools.recipe_search_tool import RecipeSearchTool

    registry.register(RecipeSearchTool(storage=db))

    llm = _ScriptedLLM()
    agent = Agent(
        llm=llm,
        registry=registry,
        max_steps=3,
        system_prompt=build_system_prompt("烟测"),
    )

    # 出行路由要求这三个名字都在（`_REQUIRED_TOOLS`），缺一个就 500。
    # geocode 用"永远失败"的替身 —— 这正是要验证的 failed 路径。
    travel_registry = ToolRegistry()
    travel_registry.register(_FailingGeocode())
    travel_registry.register(_NamedNoop("get_weather"))
    travel_registry.register(_NamedNoop("find_nearby_places"))

    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(recipe_router)
    app.include_router(trip_router)
    # 第 129 步：把这几个也挂上。少了它们页面会去请求 404（比如 /api/model），
    # 而"页面少一半接口"正是排查烟测失败时最容易被忽略的干扰项。
    from api.checkin_routes import router as checkin_router
    from api.mail_routes import router as mail_router
    from api.model_routes import router as model_router
    from api.usage_routes import router as usage_router

    app.include_router(model_router)
    app.include_router(usage_router)
    app.include_router(checkin_router)
    app.include_router(mail_router)
    # 挂在**根**而不是 /static：`index.html` 用的是相对路径（`app.js` / `style.css`），
    # 挂到 /static 会让浏览器去请求 `/app.js` → 404 → 脚本没加载 → 点桌宠没反应。
    # `main.py:345` 的注释正是警告这件事，而我在烟测里把同样的坑又踩了一遍（评价8）。
    # 注意：必须放在所有路由之后 —— 它会兜住剩下的所有路径。

    app.state.storage = db
    app.state.session_store = SessionStore(max_messages=40)
    app.state.recipe_store = RecipeSessionStore()
    app.state.agent_factory = lambda: agent
    app.state.llm = llm
    app.state.registry = registry
    # 显式挂上：否则食谱路由会走"缺失 -> 回退 registry"并打一条 warning，
    # 在烟测输出里看起来像出了问题（其实是装配细节）。
    app.state.recipe_registry = registry
    app.state.travel_registry = travel_registry
    app.state.route_tool = _NamedNoop("driving_route")
    app.state.trip_store = TripStore()
    # 第 149 步：签到也得挂上。少了它 `_archive()` 直接 503，
    # 日历永远画不出来 —— 症状是"等不到签到日历渲染完成"，很容易被当成前端问题。
    from core.checkin import CheckinArchive

    app.state.checkin_store = CheckinArchive()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # 静态资源放在最后挂：先匹配 /api/*，剩下的才交给 StaticFiles
    app.mount("/", StaticFiles(directory=str(ROOT / "static"), html=True), name="static")
    return app


def _future_dates(days: int = 30, span: int = 1) -> tuple[str, str]:
    """出行表单要填**今天之后**的日期：路由会拒绝过去的日期。

    理由在产品里写得很清楚：天气只有实况、景点营业时间也只有当下的数据，
    给过去的日期排行程等于编造。烟测必须遵守同一条规则。
    """
    from datetime import date, timedelta

    start = date.today() + timedelta(days=days)
    return start.isoformat(), (start + timedelta(days=span)).isoformat()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:            # noqa: BLE001 —— 起服务期间什么都可能失败
            time.sleep(0.2)
    raise RuntimeError(f"服务没起来：{url}")


# --------------------------------------------------------------- CDP 客户端


class Cdp:
    """够用就行的 CDP 客户端：发命令、等结果、执行 JS。"""

    def __init__(self, websocket: Any) -> None:
        self._ws = websocket
        self._next_id = 0
        self.session_id: str | None = None

    @classmethod
    async def connect(cls, ws_url: str) -> "Cdp":
        import websockets

        websocket = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        return cls(websocket)

    async def send(self, method: str, params: dict[str, Any] | None = None,
                   *, session: bool = True) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method,
                                   "params": params or {}}
        if session and self.session_id:
            message["sessionId"] = self.session_id
        await self._ws.send(json.dumps(message))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            data = json.loads(raw)
            if data.get("id") == self._next_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} 失败：{data['error']}")
                return data.get("result", {})

    async def js(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": f"(() => {{ {expression} }})()",
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, *, timeout: float = 15.0,
                       label: str = "") -> Any:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                value = await self.js(f"return ({expression});")
            except Exception:        # noqa: BLE001 —— 页面还没就绪
                value = None
            if value:
                return value
            await asyncio.sleep(0.1)
        raise AssertionError(f"等不到：{label or expression}")

    async def click_at(self, x: float, y: float) -> None:
        """在页面坐标上发一次**真实鼠标点击**（CDP Input 域）。

        为什么需要：`element.click()` 是合成事件，页面的拖拽逻辑
        （pointerdown/up + 阈值判断）可能会把后续 click 当"刚拖完"忽略掉。
        真实鼠标事件走的是同一条链路，更容易复现用户操作。
        """
        for kind in ("mousePressed", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": float(x),
                    "y": float(y),
                    "button": "left",
                    "clickCount": 1,
                },
            )

    async def center_of(self, selector: str) -> dict[str, float] | None:
        """取元素中心点（视口坐标）；找不到就返回 None。"""
        return await self.js(
            f"""
            const node = document.querySelector({json.dumps(selector)});
            if (!node) return null;
            const box = node.getBoundingClientRect();
            if (!box.width || !box.height) return null;
            return {{x: box.x + box.width / 2, y: box.y + box.height / 2}};
            """
        )

    async def close(self) -> None:
        await self._ws.close()


# --------------------------------------------------------------- 三个场景


CHAT_LOG = "document.getElementById('chat-log')"


async def _open_wheel_item(cdp: Cdp, key: str, selector: str) -> None:
    """点桌宠弹出轮盘，再点 `data-key=key` 那一项，等 `selector` 出现。

    第 148 步：签到、模型这类入口**只在轮盘里**（不在「›」的功能栏里），
    所以按文本找不到 —— 得先开轮盘。合成 click 有时被拖拽逻辑吞掉，
    这里保留"真实鼠标再点一次"的回退。
    """
    await cdp.wait_for(
        "document.getElementById('menu-list').childElementCount > 0",
        timeout=20,
        label="app.js 初始化（轮盘项渲染出来）",
    )
    for _attempt in range(3):
        await cdp.js("document.getElementById('pet-button').click(); return true;")
        try:
            await cdp.wait_for(
                "!document.getElementById('menu').hidden", timeout=3, label="轮盘弹出"
            )
            break
        except AssertionError:
            point = await cdp.center_of("#pet-button")
            if point:
                await cdp.click_at(point["x"], point["y"])
            await cdp.wait_for(
                "!document.getElementById('menu').hidden", timeout=5, label="轮盘弹出"
            )

    clicked = await cdp.js(
        f"""
        const item = document.querySelector('.wheel-item[data-key="{key}"]');
        if (!item) return false;
        item.click();
        return true;
        """
    )
    assert clicked, f"轮盘里找不到 data-key={key}"
    await cdp.wait_for(f"!document.querySelector({json.dumps(selector)}).hidden",
                       timeout=10, label=f"{selector} 打开")


async def _open_chat(cdp: Cdp) -> None:
    """打开聊天窗口；对"脚本还没初始化完"和"合成点击被吞掉"都留了余地。"""
    # 先确认 app.js 真的初始化了（init() 会同步调用 renderWheel）
    await cdp.wait_for(
        "document.getElementById('menu-list').childElementCount > 0",
        timeout=20,
        label="app.js 初始化（轮盘项渲染出来）",
    )

    for _attempt in range(3):
        await cdp.js("document.getElementById('pet-button').click(); return true;")
        try:
            await cdp.wait_for(
                "!document.getElementById('menu').hidden", timeout=3, label="轮盘弹出"
            )
            break
        except AssertionError:
            point = await cdp.center_of("#pet-button")
            if point:
                await cdp.click_at(point["x"], point["y"])
            await cdp.wait_for(
                "!document.getElementById('menu').hidden", timeout=5, label="轮盘弹出"
            )

    await cdp.js(
        "document.querySelector('.wheel-item[data-key=\"chat\"]').click(); return true;"
    )
    await cdp.wait_for("!document.getElementById('chat').hidden", label="聊天窗口打开")


async def _open_by_text(cdp: Cdp, text: str, selector: str) -> None:
    """按可见文本找入口并点击（不硬编码内部函数名）。"""
    # 第 147 步：功能栏（出行/邮箱/食谱）从第 146 步起收在「›」里，
    # 默认是 hidden。不先点开，下面的 finder 永远找不到可见的入口 ——
    # 评价10 就是在这里抓到烟测 4 个场景一起红的。
    await cdp.js(
        """
        const more = document.getElementById('chat-more');
        const chips = document.getElementById('chat-chips');
        if (more && chips && chips.hidden) more.click();
        return true;
        """
    )

    finder = f"""
        const nodes = Array.from(document.querySelectorAll('button, .chip, [role="button"]'));
        const hit = nodes.find((n) => (n.textContent || '').includes({json.dumps(text)})
                                  && !n.disabled && n.offsetParent !== null);
        return hit || null;
        """

    # 入口可能比页面晚一步出现（例如胶囊由 init 渲染）：先等它存在
    await cdp.wait_for(
        f"(function(){{ {finder} }}()) !== null",
        timeout=20,
        label=f"「{text}」入口出现",
    )

    clicked = await cdp.js(
        f"""
        const hit = (function(){{ {finder} }}());
        if (!hit) return false;
        hit.click();
        return true;
        """
    )
    assert clicked, f"页面上找不到「{text}」入口（可能改了入口结构）"
    try:
        await cdp.wait_for(
            f"!document.querySelector({json.dumps(selector)}).hidden",
            timeout=4,
            label=f"{text}窗口打开",
        )
        return
    except AssertionError:
        # 合成点击没反应：用真实鼠标再点一次（拖拽逻辑可能吞掉 click）
        hit_id = await cdp.js(
            f"""
            const hit = (function(){{ {finder} }}());
            if (!hit) return null;
            if (!hit.id) hit.id = 'smoke-target';
            return hit.id;
            """
        )
        if hit_id:
            point = await cdp.center_of(f"#{hit_id}")
            if point:
                await cdp.click_at(point["x"], point["y"])
            await cdp.wait_for(
                f"!document.querySelector({json.dumps(selector)}).hidden",
                timeout=8,
                label=f"{text}窗口打开",
            )



async def scenario_chat_stream(cdp: Cdp) -> list[str]:
    notes: list[str] = []
    await _open_chat(cdp)
    await cdp.js(
        """
        window.__mutations = 0;
        const log = document.getElementById('chat-log');
        new MutationObserver(() => { window.__mutations += 1; })
          .observe(log, {subtree: true, childList: true, characterData: true});
        const input = document.getElementById('user-input');
        input.value = '今天吃什么';
        input.dispatchEvent(new Event('input', {bubbles: true}));
        document.getElementById('composer').requestSubmit();
        return true;
        """
    )
    await cdp.wait_for(
        f"({CHAT_LOG}).textContent.includes('我看看')", timeout=20, label="流式回答出现"
    )
    # 第 140 步：气泡与右上角两个按钮不许重叠（用户截图报的）。
    # 量的是**真实几何**：第一条气泡的上沿必须落在按钮下沿之下。
    overlap = await cdp.js(
        """
        const log = document.getElementById('chat-log');
        const first = log && log.querySelector('.msg, .chat-msg, div');
        const clearBtn = document.getElementById('chat-clear');
        const closeBtn = document.getElementById('chat-close');
        if (!first || !clearBtn || !closeBtn) return {skip: true};
        const bubble = first.getBoundingClientRect();
        const clearBox = clearBtn.getBoundingClientRect();
        const closeBox = closeBtn.getBoundingClientRect();
        return {
          bubbleTop: bubble.top,
          buttonsBottom: Math.max(clearBox.bottom, closeBox.bottom),
          clearRight: clearBox.right,
          closeRight: closeBox.right,
        };
        """
    )
    if not overlap.get("skip"):
        notes.append(
            f"气泡上沿 {overlap['bubbleTop']:.0f} / 按钮下沿 {overlap['buttonsBottom']:.0f}"
        )
        assert overlap["bubbleTop"] >= overlap["buttonsBottom"] - 1, (
            f"气泡压在按钮下面了：气泡上沿 {overlap['bubbleTop']:.0f} < "
            f"按钮下沿 {overlap['buttonsBottom']:.0f}"
        )

    mutations = await cdp.js("return window.__mutations;")
    notes.append(f"气泡更新次数 {mutations}")
    assert mutations and mutations >= 2, (
        f"气泡只更新了 {mutations} 次 —— 说明不是增量渲染，而是一次成型"
    )
    return notes


async def scenario_recipe_cards(cdp: Cdp) -> list[str]:
    notes: list[str] = []
    await _open_by_text(cdp, "食谱", "#recipe-modal")

    # 等**初始化完成**再发：`prepareRecipe()` 是异步的，最后一步 restore 会
    # `replaceChildren()`。发得太早会撞上它 —— 那正是评价8 抓到的竞态。
    # 就绪信号：对话区已经有内容（开场白或恢复出来的历史）。
    await cdp.wait_for(
        "(document.getElementById('recipe-log') || {}).childElementCount > 0",
        timeout=20, label="食谱窗口初始化完成",
    )

    await cdp.js(
        """
        const page = document.getElementById('recipe-chat-page');
        if (page && page.hidden) {
          const entry = document.getElementById('recipe-chat-entry');
          if (entry) entry.click();
        }
        const input = document.getElementById('recipe-input');
        input.value = '想吃鱼';
        input.dispatchEvent(new Event('input', {bubbles: true}));
        document.getElementById('recipe-composer').requestSubmit();
        return true;
        """
    )
    count = await cdp.wait_for(
        "document.querySelectorAll('.recipe-answer-card').length", timeout=25,
        label="食谱卡片",
    )
    notes.append(f"卡片 {count} 张")
    assert int(count) >= 1, "SSE 的 meta 到了却没有任何卡片"

    # 右栏在对话页里，而 showRecipeView('chat') 有 220ms 过渡（先播动画再 hidden=false）。
    # 不等它就会量到一个"还没有盒子"的元素 —— width/right 全是 0（实测踩过）。
    await cdp.wait_for(
        "!document.getElementById('recipe-chat-page').hidden",
        timeout=10,
        label="对话页显示（右栏才有布局）",
    )

    # 右侧栏必须**真的在窗口里**（第 131 步踩过：给 #recipe-log 多包一层，
    # 它失去 flex 子项身份，aside 被挤出窗口 —— DOM 桩测不出这种塌陷，
    # 只有量真实几何才发现）。
    box = await cdp.js(
        """
        const aside = document.getElementById('recipe-aside');
        if (!aside) return null;
        const rect = aside.getBoundingClientRect();
        return {width: rect.width, right: rect.right, inner: window.innerWidth};
        """
    )
    assert box, "找不到右侧栏元素"
    notes.append(f"右侧栏宽 {box['width']:.0f}px，右边界 {box['right']:.0f}/{box['inner']:.0f}")
    assert box["width"] >= 200, (
        f"右侧栏被压扁了（视口 1200 宽时它应当是 296px 的列）：{box}"
    )
    assert box["right"] <= box["inner"] + 1, (
        f"右侧栏跑到窗口外面了（右边界 {box['right']:.0f} > 窗口 {box['inner']:.0f}）"
    )

    # 窄屏分支（≤900px）也要看一眼：CSS 把右栏收成横条。
    # 这个分支我刚踩过（758px 时窗口默认宽度命中了它），所以值得钉住：
    # 收成横条可以，但**不许横向溢出**、也不许彻底消失。
    await cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 758, "height": 900, "deviceScaleFactor": 1, "mobile": False},
    )
    narrow = await cdp.js(
        """
        const aside = document.getElementById('recipe-aside');
        const rect = aside.getBoundingClientRect();
        return {
          width: rect.width,
          scrollWidth: document.documentElement.scrollWidth,
          inner: window.innerWidth,
        };
        """
    )
    notes.append(
        f"窄屏(758) 右栏 {narrow['width']:.0f}px；横向内容 {narrow['scrollWidth']:.0f}/{narrow['inner']:.0f}"
    )
    assert narrow["width"] > 0, f"窄屏下右栏彻底消失了：{narrow}"
    assert narrow["scrollWidth"] <= narrow["inner"] + 1, (
        f"窄屏出现横向溢出（{narrow['scrollWidth']} > {narrow['inner']}），窗口会被撑出滚动条"
    )

    # 恢复宽视口，后面的场景不受影响
    await cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1200, "height": 900, "deviceScaleFactor": 1, "mobile": False},
    )
    return notes


async def scenario_trip_failed(cdp: Cdp) -> list[str]:
    notes: list[str] = []
    await _open_by_text(cdp, "出行", "#travel-modal")
    start, end = _future_dates()
    await cdp.js(
        f"""
        document.getElementById('travel-city').value = '北京';
        document.getElementById('travel-start').value = '{start}';
        document.getElementById('travel-end').value = '{end}';
        document.getElementById('travel-form').requestSubmit();
        return true;
        """
    )
    # 失败原因写进 `#travel-error`；`#plan-notice` 是"降级/校验提示"那一类
    # （评价8 指出我原来等错了元素，那个场景永远等不到）
    notice = await cdp.wait_for(
        "((document.getElementById('travel-error') || {}).textContent || '').trim()",
        timeout=25, label="失败原因",
    )
    notes.append(f"提示：{str(notice)[:40]}")
    # 真正的失败语义（`static/app.js:1177`）：**整个行程都不渲染**，
    # 停在表单页 + 红字原因。所以这里要查的是"什么都没画"，而不是"画了空天"。
    # （"这一天没有排到景点"是**生成成功但某天为空**时的文案，不属于失败态 ——
    # 我上一版把它当失败态断言，才有了这次误报。）
    plan_hidden = await cdp.js(
        "return document.getElementById('travel-page-plan').hidden;"
    )
    spots = await cdp.js(
        "return document.querySelectorAll('#travel-modal [class*=\"spot-\"]').length;"
    )
    days = await cdp.js("return document.querySelectorAll('.day-item').length;")
    notes.append(f"计划页隐藏={plan_hidden}；景点条目={spots}；天标签={days}")
    # 不把"计划页隐藏"当断言：它失败时本来就是默认隐藏的，断言它等于没断言。
    # 只当参考信息记下来。
    assert int(spots) == 0, f"地理编码失败却渲染了 {spots} 个景点条目"
    assert int(days) == 0, f"地理编码失败却渲染了 {days} 个天标签"
    assert "没能生成行程" in str(notice), f"失败原因不明确：{notice!r}"
    return notes


async def scenario_recipe_new_session(cdp: Cdp) -> list[str]:
    """食谱「新建会话」：确认后清空对话，勾选的菜也一起清（第 131 步）。

    这条只能靠真浏览器验 —— DOM 桩里点不到"确认框"，也测不出 `hidden` 的真实布局。
    """
    notes: list[str] = []
    if await cdp.js("return document.getElementById('recipe-modal').hidden;"):
        await _open_by_text(cdp, "食谱", "#recipe-modal")
    await cdp.wait_for(
        "(document.getElementById('recipe-log') || {}).childElementCount > 0",
        timeout=20, label="食谱窗口初始化完成",
    )

    # 先造一点可清的东西：勾一张卡片 + 发一句话
    await cdp.js(
        """
        const page = document.getElementById('recipe-chat-page');
        if (page && page.hidden) {
          const entry = document.getElementById('recipe-chat-entry');
          if (entry) entry.click();
        }
        const input = document.getElementById('recipe-input');
        input.value = '想吃鱼';
        input.dispatchEvent(new Event('input', {bubbles: true}));
        document.getElementById('recipe-composer').requestSubmit();
        return true;
        """
    )
    await cdp.wait_for(
        "document.querySelectorAll('.recipe-answer-card').length > 0",
        timeout=25, label="先有一张卡片",
    )
    await cdp.js(
        "const pick = document.querySelector('.recipe-pick');"
        "if (pick) { pick.checked = true; pick.dispatchEvent(new Event('change', {bubbles: true})); }"
        "return true;"
    )
    picked = await cdp.js("return document.getElementById('shopping-bar').hidden === false;")
    notes.append(f"勾选后有工具条={picked}")

    # 新建会话：点按钮 -> 确认框 -> 确认
    await cdp.js("document.getElementById('recipe-new').click(); return true;")
    await cdp.wait_for(
        "!document.getElementById('recipe-confirm').hidden", timeout=10, label="确认框出现"
    )
    await cdp.js("document.getElementById('recipe-confirm-ok').click(); return true;")
    await cdp.wait_for(
        "document.querySelectorAll('.recipe-answer-card').length === 0",
        timeout=15, label="对话被清空",
    )
    await cdp.wait_for(
        "document.getElementById('recipe-confirm').hidden", timeout=10, label="确认框关闭"
    )
    log_text = await cdp.js(
        "return (document.getElementById('recipe-log').textContent || '').trim().length;"
    )
    bar_hidden = await cdp.js("return document.getElementById('shopping-bar').hidden;")
    notes.append(f"清空后对话区 {log_text} 字；工具条隐藏={bar_hidden}")
    assert int(log_text) > 0, "清空后应该回到开场白，而不是一片空白"
    assert bar_hidden is True, "新建会话后勾选的菜应当一起清掉"

    return notes


async def scenario_recipe_opens_at_bottom(cdp: Cdp) -> list[str]:
    """重开食谱对话时停在最后一条（第 137 步，用户反馈）。

    真浏览器的**布局**才能验这件事：DOM 桩里 scrollHeight 恒为 0，
    写多少遍 `scrollTop = scrollHeight` 都看不出对错。
    """
    notes: list[str] = []

    async def open_chat_page() -> None:
        if await cdp.js("return document.getElementById('recipe-modal').hidden;"):
            await _open_by_text(cdp, "食谱", "#recipe-modal")
        await cdp.wait_for(
            "(document.getElementById('recipe-log') || {}).childElementCount > 0",
            timeout=20, label="食谱窗口初始化完成",
        )
        await cdp.js(
            """
            const page = document.getElementById('recipe-chat-page');
            if (page && page.hidden) {
              const entry = document.getElementById('recipe-chat-entry');
              if (entry) entry.click();
            }
            return true;
            """
        )
        await cdp.wait_for(
            "!document.getElementById('recipe-chat-page').hidden",
            timeout=10, label="对话页显示",
        )

    # 先发几轮，把对话撑到需要滚动
    await open_chat_page()
    for index in range(3):
        before = int(
            await cdp.js("return document.querySelectorAll('.recipe-answer-card').length;")
        )
        await cdp.js(
            f"""
            const input = document.getElementById('recipe-input');
            input.value = '想吃鱼 {index}';
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            document.getElementById('recipe-composer').requestSubmit();
            return true;
            """
        )
        # 等**这一轮**的卡片（上一轮的会让"length > 0"立刻成立）
        await cdp.wait_for(
            f"document.querySelectorAll('.recipe-answer-card').length > {before}",
            timeout=25,
            label=f"第 {index + 1} 轮卡片出现",
        )

    overflow = await cdp.js(
        """
        const log = document.getElementById('recipe-log');
        return log.scrollHeight > log.clientHeight + 20;
        """
    )
    if not overflow:
        # 第 145 步：不许再"软通过"（评价9 P2 点名）。
        # 内容不够滚动 = 这条断言其实什么都没验。做法是把窗口压矮**逼出**可滚动状态，
        # 而不是记一句"跳过"就过。
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1200, "height": 420, "deviceScaleFactor": 1, "mobile": False},
        )
        await cdp.wait_for(
            "document.getElementById('recipe-log').scrollHeight"
            " > document.getElementById('recipe-log').clientHeight + 20",
            timeout=10,
            label="压矮窗口后对话区可滚动",
        )
        notes.append("窗口压到 420 高才可滚动")

    # 关窗再打开
    await cdp.js("document.getElementById('recipe-close').click(); return true;")
    await cdp.wait_for("document.getElementById('recipe-modal').hidden", timeout=10,
                       label="食谱窗口关闭")
    await open_chat_page()
    await cdp.wait_for("(document.getElementById('recipe-log').scrollHeight) > 0",
                       timeout=10, label="对话区有高度")

    # 滚动有两次：露出对话区后的 rAF、以及卡片按 id 异步补回来之后。
    # 直接量会量到"还没滚"的中间态（实测：scrollTop=2 / 距底 144px），
    # 所以先等它**稳定到贴底**，再断言。
    try:
        await cdp.wait_for(
            "(function(){const l=document.getElementById('recipe-log');"
            "return l.scrollHeight - l.clientHeight - l.scrollTop <= 8;})()",
            timeout=6,
            label="对话区滚到底",
        )
    except AssertionError:
        pass          # 没滚到底就让下面的断言报出真实数字

    box = await cdp.js(
        """
        const log = document.getElementById('recipe-log');
        return {
          top: log.scrollTop,
          height: log.scrollHeight,
          client: log.clientHeight,
        };
        """
    )
    gap = box["height"] - box["client"] - box["top"]
    notes.append(f"scrollTop={box['top']:.0f} 距底部 {gap:.0f}px")
    assert gap <= 8, (
        f"重开之后没停在最后一条（离底部还有 {gap:.0f}px，"
        f"scrollTop={box['top']:.0f} / 总高 {box['height']:.0f}）"
    )
    return notes


async def scenario_checkin_calendar(cdp: Cdp) -> list[str]:
    """签到日历的真实布局（第 147 步 / 评价10 的"测试缺口"）。

    评价10 指出：日历改成 6 行之后，**没有任何真浏览器验证**。DOM 桩能验数据与状态，
    验不了"6 行 × 7 列有没有溢出、休/班小标有没有画出来" —— 那是布局。
    """
    notes: list[str] = []

    if await cdp.js("return document.getElementById('checkin-modal').hidden;"):
        # 签到入口在**轮盘**里（不在「›」的功能栏里），按文本找不到
        await _open_wheel_item(cdp, "checkin", "#checkin-modal")
    try:
        await cdp.wait_for(
            "!document.getElementById('checkin-grid').hidden",
            timeout=20,
            label="签到日历渲染完成",
        )
    except AssertionError as exc:
        # 失败时把窗口里那行状态文字带出来 —— 否则只会看到"等不到"，
        # 分不清是接口 503、还是前端渲染坏了。
        state = await cdp.js(
            "return (document.getElementById('checkin-state') || {}).textContent || '';"
        )
        raise AssertionError(f"{exc}；签到窗口状态文字：{state!r}") from exc

    grid = await cdp.js(
        """
        const table = document.getElementById('checkin-grid');
        const rows = document.querySelectorAll('#checkin-days tr');
        const cells = document.querySelectorAll('#checkin-days button.cal-day');
        const badges = document.querySelectorAll('#checkin-days .cal-badge');
        const restCells = document.querySelectorAll(
          '#checkin-days .cal-day.is-rest-off, #checkin-days .cal-day.is-rest-work'
        );
        return {
          rows: rows.length,
          rowLengths: Array.from(rows).map((row) => row.children.length),
          cells: cells.length,
          badges: badges.length,
          restCells: restCells.length,
          width: table.getBoundingClientRect().width,
          scrollWidth: document.getElementById('checkin-modal').scrollWidth,
          inner: window.innerWidth,
          label: (document.getElementById('checkin-month-label') || {}).textContent || '',
        };
        """
    )
    notes.append(
        f"{grid['label']} · {grid['rows']} 行 {grid['rowLengths']} / {grid['cells']} 格 / "
        f"{grid['badges']} 个休班标（对应 {grid['restCells']} 个标记格）"
    )

    # 5 或 6 行 —— **不是一个固定值**：10 月（周四月首）只占 5 行，3 月要 6 行。
    # 上次我把"6 行"写死，10 月一跑就红（等于按某个月的形状写断言）。
    assert 5 <= grid["rows"] <= 6, f"日历行数应在 5–6 之间，实际 {grid['rows']}"
    lengths = grid["rowLengths"]
    assert all(n == 7 for n in lengths[:-1]), f"除最后一行外每行都该是 7 格：{lengths}"
    assert 1 <= lengths[-1] <= 7, f"最后一行格数异常：{lengths}"
    assert grid["cells"] >= 28, f"一个月的日期格至少 28 个，实际 {grid['cells']}"
    assert grid["width"] > 200, f"日历宽度异常：{grid['width']}"
    assert grid["scrollWidth"] <= grid["inner"] + 1, (
        f"日历把窗口撑出横向滚动（{grid['scrollWidth']} > {grid['inner']}）"
    )
    # 休/班小标：不能写死"必须有" —— 只有 2024–2026 有放假安排，
    # 换个月（或以后年份）本来就可以一个都没有。
    # 真正的不变量是**两者一一对应**：每个休/班格恰好一个小标，也没有无主的小标。
    assert grid["badges"] == grid["restCells"], (
        f"休/班小标与标记格对不上：{grid['badges']} 个小标 vs {grid['restCells']} 个标记格"
    )

    await cdp.js("document.getElementById('checkin-close').click(); return true;")
    return notes


async def scenario_pet_actions(cdp: Cdp) -> list[str]:
    """桌宠动作菜单（第 159 步）：两个视频动作 + 手动点击优先。

    要验的三件事，都是用户实际撞到过的：
    1. 菜单里是**两个**动作（喝茶 / 擦擦灰），不是只剩一个；
    2. 点「擦擦灰」后视频**一直播到底**，不会被待机调度换掉（以前播到一半就重置）；
    3. 手动点「喝茶」可以打断正在播的动作（用户点了就该有反应）。
    """
    notes: list[str] = []

    if await cdp.js("return document.getElementById('action-menu').hidden;"):
        await _open_wheel_item(cdp, "actions", "#action-menu")

    menu = await cdp.js(
        """
        const menu = document.getElementById('action-menu');
        const items = Array.from(menu.querySelectorAll('.action-item'));
        const box = menu.getBoundingClientRect();
        const next = items[1] ? items[1].getBoundingClientRect() : null;
        return {
          count: items.length,
          ids: items.map((item) => item.dataset.action),
          labels: items.map((item) => (item.textContent || '').trim()),
          width: box.width,
          stacked: next ? next.top >= items[0].getBoundingClientRect().bottom - 1 : true,
        };
        """
    )
    notes.append(f"菜单 {menu['count']} 项：{menu['labels']}；宽 {menu['width']:.0f}px")

    # 断言**不依赖动作总数**：动作池会一直加（这一轮就加了「摸鱼」），
    # 写死数量只会让每加一个动作就红一次（已经踩过三次）。
    # 这里只钉"菜单里确实有动作、我们要点的那个在、且每项都带 data-action"。
    assert menu["count"] >= 2, f"菜单里至少要有两个动作，实际 {menu['count']}"
    assert all(menu["ids"]), f"有动作项没带 data-action：{menu['ids']}"
    assert "wipe" in menu["ids"], f"菜单里没有「擦擦灰」：{menu['ids']}"
    assert any("擦擦灰" in label for label in menu["labels"]), menu["labels"]
    assert menu["stacked"], "菜单项应当由上而下竖排"
    assert menu["width"] > 60, f"菜单宽度异常：{menu['width']}"

    # 点「擦擦灰」：应当切到 wipe.webm，并且 2 秒后仍然是它（不被待机抢戏）
    await cdp.js(
        "document.querySelector('#action-menu .action-item[data-action=wipe]').click();"
        " return true;"
    )
    started = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        return { src: idle.src, playing: !document.getElementById('action-menu').hidden };
        """
    )
    notes.append(f"点擦擦灰 -> {started['src'].split('/').pop()}")
    assert "wipe.webm" in started["src"], f"没有切到擦擦灰素材：{started['src']}"

    await asyncio.sleep(2.0)
    later = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        const pet = document.getElementById('pet');
        return {
          src: idle.src,
          playing: pet.classList.contains('is-idle-playing'),
          currentTime: idle.currentTime,
        };
        """
    )
    notes.append(
        f"2 秒后：{later['src'].split('/').pop()}，"
        f"playing={later['playing']}，t={later['currentTime']:.1f}s"
    )
    assert "wipe.webm" in later["src"], (
        f"播到一半被换掉了（待机抢戏）：{later['src']}"
    )

    # 手动点「喝茶」：允许打断
    if await cdp.js("return document.getElementById('action-menu').hidden;"):
        await _open_wheel_item(cdp, "actions", "#action-menu")
    await cdp.js(
        "document.querySelector('#action-menu .action-item[data-action=tea]').click();"
        " return true;"
    )
    switched = await cdp.js("return document.getElementById('pet-idle').src;")
    notes.append(f"手动切到 {switched.split('/').pop()}")
    assert "tea.webm" in switched, f"手动点击没有生效：{switched}"
    return notes


async def scenario_pet_idle(cdp: Cdp) -> list[str]:
    """待机动画：每 10 秒演一次（第 151 步）。

    **只能真浏览器验**：DOM 桩里 setInterval 立即执行，"每 10 秒一次"这个节奏
    根本不存在；而"动画在演的时候静帧要淡出"也只有真 CSS 才有意义。
    """
    notes: list[str] = []

    await cdp.wait_for(
        "!!document.getElementById('pet-idle')", timeout=10, label="动作层存在"
    )
    kind = await cdp.js(
        "return document.getElementById('pet-idle').tagName;"
    )
    assert kind == "VIDEO", f"动作层应当是 <video>（webm 要它才能播），实际 {kind}"

    # 第一轮：最多等 13 秒（间隔 10 秒 + 一点余量）
    await cdp.wait_for(
        "document.getElementById('pet').classList.contains('is-idle-playing')",
        timeout=13,
        label="待机动画开始播放（每 10 秒一次）",
    )
    started = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        const still = document.getElementById('pet-image');
        const idleBox = idle.getBoundingClientRect();
        return {
          src: idle.src,
          currentTime: idle.currentTime,
          stillOpacity: Number(getComputedStyle(still).opacity),
          idleOpacity: Number(getComputedStyle(idle).opacity),
          width: idleBox.width,
          height: idleBox.height,
        };
        """
    )
    notes.append(
        f"开播：动画不透明度 {started['idleOpacity']:.2f} / 静帧 {started['stillOpacity']:.2f}"
        f" / {started['width']:.0f}x{started['height']:.0f}"
    )

    # 第 153 步起素材是 webm：重播靠 currentTime 归零，不是换 URL
    assert started["src"].endswith(".webm"), f"动作素材应当是 webm：{started['src']}"
    # 读的时候视频已经播了几十毫秒（读数本身要花时间），所以判"接近开头"而不是恰好 0
    assert started["currentTime"] < 1.0, (
        f"视频动作应当从头播，实际已到 {started['currentTime']:.2f}s"
    )
    assert started["idleOpacity"] > 0.5, "开始播了，动画层却还是透明的"
    assert started["stillOpacity"] < 0.5, "动画在演时静帧应当淡出（否则两层叠着）"
    assert started["width"] > 10 and started["height"] > 10, "动画层尺寸异常"

    # 第 175 步：间隔改成 0 之后，"播完收起"这个瞬间不存在（刚收起就接下一个）。
    # 改成看"演过的动作数在涨" —— 这才是"动画链在推进"的本质。
    await cdp.wait_for(
        "Number(document.getElementById('pet').dataset.actions || 0) >= 2",
        timeout=12,          # 素材 8 秒，留一点余量
        label="动画链推进到第二个动作",
    )
    return notes


async def scenario_pet_drag(cdp: Cdp) -> list[str]:
    """拖拽姿势（第 163 步）：真鼠标按住拖 → 播 drag.webm 循环；松手 → 回静帧。

    DOM 桩能验"按/松的接线"，但验不了"按住的时候视频真的在循环播"——
    那要真浏览器。
    """
    notes: list[str] = []

    box = await cdp.js(
        """
        const pet = document.getElementById('pet-button').getBoundingClientRect();
        return { x: pet.x + pet.width / 2, y: pet.y + pet.height / 2 };
        """
    )
    start_x, start_y = box["x"], box["y"]

    # 按住 → 分步移动 → 停一会儿（检查姿势）→ 松开。用手写步骤而不是封装，
    # 因为"按住时的状态"必须在松开之前读。
    async def mouse(kind: str, x: float, y: float, *, held: bool) -> None:
        event = {
            "type": kind,
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        }
        if held:
            event["buttons"] = 1
        await cdp.send("Input.dispatchMouseEvent", event)

    await mouse("mousePressed", start_x, start_y, held=True)
    for step in range(1, 9):
        ratio = step / 8
        await mouse(
            "mouseMoved",
            start_x + 90 * ratio,
            start_y + 60 * ratio,
            held=True,
        )
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.4)          # 刚拎起来的瞬间
    during = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        return { src: idle.src, loop: idle.loop };
        """
    )
    notes.append(f"拎起来：{during['src'].split('/').pop()} loop={during['loop']}")
    assert "drag-lift.webm" in during["src"], f"拖动时没有播「被拎起来」：{during['src']}"

    # 「被拎起来」3.2 秒播完 -> 应当自动切到「来回荡」并循环（长时间按着不回地面）
    await cdp.wait_for(
        "document.getElementById('pet-idle').src.indexOf('drag-swing.webm') >= 0",
        timeout=6,
        label="提起播完切到「来回荡」",
    )
    swinging = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        return { src: idle.src, loop: idle.loop };
        """
    )
    notes.append(f"荡起来：{swinging['src'].split('/').pop()} loop={swinging['loop']}")
    assert swinging["loop"] is True, "「来回荡」应当循环播"

    await mouse("mouseReleased", start_x + 90, start_y + 60, held=False)
    await asyncio.sleep(0.2)

    after = await cdp.js(
        """
        const idle = document.getElementById('pet-idle');
        return { loop: idle.loop, src: idle.src };
        """
    )
    notes.append(
        f"松手后：{after['src'].split('/').pop()} loop={after['loop']}"
    )
    assert after["loop"] is False, "松手后不该继续循环"
    # 第 175 步：松手会立刻接回待机（间隔 0），所以不能断言"动作层收起"——
    # 断言"已经不在播拖拽素材"才是对的。
    assert "drag-" not in after["src"], f"松手后还在播拖拽素材：{after['src']}"

    # ---- 第 176 步的回归：动作不许被上一任的残留计时器打断 ----
    # 有那个 bug 时，每个动作刚播 0.4 秒就被上一个动作的兜底计时器掐断，
    # 12 秒里能换几十个动作。正常节奏是 8~10 秒一个。
    before_count = await cdp.js(
        "return Number(document.getElementById('pet').dataset.actions || 0);"
    )
    await asyncio.sleep(12)
    after_count = await cdp.js(
        "return Number(document.getElementById('pet').dataset.actions || 0);"
    )
    switched = after_count - before_count
    notes.append(f"12 秒内换了 {switched} 个动作")
    assert switched <= 3, (
        f"12 秒内换了 {switched} 个动作 —— 动作被打断了（正常应当 ≤3）"
    )
    return notes


# --------------------------------------------------------------- 主流程


async def _run(keep: bool) -> int:
    chrome = next((path for path in CHROME_CANDIDATES if Path(path).exists()), None)
    if chrome is None:
        # 「没跑」不等于「通过」：退出码 2 让 CI/脚本能区分（评价8）
        print("找不到 Chrome / Edge：真浏览器烟测**没有执行**")
        return 2

    import threading

    import uvicorn

    port = _free_port()
    debug_port = _free_port()
    app = _build_app()

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    profile = Path(tempfile.mkdtemp(prefix="pet-smoke-"))
    process: subprocess.Popen[bytes] | None = None
    cdp: Cdp | None = None
    failures: list[str] = []
    try:
        _wait_http(f"http://127.0.0.1:{port}/api/health")

        # 第 129 步：先确认浏览器**要拿的那两个资源**真的在（不依赖浏览器）。
        # CI 上曾经出现过"三项全挂但看不出原因"，其中一种可能就是这个。
        for asset in ("/app.js", "/style.css"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{asset}", timeout=10
            ) as response:
                size = len(response.read())
            if response.status != 200 or size < 1000:
                raise RuntimeError(
                    f"静态资源不可用：{asset} -> HTTP {response.status}, {size} 字节"
                )
            print(f"  静态资源 {asset} -> 200，{size} 字节")
        process = subprocess.Popen(
            [
                chrome, "--headless=new", f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-gpu", "--disable-extensions", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        version_url = f"http://127.0.0.1:{debug_port}/json/version"
        try:
            _wait_http(version_url, timeout=30)
        except RuntimeError as exc:
            # 浏览器存在但起不来（沙箱拦住 crashpad、没有显示、被安全软件挡等）。
            # 退出码 3 与"没有浏览器"(2) 分开，免得两者混为一谈。
            print(f"浏览器没能启动（{chrome}）：{exc}")
            print("  常见原因：沙箱/安全软件拦住了 Chrome 的 crashpad，或调试端口被占。")
            return 3
        with urllib.request.urlopen(version_url, timeout=5) as response:
            ws_url = json.loads(response.read())["webSocketDebuggerUrl"]
        # webSocketDebuggerUrl 指向浏览器级，不是页面级
        cdp = await Cdp.connect(ws_url)
        target = await cdp.send(
            "Target.createTarget", {"url": f"http://127.0.0.1:{port}/"}, session=False
        )
        attached = await cdp.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True},
            session=False,
        )
        cdp.session_id = attached["sessionId"]
        await cdp.send("Runtime.enable")
        await cdp.send("Page.enable")

        # 第 151 步起待机动作是**随机**挑的（晃晃 / 擦擦灰）。烟测要断言"演了哪个"，
        # 所以先把 Math.random 钉成 0 —— 那么第一个动作永远是「晃晃」（有素材的那条）。
        await cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Math.random = () => 0;"},
        )

        # 第 138 步：**固定视口**。之前依赖 runner 的默认窗口宽度：
        # 一次跑出来 758px（命中 ≤900px 的窄屏断点，右栏收成横条），
        # 另一次可能是 1200px —— 同一条断言时好时坏。
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1200, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )

        # 第 129 步：在**页面脚本之前**装错误收集器。
        # 没有它，一次顶层抛错的表现只是"点了没反应"，得靠猜。
        await cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "window.__errors = [];"
                    "window.addEventListener('error', (e) => {"
                    "  window.__errors.push(String(e.message || e.error));"
                    "});"
                    "window.addEventListener('unhandledrejection', (e) => {"
                    "  window.__errors.push('rejection: ' + String(e.reason));"
                    "});"
                )
            },
        )
        await cdp.send("Page.reload")
        await cdp.wait_for("document.readyState === 'complete'", label="页面加载")
        await cdp.wait_for("!!document.getElementById('pet-button')", label="桌宠渲染")

        # 真正判"脚本跑起来了"的信号：init() 会同步调用 renderWheel()。
        # 只等 readyState 是不够的 —— 脚本顶层一抛错，页面照样"加载完成"，
        # 但点什么都没反应（CI 上就是这样：三项全挂却看不出原因）。
        try:
            await cdp.wait_for(
                "document.getElementById('menu-list').childElementCount > 0",
                timeout=15,
                label="轮盘渲染（说明 app.js 初始化完成）",
            )
        except AssertionError:
            errors = await cdp.js("return (window.__errors || []).slice(0, 5);")
            raise AssertionError(
                "app.js 没有初始化（轮盘一项都没渲染）。页面里的错误："
                f"{errors}"
            ) from None

        for name, scenario in (
            ("聊天流式（SSE 增量渲染）", scenario_chat_stream),
            ("食谱卡片（meta → 卡片）", scenario_recipe_cards),
            ("出行失败（只给原因、不给假行程）", scenario_trip_failed),
            ("食谱新建会话（清对话、清勾选）", scenario_recipe_new_session),
            ("食谱重开停在最后一条", scenario_recipe_opens_at_bottom),
            ("签到日历 6 行布局", scenario_checkin_calendar),
            ("桌宠动作（擦擦灰）", scenario_pet_actions),
            ("待机动画链（播完立刻接下一个）", scenario_pet_idle),
            ("拖拽姿势（按住播、松手停）", scenario_pet_drag),
        ):
            print(f"=== {name} ===")
            try:
                notes = await scenario(cdp)
                print("  PASS  " + "；".join(notes))
            except Exception as exc:      # noqa: BLE001 —— 烟测要报出每一条
                failures.append(f"{name}: {exc}")
                print(f"  FAIL  {exc}")
                try:
                    page_errors = await cdp.js(
                        "return (window.__errors || []).slice(0, 5);"
                    )
                    if page_errors:
                        print(f"        页面错误：{page_errors}")
                except Exception:      # noqa: BLE001 —— 诊断失败不该掩盖原报错
                    pass
                if keep:
                    # 存到仓库内的稳定目录：原来存在临时 profile 里，
                    # 最后 `shutil.rmtree(profile)` 会把截图一并删掉（评价8）。
                    shots_dir = ROOT / ".testtmp" / "smoke-shots"
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    shot = shots_dir / f"fail-{len(failures)}.png"
                    try:
                        data = await cdp.send("Page.captureScreenshot", {"format": "png"})
                        import base64

                        shot.write_bytes(base64.b64decode(data["data"]))
                        print(f"        截图：{shot}")
                    except Exception:      # noqa: BLE001
                        pass
    finally:
        if cdp is not None:
            await cdp.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)

    print()
    if failures:
        print(f"烟测失败 {len(failures)} 项：")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("烟测通过：9/9")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="真浏览器烟测（CDP，不花钱）")
    parser.add_argument("--keep", action="store_true", help="失败时保留截图")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
