"""Agent 端到端场景评测：跑 `tests/golden/agent_scenarios.json` 里的整轮场景。

验的是跨场景不变量（该不该调工具、参数对不对、失败有没有回灌、步数有没有失控），
用脚本化的假模型，不花钱、能进 CI。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.console import force_utf8  # noqa: E402
from core.schemas import ToolResult  # noqa: E402
from tools.base import BaseTool  # noqa: E402

SCENARIOS = ROOT / "tests" / "golden" / "agent_scenarios.json"


# --------------------------------------------------------------- 假工具


class _Lookup(BaseTool):
    """查一件事：参数是 `q`，缺了会返回可读的失败（验证"参数错也不崩"）。"""

    name = "lookup"
    description = "查一件事"
    show_in_menu = False
    parameters = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }

    async def run(self, **arguments: Any) -> ToolResult:
        q = str(arguments.get("q") or "").strip()
        if not q:
            return ToolResult(tool_name=self.name, ok=False, error="缺少 q", arguments=arguments)
        return ToolResult(
            tool_name=self.name, ok=True, content=f"查到 {q}", arguments=arguments
        )


class _Weather(BaseTool):
    name = "weather"
    description = "查天气"
    show_in_menu = False

    async def run(self, **arguments: Any) -> ToolResult:
        return ToolResult(tool_name=self.name, ok=True, content="晴", arguments=arguments)


class _Boom(BaseTool):
    """会炸的工具：验证"工具失败回灌给模型"这条链。"""

    name = "boom"
    description = "会炸"
    show_in_menu = False

    async def run(self, **arguments: Any) -> ToolResult:
        raise RuntimeError("工具内部炸了")


class _Slow(BaseTool):
    name = "slow"
    description = "慢查"
    show_in_menu = False
    parameters = {
        "type": "object",
        "properties": {"delay": {"type": "number"}},
    }

    async def run(self, **arguments: Any) -> ToolResult:
        await asyncio.sleep(float(arguments.get("delay") or 0))
        return ToolResult(
            tool_name=self.name, ok=True, content="慢查完了", arguments=arguments
        )


class _Echo(BaseTool):
    name = "echo"
    description = "回声"
    show_in_menu = False
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }

    async def run(self, **arguments: Any) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            ok=True,
            content=str(arguments.get("text") or ""),
            arguments=arguments,
        )


TOOLS = {
    "lookup": _Lookup,
    "weather": _Weather,
    "boom": _Boom,
    "slow": _Slow,
    "echo": _Echo,
}


# --------------------------------------------------------------- 脚本化模型


class _ScriptedLLM:
    """按 JSON 脚本吐消息；顺便记录每次调用看到的 messages（用于断言回灌）。"""

    def __init__(self, script: list[dict[str, Any]], ceiling: int = 8) -> None:
        self.script = list(script)
        self.ceiling = ceiling
        self.calls: list[list[Any]] = []

    async def complete(self, messages, tools=None):
        from agents.llm import LLMError
        from core import usage as usage_store
        from core.schemas import Message

        self.calls.append(list(messages))
        if len(self.calls) > self.ceiling:
            raise AssertionError("模型被调用的次数超过脚本长度 —— 循环失控了")
        if not self.script:
            raise LLMError("脚本已用尽")

        step = self.script.pop(0)
        if "spend" in step:
            usage_store.add_turn_tokens(int(step["spend"]))
        if step.get("fail"):
            raise LLMError(str(step["fail"]))
        wanted = step.get("tools") or ([step] if step.get("tool") else [])
        if wanted:
            from core.schemas import ToolCall

            calls = []
            for index, item in enumerate(wanted):
                raw = item.get("raw_args")
                arguments = raw if raw is not None else json.dumps(item.get("args") or {})
                try:
                    parsed = json.loads(arguments) if raw is None else {}
                except ValueError:          # 故意写坏的参数：交给上层容错
                    parsed = {}
                calls.append(
                    ToolCall(
                        id=f"call-{len(self.calls)}-{index}",
                        name=str(item.get("tool") or ""),
                        arguments=parsed if isinstance(parsed, dict) else {},
                        raw_arguments=arguments,
                    )
                )
            return Message(role="assistant", content="", tool_calls=calls)
        return Message(role="assistant", content=str(step.get("text") or ""))


# --------------------------------------------------------------- 断言


def _check(expect: dict[str, Any], *, agent_reply, llm: _ScriptedLLM, elapsed: float) -> list[str]:
    """返回**失败原因**列表（空 = 通过）。"""
    problems: list[str] = []
    called = [inv.tool_name for inv in agent_reply.tool_invocations if inv.ok or inv.error]

    if "tools" in expect:
        want = list(expect["tools"])
        if called != want:
            problems.append(f"工具调用期望 {want}，实际 {called}")

    if "args" in expect:
        for tool_name, want_args in expect["args"].items():
            matched = [
                inv.arguments
                for inv in agent_reply.tool_invocations
                if inv.tool_name == tool_name
            ]
            if not matched:
                problems.append(f"{tool_name} 没有被调用")
                continue
            got = matched[0] or {}
            for key, value in want_args.items():
                if got.get(key) != value:
                    problems.append(f"{tool_name}.{key} 期望 {value!r}，实际 {got.get(key)!r}")

    if "states" in expect:
        want_states = list(expect["states"])
        got_states = list(getattr(agent_reply, "states", ()))
        if got_states != want_states:
            problems.append(f"状态序列期望 {want_states}，实际 {got_states}")

    if "steps" in expect and agent_reply.steps != expect["steps"]:
        problems.append(f"步数期望 {expect['steps']}，实际 {agent_reply.steps}")

    if "model_calls" in expect and len(llm.calls) != expect["model_calls"]:
        problems.append(f"模型调用次数期望 {expect['model_calls']}，实际 {len(llm.calls)}")

    content = agent_reply.content or ""
    for fragment in expect.get("final_contains") or []:
        if fragment not in content:
            problems.append(f"最终回答里缺少 {fragment!r}（实际：{content[:40]!r}）")
    for fragment in expect.get("final_not_contains") or []:
        if fragment in content:
            problems.append(f"最终回答里不该出现 {fragment!r}")

    if expect.get("readable_error") and agent_reply.error is None:
        problems.append("期望是一次模型失败（error 非空），实际没有失败")

    if expect.get("second_call_sees_failure"):
        if len(llm.calls) < 2:
            problems.append("没有第二次模型调用，无法验证'失败已回灌'")
        else:
            flattened = " ".join(
                str(getattr(m, "content", "")) for m in llm.calls[1]
            )
            if "炸" not in flattened and "失败" not in flattened:
                problems.append(f"第二次调用里没有工具失败的信息：{flattened[-80:]!r}")

    if "messages_in_second_call_min" in expect:
        want = int(expect["messages_in_second_call_min"])
        if len(llm.calls) < 2:
            problems.append("没有第二次模型调用")
        elif len(llm.calls[1]) < want:
            problems.append(f"第二次调用的消息数期望 ≥{want}，实际 {len(llm.calls[1])}")

    # 第 200 步（评价15 T3-13）：时间断言。并发的价值只有量出来才算证明 ——
    # 两个 200ms 的工具并发跑应该在 200ms 量级，串行会是 400ms。
    if "max_ms" in expect and elapsed * 1000 > float(expect["max_ms"]):
        problems.append(f"耗时超过上限：{elapsed * 1000:.0f}ms > {expect['max_ms']}ms（并发退化成串行？）")
    if "min_ms" in expect and elapsed * 1000 < float(expect["min_ms"]):
        problems.append(f"耗时低于下限：{elapsed * 1000:.0f}ms < {expect['min_ms']}ms（工具根本没真跑？）")

    return problems


# --------------------------------------------------------------- 跑一场


async def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    from agents.agent import Agent, build_system_prompt, prompt_version
    from core import usage as usage_store
    from core.schemas import Message
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    for name in case.get("registry") or []:
        registry.register(TOOLS[name]())

    llm = _ScriptedLLM(case["script"])
    # 用**线上那份**系统提示（第 123 步）：这样报告里的版本号才有意义 ——
    # 改了提示词，报告里的版本号会变，能对上账本里的同一条。
    system_prompt = build_system_prompt(list(registry.names()))
    agent = Agent(
        llm=llm,
        registry=registry,
        max_steps=int(case.get("max_steps") or 4),
        system_prompt=system_prompt,
        turn_token_budget=int(case.get("budget") or 0),
    )

    turns = case.get("turns") or [case["messages"]]
    messages: list[Any] = []
    usage_store.reset_turn_tokens()
    started = time.monotonic()
    reply = None
    for turn in turns:
        for text in turn:
            messages.append(Message(role="user", content=text))
        reply = await agent.run(messages)
        # 与真实路由一致：只把"用户提问 + 助手最终回答"留在上下文里
        if reply is not None and reply.content:
            messages.append(Message(role="assistant", content=reply.content))
    elapsed = time.monotonic() - started

    problems = _check(case["expect"], agent_reply=reply, llm=llm, elapsed=elapsed)
    return {
        "id": case["id"],
        # 第 124 步：记下**这个场景实际用的**提示词版本（评价8 发现报告头用的是另一个哈希）
        "prompt_version": prompt_version(system_prompt),
        "note": case.get("note", ""),
        "ok": not problems,
        "problems": problems,
        "steps": reply.steps,
        # 第 136 步：这一轮走过的状态序列（plan/act/observe/answer/degrade）
        "states": list(getattr(reply, "states", ())),
        "calls": len(llm.calls),
        "ms": int(elapsed * 1000),
    }


# --------------------------------------------------------------- 报告


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="Agent 端到端场景评测")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--case", default="", help="只跑 id 包含该子串的场景")
    args = parser.parse_args(argv)

    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    cases = [
        case
        for case in data["cases"]
        if args.case in case["id"]
    ]
    if not cases:
        print(f"没有匹配的场景：{args.case!r}")
        return 2

    started = time.monotonic()
    results = [asyncio.run(_run_case(case)) for case in cases]
    elapsed = time.monotonic() - started

    print("=" * 78)
    print("Agent 场景评测（脚本化模型，不花钱）")
    # 这句话必须写在报告里，不能只写在文档里：
    # "任务成功率 100%" 很容易被读成"模型质量 100%"（评价8 的提醒）。
    print("这是**契约测试**：验 Agent 循环不变量（工具选择/参数/回灌/步数/预算），")
    print("不代表真实模型在模糊问题上的准确率 —— 那要跑真模型 e2e（花钱、手动）。")
    # 提示词版本：改一个字就变。这里收集的是**每个场景实际用的**版本号，
    # 不是拿 id 拼一句话再哈希 —— 那与真实运行的提示词无关（评价8 抓到的 bug）。
    versions = sorted({result["prompt_version"] for result in results})
    print(f"提示词版本：{', '.join(versions) if versions else '（无）'}")
    print("=" * 78)
    print(f"  {'场景':<34}{'步数':>5}{'调用':>5}{'耗时':>8}  结论")
    print("-" * 78)
    for result in results:
        mark = "PASS" if result["ok"] else "FAIL"
        print(
            f"  {result['id']:<34}{result['steps']:>5}{result['calls']:>5}"
            f"{result['ms']:>7}ms  {mark}  {result['prompt_version']}"
        )
        if args.verbose and result["states"]:
            print(f"       状态：{' → '.join(result['states'])}")
        if not result["ok"]:
            for problem in result["problems"]:
                print(f"       - {problem}")
        elif args.verbose and result["note"]:
            print(f"       {result['note']}")

    passed = sum(1 for result in results if result["ok"])
    total_steps = sum(result["steps"] for result in results)
    print("-" * 78)
    print(
        f"  任务成功率 {passed}/{len(results)}"
        f"（{passed / len(results) * 100:.0f}%）"
        f"  平均步数 {total_steps / len(results):.1f}"
        f"  总耗时 {elapsed:.2f}s"
    )
    print("=" * 78)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
