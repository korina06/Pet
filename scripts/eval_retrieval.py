"""检索质量评测：跑 golden 与 holdout 查询，输出约束满足、零命中、放宽透明度、
recall@5 与 MRR，并列出不达标用例。

默认只测检索层；`--with-model`、`--vectors` 才会真调模型与嵌入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_PATH = ROOT / "tests" / "golden" / "retrieval_golden.json"
#: 冻结的 holdout（第 75 步）。与上面那份的区别见文件里的 _comment。
HOLDOUT_PATH = ROOT / "tests" / "golden" / "retrieval_holdout.json"
#: v2（第 87 步）：含 exclude_tags 这类"后来才有的能力"，并修正 v1 标错的地方。
#: v1 一个字都不改 —— 它记录的是"还没有那个能力时"的基线。
HOLDOUT_V2_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v2.json"
#: holdout v3（第 189 步 / 评价15 T0-2）：写的时候不看场景词表，跑之前冻结。
#: 与 v1/v2 不同，它**专门**带 37 条真零命中，用来量「该空的时候空不空」。
HOLDOUT_V3_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v3.json"
#: holdout v4（第 248 步）：30 条（15 有答案 + 15 零命中），为「失败率」凑够分母。
HOLDOUT_V4_PATH = ROOT / "tests" / "golden" / "retrieval_holdout_v4.json"

#: 质量门槛（正确性三条是硬门槛，见文件头）
THRESHOLDS = {
    "constraint_satisfaction": 1.0,
    "zero_hit_accuracy": 1.0,
    "relaxation_transparency": 1.0,
    "recall@5": 0.80,
    "mrr": 0.60,
}


def _text(item: dict) -> str:
    return item["name"] + " " + " ".join(item["ingredients"])


def _satisfies(item: dict, must: dict) -> tuple[bool, str]:
    """这条结果是否满足机械约束；不满足时返回原因。"""
    if "tags_all" in must:
        missing = [tag for tag in must["tags_all"] if tag not in item["tags"]]
        if missing:
            return False, f"缺标签 {missing}"
    if "tags_any" in must:
        if not any(tag in item["tags"] for tag in must["tags_any"]):
            return False, f"标签不含 {must['tags_any']}"
    if "text_any" in must:
        if not any(word in _text(item) for word in must["text_any"]):
            return False, f"文本不含 {must['text_any']}"
    if "forbid_text" in must:
        hit = [word for word in must["forbid_text"] if word in _text(item)]
        if hit:
            return False, f"出现了不该有的 {hit}"
    minutes = item["time_minutes"]
    if "max_minutes" in must and minutes is not None and minutes > must["max_minutes"]:
        return False, f"时长 {minutes} 超过 {must['max_minutes']}"
    if "min_minutes" in must and minutes is not None and minutes < must["min_minutes"]:
        return False, f"时长 {minutes} 不足 {must['min_minutes']}"
    return True, ""


def _build_tool(embedder=None, judge=None):
    """内存库 + 种子数据（不碰 data/pet.db）。

    `embedder` 非空时，额外把种子菜谱的向量**现算**进内存库（第 182 步）——
    这样 `--vectors` 量的是真嵌入，而不是读一份可能过期的落盘向量。
    """
    from core.recipe_library import ensure_schema
    from core.recipe_seed import import_seed
    from core.storage import Storage
    from tools.recipe_search_tool import RecipeSearchTool

    db = Storage(":memory:")
    ensure_schema(db)
    import_seed(db)
    if embedder is not None:
        import asyncio

        from core.recipe_library import list_recipes
        from core.recipe_vectors import content_hash, embed_text, save_vector

        recipes = list_recipes(db, limit=1000)
        texts = [embed_text(recipe) for recipe in recipes]
        vectors = asyncio.run(embedder.embed(texts))
        for recipe, text, vector in zip(recipes, texts, vectors):
            save_vector(
                db,
                recipe_id=recipe.id,
                model=getattr(embedder, "model", "unknown"),
                vector=vector,
                digest=content_hash(text),
            )
        print(f"  已为 {len(recipes)} 道菜现算向量（模型 {getattr(embedder, 'model', '?')}）")
    return RecipeSearchTool(db, embedder=embedder, judge=judge), db


#: 评测**全程**共用一个事件循环（第 189 步修）。
#:
#: 为什么：`httpx.AsyncClient` 第一次请求时绑定当前 loop，之后换 loop 就会
#: `Event loop is closed`。而原来每条用例都 `asyncio.run(...)`（= 每次新建一个
#: loop 再关掉），于是第二条用例起，嵌入器和裁决者的每次调用都在抛这个异常 ——
#: 嵌召回返回空、裁决 fail-closed 成「都不合适」，**向量路整条被静默跳过**，
#: 评测看起来"跑通了"，实际上量的是纯字面检索。
_LOOP = None


def _shared_loop():
    global _LOOP
    import asyncio

    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def _run_case(
    tool, case: dict, extra: dict | None = None, *, with_query_text: bool = False
) -> dict:
    args = dict(case["args"])
    # 第 182 步：走向量路时把**用户原话**交给工具（语义召回要用原话，
    # 这正是评价14 v1 缺的那块）
    if with_query_text:
        args["query_text"] = case["query"]
    if extra:
        args.update(extra)
    result = _shared_loop().run_until_complete(tool.run(**args))
    if not result.ok:
        return {"id": case["id"], "error": result.error, "items": [], "payload": {}}
    payload = result.as_payload()
    return payload


def _build_library() -> list[dict]:
    """把种子读成评测用的"菜谱事实"列表（不落库）。"""
    from core.recipe_seed import load_seed

    return [
        {
            "name": dish["name"],
            "tags": dish["tags"],
            "time_minutes": dish["time_minutes"],
            "ingredients": dish["ingredients"],
        }
        for dish in load_seed()
    ]


def _relevant(library: list[dict], must: dict) -> list[str]:
    """机械算出的"相关集"：库里所有满足 must 的菜。

    **为什么不用用例里手写的 expect 算 recall**：
    "想吃猪肉"这种宽泛查询，满足条件的可能有二三十道，手写 3 道当"标准答案"
    是武断的——排序里出现别的猪肉菜不算错。所以 recall 的分母由约束算出来。
    """
    out = []
    for dish in library:
        ok, _ = _satisfies(dish, must)
        if ok:
            out.append(dish["name"])
    return out


def _hand_metrics(names: list[str], expect: list[str], expect_all: bool) -> dict:
    """按**人工标注**算命中，而不是按机械约束算（第 75 步）。

    为什么必须换一套算法：另一份 golden 的 recall 分母是 `must` 机械算出来的
    相关集，而 `must` **同时又是工具过滤用的谓词** —— 那等于"用同一条条件
    衡量同一条条件"，数字再高也说明不了"用户会不会满意"。

    这里换成人工写的 `expect`：

    - ``hit@5``：前 5 条里有没有**任何一道**人工认可的答案；
    - ``precision@5``：前 5 条里有多少落在 ``expect`` 里。只有 ``expect_all``
      为 true 时才算（那份清单声明是"全部应得答案"），否则是冤枉。
    """
    top = names[:5]
    hits = [name for name in top if name in expect]
    precision = None
    if expect_all and expect and top:
        # 分母是**实际给出的前 5 条**，不是 len(expect)。
        # 第一版写成 min(5, len(expect))，结果"expect 只有 1 道菜"的用例
        # 只要命中 1 条就算 1.00 —— 把"另外 4 条并不合格"整块藏了起来。
        precision = len(hits) / len(top)
    return {"hit_at_5": 1.0 if hits else 0.0, "precision_at_5": precision}


def _load_cases(include_v3: bool = False, include_v4: bool = False) -> list[dict]:
    """读几份用例，并各自打上 split（dev / holdout / holdout_v2 / holdout_v3 / v4）。

    v3/v4 默认**不**读：它们是盲测集，只有在显式 `--v3` / `--v4` 时才掺进来 ——
    否则平时跑 dev 也会顺带把答案摊在屏幕上，盲测就废了。
    """
    out: list[dict] = []
    paths = [
        (GOLDEN_PATH, "dev"),
        (HOLDOUT_PATH, "holdout"),
        (HOLDOUT_V2_PATH, "holdout_v2"),
    ]
    if include_v3:
        paths.append((HOLDOUT_V3_PATH, "holdout_v3"))
    if include_v4:
        paths.append((HOLDOUT_V4_PATH, "holdout_v4"))
    for path, split in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data["cases"]:
            tagged = dict(case)
            tagged["split"] = data.get("split") or split
            out.append(tagged)
    return out


def evaluate(
    verbose: bool | None = None,
    embedder=None,
    judge=None,
    v3: bool = False,
    v4: bool = False,
) -> int:
    """跑一遍评测。`verbose=None` 时看环境变量 `EVAL_VERBOSE=1`（第 180 步）。

    为什么加这个开关：语义用例只看到一个 hit/miss 分不清"真的搜到了"还是
    "关键词被放宽链丢掉后撞到一条"——必须能逐条看前 5 名。
    """
    if verbose is None:
        import os

        verbose = os.environ.get("EVAL_VERBOSE", "").strip() in {"1", "true", "yes"}
    tool, db = _build_tool(embedder=embedder, judge=judge)
    library = _build_library()
    cases = _load_cases(include_v3=v3, include_v4=v4)

    rows = []
    for case in cases:
        payload = _run_case(tool, case, with_query_text=embedder is not None)
        items = payload.get("items", [])
        must = case.get("must") or {}

        # 约束满足率：返回项里满足 must 的比例
        ok_count = 0
        failures = []
        for item in items:
            ok, why = _satisfies(item, must)
            if ok:
                ok_count += 1
            else:
                failures.append((item["name"], why))
        if case.get("expect_zero"):
            satisfaction = 1.0 if not items else 0.0
        else:
            satisfaction = (ok_count / len(items)) if items else 0.0

        # 相关集：由 must 机械算出（手写的 expect 仅用于自检）
        relevant = _relevant(library, must)
        narrow = 0 < len(relevant) <= 12

        names = [item["name"] for item in items]
        recall = mrr = None
        if narrow and not case.get("expect_zero"):
            top = names[:5]
            hits = [name for name in top if name in relevant]
            recall = len(hits) / min(5, len(relevant))
            ranks = [names.index(name) + 1 for name in hits]
            mrr = 1 / min(ranks) if ranks else 0.0

        # 零命中：该空的时候空了没有
        zero_ok = None
        if case.get("expect_zero"):
            zero_ok = payload.get("count", 0) == 0

        # 放宽透明度：注定要放宽的用例，必须给出 relaxed
        relax_ok = None
        if case.get("expect_relaxed"):
            relax_ok = bool(payload.get("relaxed"))

        # 自检：手写的 expect 必须落在机械相关集里（否则是 golden 写错了）
        expect_problems = [
            name for name in (case.get("expect") or []) if name not in relevant
        ]

        hand = _hand_metrics(names, case.get("expect") or [], bool(case.get("expect_all")))

        # 第 82 步：holdout 另跑一遍「**当前行为**」。
        # 冻结基线的 args 是写死的，不含后来才有的能力（例如第 77 步的 exclude_tags），
        # 所以 0.20/0.80 那两个数字**不代表现在**。这里把服务端会注入的东西
        # （从这句话里抽出的否定口味）加进去，另算一列，冻结基线本身**不动**。
        current = None
        if case["split"] == "holdout":
            from core.recipes import extract_negative_taste

            injected = extract_negative_taste(case["query"])
            if injected:
                payload_now = _run_case(
                    tool,
                    case,
                    extra={"exclude_tags": injected},
                    with_query_text=embedder is not None,
                )
                now_names = [item["name"] for item in payload_now.get("items", [])]
                current = _hand_metrics(
                    now_names, case.get("expect") or [], bool(case.get("expect_all"))
                )
                current["count"] = payload_now.get("count", 0)
                current["injected"] = injected

        rows.append({
            "split": case["split"],
            # 第 180 步：语义用例按类分组（synonym / descriptive / method），
            # 好回答"到底哪一类在失败"—— 不分类只看到一个总分，做不出取舍。
            "category": case.get("category", ""),
            "hit_at_5": hand["hit_at_5"],
            "precision_at_5": hand["precision_at_5"],
            "expect_all": bool(case.get("expect_all")),
            "current": current,
            "note": case.get("note", ""),
            "id": case["id"],
            "query": case["query"],
            "count": payload.get("count", 0),
            "satisfaction": satisfaction,
            "recall": recall,
            "mrr": mrr,
            "zero_ok": zero_ok,
            "relax_ok": relax_ok,
            "failures": failures,
            "relaxed": payload.get("relaxed"),
            "names": names,
            "relevant_size": len(relevant),
            "narrow": narrow,
            "expect_problems": expect_problems,
        })

    db.close()
    return _report(rows, verbose)


def _report_holdout_v3(
    rows: list[dict],
    verbose: bool,
    *,
    title: str = "holdout v3（盲测：写时不看词表、跑前冻结，只报告不设门槛）",
) -> None:
    """盲测集：只报告，不设门槛（第 189 步 v3 / 第 248 步 v4 共用）。

    与 v1/v2 的区别是**这一份专门量"该空的时候空不空"** —— 有真零命中的用例，
    库里确实没有那道菜。所以指标拆成两半：

      * 有答案的（expect 非空）：hit@5 / P@5，看搜得到搜不到；
      * 没答案的（expect_zero）：**零命中正确率**，看会不会硬凑一条。
    """
    semantic = [row for row in rows if not row.get("zero_ok") is not None]
    zero = [row for row in rows if row.get("zero_ok") is not None]

    print("\n" + "=" * 82)
    print(title)
    print("=" * 82)
    print(
        "⚠️ 这一份**跑完就是基线**。难看也不要回头调场景词表/阈值 —— 那会立刻把它变成",
    )
    print("   第二份 v2（被调过的 holdout 只能证明「这些能力覆盖到位」，不是泛化证据）。")
    print("=" * 82)

    if semantic:
        hits = [row["hit_at_5"] for row in semantic]
        precisions = [
            row["precision_at_5"]
            for row in semantic
            if row["precision_at_5"] is not None
        ]
        print(f"\n  【有答案】{len(semantic)} 条  命中 {sum(1 for h in hits if h)}/{len(hits)}"
              f"  hit@5 {sum(hits) / len(hits):.3f}")
        if precisions:
            print(f"           P@5 {sum(precisions) / len(precisions):.3f}"
                  f"（{len(precisions)} 条可算）")
        for row in semantic:
            mark = "✓" if row["hit_at_5"] else "✗"
            print(f"    {mark} {row['id']:<24} {row['query'][:24]}")
            if verbose:
                print(f"        前 5：{row['names'][:5]}")

    if zero:
        ok = [row for row in zero if row["zero_ok"]]
        print(f"\n  【零命中】{len(zero)} 条  正确（返回空）{len(ok)}/{len(zero)}"
              f"  正确率 {len(ok) / len(zero):.3f}")
        for row in zero:
            mark = "✓" if row["zero_ok"] else "✗"
            print(f"    {mark} {row['id']:<24} {row['query'][:24]}")
            if not row["zero_ok"]:
                print(f"        ✗ 硬凑了 {row['count']} 条：{row['names'][:5]}")
    print("=" * 82)


def _report_holdout(
    rows: list[dict],
    verbose: bool,
    *,
    title: str = "holdout（冻结集，只报告不设门槛）",
    caveat: str = "",
) -> None:
    """holdout **只报告，不设门槛**（第 75 步）。

    为什么不设门槛：门槛要先有基线。第一次跑出来的数字就是基线，
    如果它难看，正确的反应是**记下来**，不是回去调检索逻辑
    （那样它立刻就不是 holdout 了）。

    指标全部来自人工标注的 `expect`，与工具内部用的 `must` 谓词**无关**。
    """
    if not rows:
        print("\n  （没有 holdout 用例：检查 tests/golden/retrieval_holdout.json）")
        return

    print("\n" + "=" * 82)
    print(title)
    print("=" * 82)
    has_current = any(row.get("current") for row in rows)
    if caveat:
        print(caveat)
    if has_current:
        print(f"{'用例':<26}{'基线P@5':>8}{'当前P@5':>8}  说明")
    else:
        print(f"{'用例':<26}{'P@5':>8}{'hit@5':>7}  说明")
    print("-" * 82)
    for row in rows:
        p5 = "—" if row["precision_at_5"] is None else f"{row['precision_at_5']:.2f}"
        note = row["note"]
        if has_current:
            current = row.get("current")
            now = "—"
            if current and current.get("precision_at_5") is not None:
                now = f"{current['precision_at_5']:.2f}"
            if current:
                note = f"[注入 {current['injected']}] " + note
            print(f"{row['id']:<26}{p5:>8}{now:>8}  {note}")
        else:
            print(f"{row['id']:<26}{p5:>8}{row['hit_at_5']:>7.0f}  {note}")
        if verbose:
            print(f"    前 5：{row['names'][:5]}")
    print("-" * 82)

    hits = [row["hit_at_5"] for row in rows]
    precisions = [row["precision_at_5"] for row in rows if row["precision_at_5"] is not None]
    hit_rate = sum(hits) / len(hits) if hits else 0.0
    print(f"  hit@5 均值 {hit_rate:.3f}（{sum(1 for h in hits if h)} / {len(hits)} 条命中）")
    if precisions:
        print(f"  基线 P@5  {sum(precisions) / len(precisions):.3f}（{len(precisions)} 条可算）")

    now_values = [
        row["current"]["precision_at_5"]
        for row in rows
        if row.get("current") and row["current"].get("precision_at_5") is not None
    ]
    if now_values:
        print(
            f"  当前 P@5  {sum(now_values) / len(now_values):.3f}"
            f"（{len(now_values)} 条受服务端注入影响）"
        )
        print("    左列是**冻结基线**（args 写死，不含后来才有的能力）；")
        print("    右列是**当前行为**（额外注入服务端从这句话里抽出的否定口味）。")
    print("  基线不是门槛；难看也不要回头调检索——那会毁掉 holdout。")
    _report_by_category(rows)
    print("=" * 82)


#: 分类的中文说明 + 典型例子（第 180 步，评价14 阶段 0）
CATEGORY_LABELS = {
    "synonym": ("同物异名", '用户说「西红柿」，库里写「番茄」'),
    "descriptive": ("描述型", '用户说「下酒菜 / 牙口不好」，标签里没有这些词'),
    "method": ("功效/做法语义", '用户说「发汗」，菜谱正文里没有这两个字'),
    "expanded": ("扩展后（对照组）", '假设模型给对了 search_terms，看检索层接不接得住'),
}


def _report_by_category(rows: list[dict]) -> None:
    """按类汇总语义用例（只对带 category 的用例生效）。"""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("category"):
            grouped.setdefault(row["category"], []).append(row)
    if not grouped:
        return

    print()
    print("  按类汇总（语义用例）：")
    for key in ("synonym", "descriptive", "method", "expanded"):
        items = grouped.get(key)
        if not items:
            continue
        label, hint = CATEGORY_LABELS.get(key, (key, ""))
        misses = [row for row in items if not row["hit_at_5"]]
        precisions = [
            row["precision_at_5"] for row in items if row["precision_at_5"] is not None
        ]
        hit_rate = sum(row["hit_at_5"] for row in items) / len(items)
        head = f"    {label:<10} {len(items) - len(misses)}/{len(items)} 命中（{hit_rate:.0%}）"
        if precisions:
            head += f"，P@5 {sum(precisions) / len(precisions):.2f}"
        print(head + f"  —— {hint}")
        for row in misses:
            print(f"      未命中：{row['query']}  -> 前 5 {row['names'][:3]}")


def _report(rows: list[dict], verbose: bool) -> int:
    all_rows = rows
    rows = [row for row in rows if row["split"] == "dev"]
    print("=" * 82)
    print("检索质量评测（dev / 回归集）")
    print("⚠️ 这一节的 recall/MRR 分母由 `must` 机械算出，而 `must` 同时是工具的过滤条件 ——")
    print("   它只能说明\"实现与用例自洽\"，**不能当质量结论引用**。质量看下面的 holdout。")
    print("=" * 82)
    print(f"{'用例':<26}{'相关':>5}{'结果':>5}{'约束':>7}{'recall':>8}{'MRR':>7}  说明")
    print("-" * 82)

    for row in rows:
        recall = "—" if row["recall"] is None else f"{row['recall']:.2f}"
        mrr = "—" if row["mrr"] is None else f"{row['mrr']:.2f}"
        note = ""
        if row["zero_ok"] is not None:
            note = "零命中 ✓" if row["zero_ok"] else "零命中 ✗"
        elif row["relax_ok"] is not None:
            note = "放宽已说明 ✓" if row["relax_ok"] else "放宽未说明 ✗"
        elif not row["narrow"]:
            note = "宽泛查询（只查约束）"
        if row["expect_problems"]:
            note += f" ⚠️ golden 期望不在相关集：{row['expect_problems']}"
        print(
            f"{row['id']:<26}{row['relevant_size']:>5}{row['count']:>5}"
            f"{row['satisfaction']:>7.2f}{recall:>8}{mrr:>7}  {note}"
        )
        if verbose and row["failures"]:
            for name, why in row["failures"][:5]:
                print(f"    ✗ {name}：{why}")
        if verbose and row["relaxed"]:
            print(f"    ↺ 放宽：{row['relaxed']}")

    print("-" * 82)

    def mean(values):
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else 1.0

    metrics = {
        "constraint_satisfaction": mean(row["satisfaction"] for row in rows),
        "zero_hit_accuracy": mean(
            [1.0 if row["zero_ok"] else 0.0 for row in rows if row["zero_ok"] is not None]
        ),
        "relaxation_transparency": mean(
            [1.0 if row["relax_ok"] else 0.0 for row in rows if row["relax_ok"] is not None]
        ),
        # recall/MRR 只在"窄查询"上算：宽泛查询的相关集太大，前 5 条不构成质量信号
        "recall@5": mean(row["recall"] for row in rows),
        "mrr": mean(row["mrr"] for row in rows),
    }

    narrow_count = sum(1 for row in rows if row["narrow"])
    failed = []
    for name, value in metrics.items():
        threshold = THRESHOLDS[name]
        ok = value >= threshold - 1e-9
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<26} {value:.3f}  （门槛 {threshold:.2f}）")
        if not ok:
            failed.append(name)

    golden_errors = [row for row in rows if row["expect_problems"]]
    print(f"\n  用例：{len(rows)} 条，其中可算 recall 的窄查询 {narrow_count} 条")
    print("=" * 82)
    if golden_errors:
        print("✗ golden 用例自身有问题（期望的菜不在机械相关集里）：")
        for row in golden_errors:
            print(f"    {row['id']}: {row['expect_problems']}")
        return 1
    if failed:
        print(f"✗ 未达标：{', '.join(failed)}")
        print("  提示：约束满足率/零命中/放宽透明度是**正确性**，必须 100%。")
        return 1
    print(f"✓ dev 门槛全部达标（{len(rows)} 条用例）—— 仅代表与用例自洽")
    _report_holdout(
        [row for row in all_rows if row["split"] == "holdout"],
        verbose,
        title="holdout v1（冻结基线：先于 exclude_tags，只报告不设门槛）",
    )
    _report_holdout(
        [row for row in all_rows if row["split"] == "holdout_v2"],
        verbose,
        title="holdout v2（当前能力：含 exclude_tags 与修正后的标注）",
        caveat=(
            "⚠️ 这一组是**看过实现之后**写的，而且 expect 由种子事实推出 ——\n"
            "   所以它的满分只能说明「这些能力覆盖到位」，**不能当泛化证据**。\n"
            "   真正的盲测要靠将来新写的用例；v1 才是未经修订的基线。"
        ),
    )
    v3_rows = [row for row in all_rows if row["split"] == "holdout_v3"]
    if v3_rows:
        _report_holdout_v3(v3_rows, verbose)
    v4_rows = [row for row in all_rows if row["split"] == "holdout_v4"]
    if v4_rows:
        _report_holdout_v3(
            v4_rows,
            verbose,
            title="holdout v4（30 条盲测：15 有答案 + 15 真零命中，只报告不设门槛）",
        )
    return 0


#: 这几个字段是**集合**：顺序无所谓，缺省当空集
_SET_FIELDS = ("tags", "ingredients", "exclude", "exclude_tags")
#: 这几个是**数值**：缺省当 None
_INT_FIELDS = ("min_minutes", "max_minutes")
#: 这几个是**字符串**：忽略首尾空白
_TEXT_FIELDS = ("keyword",)


def _args_match(expected: dict, actual: dict | None) -> tuple[bool, str]:
    """模型给的参数与期望是否一致；不一致时返回原因（第 86 步 / B2）。

    以前是"每个期望字段都精确相等"：list 的**顺序**不同就算错，
    `None` 和 `[]` 也不等价 —— 偏严，于是那个数字只能当噪声看。

    现在按字段类型判：
    - 集合字段（tags/ingredients/exclude/exclude_tags）：**比集合**，不看顺序，
      缺失当空集；
    - 数值字段（min/max_minutes）：比数值，缺失当 None；
    - 文本字段（keyword）：去空格后比；
    - **多余字段不罚**：模型多传一个参数是多余，不是错。
    """
    if not isinstance(actual, dict):
        return False, "模型没有调用检索工具"

    problems: list[str] = []
    for name in _SET_FIELDS:
        if name not in expected:
            continue
        want = {str(item).strip() for item in (expected.get(name) or []) if str(item).strip()}
        got = {str(item).strip() for item in (actual.get(name) or []) if str(item).strip()}
        if want != got:
            problems.append(f"{name}: 期望 {sorted(want)} 实际 {sorted(got)}")

    for name in _INT_FIELDS:
        if name not in expected:
            continue
        want = expected.get(name)
        got = actual.get(name)
        if want != got:
            problems.append(f"{name}: 期望 {want} 实际 {got}")

    for name in _TEXT_FIELDS:
        if name not in expected:
            continue
        want = str(expected.get(name) or "").strip()
        got = str(actual.get(name) or "").strip()
        if want != got:
            problems.append(f"{name}: 期望 {want!r} 实际 {got!r}")

    return (not problems, "；".join(problems))


def evaluate_model_arguments(verbose: bool = False) -> int:
    """可选：让真模型拆参数，看它和 golden 的 args 差多少（会花钱）。"""
    import asyncio

    tool, db = _build_tool()
    from agents.agent import Agent, build_recipe_prompt
    from agents.deepseek_llm import DeepSeekLLM
    from core.schemas import Message
    from tools.registry import build_default_registry

    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]
    registry = build_default_registry(storage=db)
    llm = DeepSeekLLM()
    agent = Agent(
        llm=llm, registry=registry, max_steps=3,
        system_prompt=build_recipe_prompt(),
    )

    matched = 0
    for case in cases:
        reply = asyncio.run(agent.run([Message(role="user", content=case["query"])]))
        actual = None
        for invocation in reply.tool_invocations:
            if invocation.tool_name == "search_recipes":
                actual = invocation.arguments
                break
        same, reason = _args_match(case["args"], actual)
        if same:
            matched += 1
        elif verbose:
            print(f"  {case['id']}: {reason}")

    asyncio.run(llm.aclose())
    db.close()
    rate = matched / len(cases)
    print(f"\n模型参数正确率：{rate:.2%}（{matched}/{len(cases)}）—— 只作趋势观察，不设门槛")
    return 0


def main(argv: list[str]) -> int:
    # 第 103 步：和 import_recipe_md 的教训一样 —— **不要替换 sys.stdout**，
    # 原地改编码即可（替换掉会让调用方的捕获流失效）。
    from core.console import force_utf8

    force_utf8()
    verbose = "-v" in argv or "--verbose" in argv
    embedder = None
    judge = None
    if "--vectors" in argv:
        from core.embeddings import build_embedder

        embedder = build_embedder()
        if embedder is None:
            raise SystemExit(
                "--vectors 需要可用的嵌入器：检查 .env 的 GLM_API_KEY / EMBEDDING_ENABLED"
            )
        from agents.deepseek_llm import DeepSeekLLM
        from core.semantic_judge import LlmJudge

        # 评测基建不许有方差（第 202 步 / 评价16）：
        # ① 裁决者固定 DeepSeek —— 不跟随界面上当前选中的 provider（否则同一份代码
        #    在不同机器/不同窗口设置上给出不同数字，简历里那句 hit@5 就成了天气）；
        # ② 温度固定 0 —— 之前不传温度（服务端默认），同一份代码复跑出现了 ±1 条的漂移。
        judge = LlmJudge(DeepSeekLLM(temperature=0.0))
        print(f"语义召回：开启（向量 {embedder.model} 召回 + 模型裁决，查询用用户原话）")
        print("  裁决者固定 deepseek + temperature=0：评测要可复现，不跟随界面里选的模型。")
        print(
            "  注意：向量**只做召回**，要不要由模型裁决（它可以直说「都不合适」）。"
            "没有裁决者时工具会退化为纯字面检索。"
        )
    code = evaluate(
        verbose=verbose,
        embedder=embedder,
        judge=judge,
        v3="--v3" in argv,
        v4="--v4" in argv,
    )
    if "--with-model" in argv:
        evaluate_model_arguments(verbose=verbose)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
