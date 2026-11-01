"""工具抽象。

这是整个项目最需要一次做对的地方：
新增一个工具 = 新增一个子类 + 注册一行，Agent 循环和 API 层完全不用改。
"""

from __future__ import annotations

import abc
import functools
import inspect
import logging
from typing import Any

from core.schemas import ToolResult

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    """把"看起来是整数"的值转成 int；不像就返回 None。

    为什么接受字符串：模型偶尔把 `max_minutes` 写成 `"30"`。这是**无害的偏差**，
    拒绝它只会让用户莫名其妙地查不到菜，所以矫正；但 `"abc"`、`"半小时"` 这类
    **没法猜**的值必须报错，不能像第 263 步之前那样静默当"没有这个条件"。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce(spec: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    """按 JSON Schema 里声明的类型矫正/拒绝一个参数值。

    返回 `(新值, 错误文案)`。错误文案为空表示通过。
    """
    kind = spec.get("type")
    if kind == "string":
        # 第 263 步：以前这里想把 `q=123` 矫正成 `"123"`。实测有场景（`eval_agent`
        # 的 `k-number-arg-passes-through`）专门钉住「数字参数原样透传」——
        # 我们不是模型的保姆，工具自己能处理就让它处理，**别偷偷改用户的参数**。
        return value, None
    if kind == "integer":
        parsed = _as_int(value)
        if parsed is None:
            return value, f"需要整数，收到 {value!r}"
        return parsed, None
    if kind == "number":
        parsed = _as_float(value)
        if parsed is None:
            return value, f"需要数字，收到 {value!r}"
        return parsed, None
    if kind == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", None
        return value, f"需要 true/false，收到 {value!r}"
    if kind == "array":
        items = value if isinstance(value, (list, tuple)) else [value]
        return list(items), None
    return value, None  # 没声明类型的键原样放行（比如 object）



class BaseTool(abc.ABC):
    """所有工具的基类。

    子类必须定义 name / description / parameters，并实现 run()。
    - description 会直接进入 LLM 的 tools schema，写清楚"什么时候该用"比写"做什么"更重要。
    - parameters 用 JSON Schema 描述参数。
    - label / intent_template 只用于前端的展示与点击行为，不参与 LLM 调用。
      放在工具里是为了"新增工具只改一处"。
    - show_in_menu 决定这个工具的手动入口长什么样（第 23 步）：
      True  → 出现在点击桌宠弹出的轮盘菜单里
      False → 出现在对话面板输入框上方的"快捷意图"芯片里
      两种都是"点了就发送 intent"，只是位置不同；它不影响模型能否调用该工具。
      「入口」和「能力」必须分开：天气/周边就是"不要轮盘项、但要一键可用"的那类。
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: 轮盘菜单上显示的短名称；留空则回退到 name
    label: str = ""
    #: 从菜单点击该工具时发出的自然语言意图（{label} 会被替换成 label）
    intent_template: str = "帮我用{label}看看现在的情况。"
    #: 是否在轮盘菜单里展示
    show_in_menu: bool = True

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI / DeepSeek 的 function calling 工具声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @property
    def display_label(self) -> str:
        """菜单展示名，缺省用工具名，避免前端出现空白按钮。"""
        return self.label.strip() or self.name

    def intent_text(self) -> str:
        """点击该工具时发送的意图文本。"""
        return self.intent_template.format(label=self.display_label)

    @abc.abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """执行工具。

        实现约定：
        - 不要抛异常来表达业务失败（网络错误、城市不存在等），
          而应返回 ToolResult(ok=False, error=...)。
          这样 Agent 可以把失败原因作为 tool 消息回灌给模型，
          模型才有机会向用户解释或换个参数重试。
        - 只有编程错误（比如参数类型完全不对）才允许抛出。
        """
        raise NotImplementedError

    @functools.cached_property
    def _run_signature(self) -> inspect.Signature | None:
        """`run()` 的签名（缓存一份，别每次调用都反射一次）。"""
        try:
            return inspect.signature(self.run)
        except (TypeError, ValueError):    # 某些被 C 扩展或装饰过的 callable
            return None

    def _takes_extra_kwargs(self) -> bool:
        signature = self._run_signature
        if signature is None:
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    def _accepts_parameter(self, key: str) -> bool:
        signature = self._run_signature
        return signature is not None and key in signature.parameters

    def check_arguments(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """按 `parameters` 里声明的 schema 检查模型给来的参数。

        第 263 步（评价16 3.3）：以前只查"必填键在不在"，类型完全不管。于是
        `max_minutes="abc"` 会一路走到 `_within()` 的 `except: return True`，
        被**静默当成"没有这个条件"**——用户勾了"半小时内"，结果端上来一道 90 分钟的菜，
        而界面上没有任何异常。这比报错更坏，因为它看起来"正常"。

        返回 `(清理后的参数, 错误文案)`。规则：
        - 无害的偏差**矫正**：`"30"` → `30`，`tags="辣"` → `["辣"]`；
        - 猜不出来的**拒绝**并给出可读原因（回灌给模型，它才有机会改）；
        - schema 里没声明的键**不擅自丢**：`run()` 能接（形参名或 `**kwargs`）就原样透传，
          真接不了才丢掉并记日志 —— 丢参数本身就是一种静默篡改（`geocode_city` 吃过这个亏）。
        """
        properties: dict[str, Any] = self.parameters.get("properties") or {}
        cleaned: dict[str, Any] = {}

        for key, value in arguments.items():
            if key not in properties:
                if self._accepts_parameter(key) or self._takes_extra_kwargs():
                    logger.info(
                        "工具 %s 收到 schema 外的参数 %r，原样透传", self.name, key
                    )
                    cleaned[key] = value
                else:
                    logger.info(
                        "工具 %s 收到既不在 schema、也不在 run() 签名里的参数 %r，已丢弃",
                        self.name,
                        key,
                    )
                continue
            fixed, problem = _coerce(properties[key], value)
            if problem:
                return {}, f"参数 {key} {problem}"
            cleaned[key] = fixed

        return cleaned, None

    async def safe_run(self, arguments: dict[str, Any]) -> ToolResult:
        """带兜底的执行入口：把任何意外异常转成 ToolResult。

        参数校验也在这里做——LLM 生成的参数并不可信，
        缺必填字段时直接返回可读错误，比让工具内部 KeyError 好得多。
        """
        cleaned, problem = self.check_arguments(arguments)
        if problem:
            return ToolResult(
                ok=False,
                error=problem,
                tool_name=self.name,
                arguments=arguments,
            )

        missing = [
            key
            for key in self.parameters.get("required", [])
            if key not in cleaned or cleaned[key] in (None, "")
        ]
        if missing:
            return ToolResult(
                ok=False,
                error=f"缺少必填参数：{', '.join(missing)}",
                tool_name=self.name,
                arguments=arguments,
            )

        try:
            return await self.run(**cleaned)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须兜底
            logger.exception("工具 %s 执行时抛出未捕获异常", self.name)
            return ToolResult(
                ok=False,
                error=f"工具内部错误：{type(exc).__name__}: {exc}",
                tool_name=self.name,
                arguments=arguments,
            )

