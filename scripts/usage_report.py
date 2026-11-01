"""命令行查看用量账本：按天或按功能统计调用次数与 token，以及最近若干次调用。只读，不写库。

`--days` / `--feature` / `--recent` / `--stats`；数据来自 `llm_usage` 表。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.console import force_utf8  # noqa: E402
from core.storage import Storage, resolve_db_path  # noqa: E402


def _human(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _round_up(value: int, step: int) -> int:
    """往上取整到 step 的倍数 —— 阈值写成 15000 比 14782 好读，也不该因此变小。"""
    if value <= 0:
        return 0
    return ((int(value) + step - 1) // step) * step


def _print_stats(data: dict) -> None:
    """把分布打印出来，并给出**有依据**的阈值建议（第 210 步）。

    建议的算法故意简单可复核：
    - 单轮：`max(P95 × 1.5, 最坏一轮 × 1.2)` 向上取整到 1000。
      为什么还要看最坏那一轮：预算是**硬停止**（超了就不发下一次调用），
      只按 P95 定会把真实发生过的正常长轮截断 —— 那是拿体验换省钱，不划算。
    - 单日：`最坏的一天 × 1.05` 向上取整到 10000。日告警的意义是「今天明显不正常」，
      阈值必须**高于**历史最坏的一天，否则每跑一次重活就喊一次，很快就没人看了。
    """
    print()
    print("=== 分布（定预算用）===")
    if not data["calls"]:
        print("  这几天没有记录")
        return

    print(f"  调用 {data['calls']} 次 / {data['turns']} 轮 / prompt 占比 {data['prompt_share']:.0%}")
    for label, key, hint in (
        ("单次调用", "per_call", "对应 MAX_OUTPUT_TOKENS 那一层"),
        ("单轮合计", "per_turn", "对应 TURN_TOKEN_BUDGET"),
        ("单日合计", None, "对应 DAILY_TOKEN_WARN"),
    ):
        if key:
            row = data[key]
            print(
                f"  {label}：n={row['n']:<4} P50={_human(row['p50']):>6}"
                f" P90={_human(row['p90']):>6} P95={_human(row['p95']):>6}"
                f" 最大={_human(row['max']):>6}   （{hint}）"
            )
        else:
            print(
                f"  {label}：{len(data['per_day'])} 天中最多的一天 {_human(data['max_day'])}"
                f"（{data['max_day_date']}）   （{hint}）"
            )

    print()
    print("=== 每天 ===")
    for day in data["per_day"]:
        print(f"  {day['date']}  {_human(day['total']):>8}  {day['calls']:>3} 次")

    turn = _round_up(max(data["per_turn"]["p95"] * 1.5, data["per_turn"]["max"] * 1.2), 1000)
    day_warn = _round_up(data["max_day"] * 1.05, 10000)
    driven = data["calls"] >= 20 and len(data["per_day"]) >= 3
    print()
    print("=== 按这些数字该怎么填 .env ===")
    if not driven:
        # 三个 probe 调用也能算出一串漂亮的数字，但那不叫依据。宁可先说清楚样本不够。
        print(
            f"  样本太少（{data['calls']} 次调用 / {len(data['per_day'])} 天），"
            "下面的数只能当参考，别照着定"
        )
    print(f"  TURN_TOKEN_BUDGET = {turn or 0}        # P95 {_human(data['per_turn']['p95'])} × 1.5，且不低于最坏一轮 × 1.2")
    print(f"  DAILY_TOKEN_WARN  = {day_warn or 0}       # 最坏的一天 {_human(data['max_day'])} × 1.05，只告警不拒绝")
    print("  （改完重启一次就生效；两个都是「软」预算，不会截断正常对话）")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="模型用量账本报告")
    parser.add_argument("--days", type=int, default=1, help="最近几天（含今天），默认 1")
    parser.add_argument("--feature", default="", help="只看某个功能：chat / recipe / probe")
    parser.add_argument("--recent", type=int, default=0, help="另外列出最近 N 次调用")
    parser.add_argument(
        "--prompt", default="",
        help="只看某个提示词版本（第 123 步；版本号见脚本 eval_agent 的报告头）",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="看分布（P50/P95/最大值）并按实测给出预算阈值建议（第 210 步）",
    )
    args = parser.parse_args(argv)

    from core import usage

    db_path = resolve_db_path()
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 1

    storage = Storage(db_path)
    try:
        days = max(1, int(args.days))
        # 与账本/接口一致：按**本地**零点切日（第 114 步）
        since = usage.day_start_utc(days - 1)
        # 修（第 123 步）：`day_start_utc` 返回的是**字符串**，
        # 这里原来还调 `.isoformat()` —— 从第 114 步起这个命令一直是崩的，
        # 而当时没有任何测试跑过 CLI。现在有了（tests/test_usage_cli.py）。
        data = usage.summary(
            storage, since=since,
            feature=args.feature or None,
            prompt_version=args.prompt or None,
        )

        scope = f"最近 {days} 天" + (f"（只看 {args.feature}）" if args.feature else "")
        print(f"=== 用量：{scope} ===")
        if not data["features"]:
            print("  还没有记录（账本是第 111 步才有的，之前的调用不会补记）")
        else:
            print(f"  {'功能':<8}{'提供方':<10}{'模型':<18}{'调用':>6}{'prompt':>10}{'输出':>8}{'合计':>9}")
            for row in data["features"]:
                note = f"  （{row['missing']} 次没有 usage）" if row["missing"] else ""
                print(
                    f"  {row['feature']:<8}{row['provider']:<10}{row['model']:<18}"
                    f"{row['calls']:>6}{_human(row['prompt']):>10}"
                    f"{_human(row['completion']):>8}{_human(row['total']):>9}{note}"
                )
            total = data["total"]
            print(
                f"  {'合计':<8}{'':<10}{'':<18}{total['calls']:>6}"
                f"{_human(total['prompt']):>10}{_human(total['completion']):>8}"
                f"{_human(total['total']):>9}"
            )

        if args.recent:
            print()
            print(f"=== 最近 {args.recent} 次调用 ===")
            for row in usage.recent(storage, args.recent):
                total = "-" if row["total"] is None else _human(row["total"])
                reason = f" finish={row['finish_reason']}" if row["finish_reason"] else ""
                print(
                    f"  {row['created_at']}  {row['feature']:<7} {row['model']:<16}"
                    f" step={row['step']} turn={row['turn_id'][:8] or '-'}"
                    f" total={total} {row['usage_source']}{reason}"
                )

        if args.stats:
            _print_stats(usage.stats(storage, days=days, feature=args.feature or None))
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
