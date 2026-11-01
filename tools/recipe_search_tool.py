"""本地菜谱库检索工具：按菜名/标签/主料/时长/忌口查菜，并在查不到时走放宽链。

排序按相关度，同分时用固定随机打散——同一句话结果稳定，换问法才有不同组合。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Sequence

from core import recipe_vectors
from core.recipe_scene_terms import describe_scene, expand_scene_terms
from core.embeddings import Embedder
from core.recipe_library import MAX_LIMIT, Recipe, list_recipes, search_recipes
from core.recipes import (
    RELAX_ORDER,
    expand_ingredient,
    expand_tags,
    expand_term,
    group_tags_by_dimension,
)
from core.schemas import ToolResult
from core.storage import Storage
from tools.base import BaseTool

logger = logging.getLogger(__name__)

#: 一次最多回几道菜。给多了模型会挑花眼，也给上下文添负担。
MAX_RESULTS = 6

#: 每步做法截断长度：真实做法可能很长，超长的会让工具结果吃掉上下文。
STEP_CHARS = 80

#: 一次最多扫多少条候选（个人菜谱库是几十到几千条，全量扫可接受）。
SCAN_LIMIT = 500

#: 向量路一次取多少个候选（第 188 步）。给裁决者看的量：10 个足够覆盖语义相近的菜。
VECTOR_LIMIT = 10

#: 去中心之后的余弦下限（**只是预筛**，真正的"要不要"由裁决者决定）。
#: 实测去中心后真命中在 0.12~0.26、"库里没有"在 0.20 —— 两者重叠，
#: 所以这里取得很低（0.10），只用来砍掉明显不相干的一大批，不承担判断责任。
VECTOR_MIN_SCORE = 0.10

#: 中文没有词边界，"主料是鸡"这类判断必须排掉这些**假朋友**。
#:
#: 实测过的误伤：
#: - 单字「鸡」命中「鸡蛋」「鸡汤」「鸡精」→ 海蛎煎、刀削面、佛跳墙全成了"鸡肉菜"；
#: - 「鱼」命中「鱼香」（鱼香肉丝里**没有鱼**）、「鱼露」（调料）。
#: 所以别名命中后要看一眼上下文：落在这些词里就不算。
ALIAS_FALSE_FRIENDS: dict[str, tuple[str, ...]] = {
    "鸡": ("鸡蛋", "鸡汤", "鸡精", "鸡毛菜", "鸡枞", "鸡粉"),
    "鸭": ("鸭蛋",),
    "鱼": ("鱼香", "鱼露", "鱼豆腐", "鱼丸", "鱼腥草"),
    "牛": ("牛油果", "牛油", "牛至"),
    "猪": ("猪油",),
    "羊": ("羊栖菜",),
    "虾": ("虾皮",),
}


def _scan_candidates(storage: Storage, keyword: str) -> list:
    """取候选集，**分页取满 `SCAN_LIMIT`**，而不是只取第一页。

    为什么必须分页（第 66 步修）：`list_recipes` / `search_recipes` 内部会把
    limit 夹到 `MAX_LIMIT`（200），所以直接传 `SCAN_LIMIT = 500` 实际只拿到 200 条。
    菜谱库一旦超过 200 条，排在后面（更旧）的菜就被**静默漏掉**：
    不报错、查不到，看起来像"库里没有"。

    取不满一页就说明到底了，立刻停——不要为了凑满 500 条空转。
    """
    rows: list = []
    offset = 0
    while len(rows) < SCAN_LIMIT:
        want = min(MAX_LIMIT, SCAN_LIMIT - len(rows))
        page = (
            search_recipes(storage, keyword, limit=want, offset=offset)
            if keyword
            else list_recipes(storage, limit=want, offset=offset)
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < want:
            break
    return rows

def _recipe_payload(recipe: Recipe) -> dict[str, Any]:
    """一道菜 → 给模型看的精简结构。

    **食材不截断**：工具是按完整食材匹配的，只回前 8 项会导致
    "为什么它命中了"在结果里看不出来。评测时抓到过实例：某道菜是因为
    第 9 项食材里的"鹌鹑蛋"命中的，但返回里看不到，看起来像误命中。

    **做法只留第一步**（第 211 步）：原来留 8 步 × 80 字 ≈ 640 字，
    但提示词第 4 条明说"不要复述完整做法——做法会显示在卡片里"，
    模型拿到的那 640 字一次也没用上（实测占工具结果的 29%）。
    留第一步是为了还能答"这菜大概怎么弄"；完整做法前端点开卡片就有。
    """
    return {
        "id": recipe.id,
        "name": recipe.name,
        "time_minutes": recipe.time_minutes,
        "tags": list(recipe.tags),
        "ingredients": list(recipe.ingredients),
        "steps": [step[:STEP_CHARS] for step in recipe.steps][:1],
        "source": recipe.source,
    }


#: 紧凑文本里的字段分隔符。用全角竖线：中文内容里不会出现，模型也看得清列。
_FIELD_SEP = "｜"


def _model_content(payload: dict[str, Any]) -> str:
    """把结构化结果压成**给模型看的紧凑文本**（第 211 步）。

    ## 为什么不再直接 dump JSON

    实测一次 6 道菜的检索结果 JSON 是 2,184–2,366 字，其中
    `{"id":…,"name":…,"time_minutes":…,"tags":…,"ingredients":…,"steps":…,"source":…}`
    这套键名每道菜重复一遍 ≈ 700 字，全是模型不需要的语法噪音。
    压成"一行一道菜"后同一个结果约 860 字（**−60%**），
    而模型要的东西一样没少：菜名、时间、标签、食材、做法首步。

    ## 为什么不顺手把结构删掉

    卡片要靠 `items[].id` 渲染（`api/recipe_routes.py` 的 `_mentioned_ids`），
    所以结构化数据挪到 `ToolResult.data` 里（`core/schemas.py` 早就写明
    "data 是给代码看的原始字段"），`content` 专心服务模型。两边各取所需。
    """
    items = payload.get("items") or []
    if not items:
        lines: list[str] = ["共 0 道。"]
    else:
        lines = ["共 %d 道（每行：序号. 菜名%s分钟%s标签%s食材%s做法首步）"
                 % (len(items), _FIELD_SEP, _FIELD_SEP, _FIELD_SEP, _FIELD_SEP)]
    for index, row in enumerate(items, start=1):
        parts = [
            str(row.get("name") or ""),
            str(row.get("time_minutes") or ""),
            ",".join(row.get("tags") or []),
            ",".join(row.get("ingredients") or []),
        ]
        steps = row.get("steps") or []
        if steps:
            parts.append(str(steps[0]))
        lines.append(f"{index}. " + _FIELD_SEP.join(parts))
    hint = payload.get("hint")
    if hint:
        lines.append("[提示] " + str(hint))
    return "\n".join(lines)


def _text_of(recipe: Recipe) -> str:
    return " ".join([recipe.name, *recipe.ingredients])


def _alias_hit(text: str, alias: str) -> bool:
    """`alias` 是否**作为食材**出现在文本里（排掉"鸡蛋""鱼香"这类假朋友）。

    中文没有词边界，只能靠上下文：找到每个出现位置，看一眼后面几个字
    是不是组成了另一个词。是就跳过，继续找下一次出现。
    """
    false_friends = ALIAS_FALSE_FRIENDS.get(alias, ())
    start = 0
    while True:
        index = text.find(alias, start)
        if index < 0:
            return False
        window = text[index:index + 3]
        if not any(window.startswith(word) for word in false_friends):
            return True
        start = index + 1


def _hits_ingredient(recipe: Recipe, words: Sequence[str]) -> bool:
    haystack = _text_of(recipe)
    return any(word and _alias_hit(haystack, word) for word in words)


def _hits_tag(recipe: Recipe, words: Sequence[str]) -> bool:
    return bool(set(recipe.tags) & set(words))


#: 忌口词 -> 它该顺带排掉的标签（第 76 步）。
#:
#: 为什么需要：`_text_of()` 只拼了**菜名 + 食材**，所以「不吃辣」原来是靠
#: 「食材里恰好写着辣椒/花椒」才生效的。实测 22 道带 `辣` 标签的菜里，
#: 「回锅肉」的食材一个辣字都没有 —— `exclude=["辣"]` 拦不住它，
#: 而界面上那个「一直记住 · 不吃辣」看起来是绝对的。
#:
#: 「微辣」也算辣：用户说不吃辣，不会因为标签写的是"微辣"就接受。
_TAG_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "辣": ("辣", "微辣"),
}


def _excluded_by(recipe: Recipe, terms: Sequence[str]) -> str:
    """这道菜是否踩到忌口；返回命中的那个词（没踩到返回空串）。

    文本（菜名 + 食材）和**标签**都要查：前者管"香菜/虾"这类具体东西，
    后者管"辣"这类只体现在标签上的属性。
    """
    haystack = _text_of(recipe)
    for term in terms:
        for word in expand_term(term):
            if word and word in haystack:
                return term
        # 标签也要看：只查食材会让「回锅肉」这种菜漏网
        for tag in _TAG_EXPANSIONS.get(term, (term,)):
            if tag in recipe.tags:
                return term
    return ""


class RecipeSearchTool(BaseTool):
    """在本地菜谱库里按"口味 + 主料 + 时长"查菜谱。"""

    name = "search_recipes"
    description = (
        "在本地菜谱库里查菜谱。**把用户的话拆成参数**：口味/菜系/场景放 tags，"
        "主料放 ingredients（如「鸡肉」→ ingredients=['鸡肉']），时长放 max_minutes，"
        "忌口放 exclude。用户问「有没有辣的鸡肉」要拆成 tags=['辣'] + ingredients=['鸡肉']，"
        "不要整句塞进 keyword。返回的菜都带 id，只能引用返回里的菜。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "菜名里的字面词（如「三杯鸡」「麻婆」）。不确定就别传，用其它参数",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "标签：口味（清淡/辣/微辣/酸甜/咸香）、"
                    "菜系（川菜/粤菜/湘菜…）、场景（快手/下饭/主食/汤）"
                ),
            },
            "ingredients": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "主料，例如「鸡肉」「牛肉」「鱼」「豆腐」「土豆」。"
                    "会按别名匹配（鸡肉 → 三黄鸡/鸡腿/鸡丁）"
                ),
            },
            "min_minutes": {
                "type": "integer",
                "description": "最短烹饪时间（分钟）。用户说「30 分钟以上」「有时间慢慢做」就传 30",
            },
            "max_minutes": {
                "type": "integer",
                "description": "最长烹饪时间（分钟）。用户说「20 分钟内」就传 20",
            },
            "search_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "**同义/场景扩展词**（第 181 步）：对方说的是场景或功效而不是菜名时用。"
                    "例如「下酒」→ [\"卤味\", \"凉菜\", \"酱牛肉\", \"花生米\"]；"
                    "「发汗」→ [\"酸辣\", \"胡辣汤\", \"麻辣\"]；"
                    "「牙口不好想吃软的」→ [\"清蒸\", \"羹\", \"豆腐\"]。"
                    "只在 keyword 一条都没命中时才会被用到（有字面命中时它不影响结果）"
                ),
            },
            "query_text": {
                "type": "string",
                "description": (
                    "**用户这一轮的原话，一字不改地抄进来**（第 193 步）。"
                    "只用于「库里没有字面对应」时的语义召回 —— 换句话说是兜底，"
                    "所以即使你已经把条件拆好放进 tags/ingredients，也要把原话带上"
                ),
            },
            "exclude": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要排除的食材（用户的忌口），例如「香菜」「海鲜」",
            },
            "exclude_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "要排除的**标签**：用户这一轮说的否定口味，例如「不要辣的」"
                    "「川菜里不辣的有吗」→ [\"辣\"]。"
                    "长期忌口不用传，服务端已经放进 exclude 了"
                ),
            },
        },
        "required": [],
    }
    #: 不进轮盘菜单：它是"模型自己决定什么时候用"的工具，不是手动入口。
    show_in_menu = False

    def __init__(
        self,
        storage: Storage | None = None,
        *,
        max_results: int = MAX_RESULTS,
        embedder: Embedder | None = None,
        judge: "Any | None" = None,
    ) -> None:
        self._storage = storage
        self._max_results = max(1, int(max_results))
        #: 向量召回的嵌入器（第 182 步）。None = 只走字面检索。
        self._embedder = embedder
        #: 语义裁决者（第 188 步）：`async (query, [菜名]) -> [候选编号]`。
        #: 没有它就不走向量路 —— 未经裁决的向量结果会破坏"零命中正确"。
        self._judge = judge
        #: 向量缓存：模型名 -> [(recipe_id, vector)]。库小，首次用到时整批读进内存。
        self._vectors: dict[str, list[tuple[int, list[float]]]] = {}
        #: 去中心化用的均值向量（按模型缓存）
        self._vector_means: dict[str, list[float]] = {}

    # ------------------------------------------------------------- 主逻辑

    async def run(
        self,
        keyword: str = "",
        tags: Sequence[str] | None = None,
        ingredients: Sequence[str] | None = None,
        min_minutes: int | None = None,
        max_minutes: int | None = None,
        exclude: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        search_terms: Sequence[str] | None = None,
        query_text: str = "",
        strict: bool = False,
    ) -> ToolResult:
        """查菜谱。

        `strict=True` 时**不放宽**用户明确给出的条件（勾选的标签/时间），
        查不到就如实返回 0 条。普通调用（模型自己推断的条件）仍走放宽链。
        """
        if self._storage is None:
            return ToolResult(
                ok=False,
                error="菜谱库还没准备好，暂时查不了菜谱",
                tool_name=self.name,
            )

        keyword = str(keyword or "").strip()
        # 诊断计时起点（第 196 步）：整条检索（字面 + 向量 + 裁决）花多久
        started = time.perf_counter()
        # 勾选的标签**按维度分组**：跨维度「与」、同维度「或」
        tag_groups = group_tags_by_dimension(tags or [])
        ingredient_words = [
            word
            for term in (ingredients or [])
            for word in expand_ingredient(term)
        ]
        exclude_list = [str(t).strip() for t in (exclude or []) if str(t).strip()]

        # 先取候选集：有关键词就用检索，否则全量扫（个人库几十到几千条）
        candidates = _scan_candidates(self._storage, keyword)

        # 第 181 步（评价14 阶段 0 量出来的缺口）：
        # 「下酒」「发汗」「牙口不好」这类**场景/功效**词，种子库里一次都没出现，
        # 字面检索必然 0 条。模型在拆参时顺手给出同义/场景扩展词，这里才用得上。
        # 只在**字面一条都没命中**时才启用，所以有命中时行为逐字节不变。
        expansion_used: list[str] = []
        #: 候选菜 id -> 命中了第几个扩展词（越小越优先）。重排要用它（第 185 步）。
        expansion_rank: dict[int, int] = {}
        scene_hit = ""
        if not candidates:
            wanted = [str(term).strip() for term in (search_terms or []) if str(term).strip()]
            if not wanted:
                # 第 184 步：模型没给扩展词时，用**服务端的场景词表**兜
                # （「下酒」→ 盐水鸭/夫妻肺片/白切鸡…）。零模型依赖、零延迟、可测试。
                scene_text = f"{keyword} {query_text}".strip()
                wanted = expand_scene_terms(scene_text)
                if wanted:
                    scene_hit = describe_scene(scene_text)
            if wanted:
                # 按**词的优先级**排序：越靠前的词越具体（词表里是"盐水鸭"在前、
                # "花生米"在后），它命中的菜就排在前面。只看"谁先被拼进来"会让
                # 泛词的菜抢到前排（实测：下酒的第一位变成螺蛳粉）。
                first_hit: dict[int, int] = {}
                by_id: dict[int, object] = {}
                expansion_rank: dict[int, int] = {}
                for index, term in enumerate(wanted):
                    hits = _scan_candidates(self._storage, term)
                    if hits:
                        expansion_used.append(term)
                    for recipe in hits:
                        by_id[recipe.id] = recipe
                        first_hit.setdefault(recipe.id, index)
                        expansion_rank.setdefault(recipe.id, index)
                candidates = [
                    by_id[recipe_id]
                    for recipe_id in sorted(first_hit, key=lambda rid: (first_hit[rid], rid))
                ]

        # 第 182 步（评价14 的向量召回）：字面候选之外，再并入"语义上接近"的菜。
        # 只有两个条件都满足才走这条路：
        #   1. 配好了嵌入器（有 GLM key 且开关打开）；
        #   2. 调用方给了 `query_text`（**用户原话**）—— 语义要能用上原话，
        #      而不是模型拆出来的那几个参数（评价14 v1 的缺口就在这里）。
        # 向量结果只**扩大候选池**，后面所有硬约束（忌口/否定口味/时间/标签）照旧生效。
        vector_note = ""
        vector_added = 0
        vector_picked: list[str] = []
        if (
            self._embedder is not None
            and self._judge is not None
            and str(query_text or "").strip()
        ):
            try:
                added = await self._vector_candidates(str(query_text).strip())
            except Exception as error:  # noqa: BLE001 —— 嵌入失败只降级，不影响字面
                logger.warning("向量召回失败，本次只走字面检索：%s", error)
                added = []
            if added:
                # 诊断用（第 196 步）：裁决选中的是谁 —— 不管最后有没有并进候选池都记下来
                vector_picked = [recipe.name for recipe in added]
                known = {recipe.id for recipe in candidates}
                extra = [recipe for recipe in added if recipe.id not in known]
                if extra:
                    vector_added = len(extra)
                    literal_rank = [recipe.id for recipe in candidates]
                    vector_rank = [recipe.id for recipe in added]
                    by_id = {recipe.id: recipe for recipe in candidates + extra}
                    fused = [
                        by_id[recipe_id]
                        for recipe_id in recipe_vectors.rrf_fuse(
                            [literal_rank, vector_rank]
                        )
                        if recipe_id in by_id
                    ]
                    candidates = fused
                    vector_note = f"语义召回补入 {len(extra)} 道"

        # 忌口是硬约束：先过滤，任何放宽都不会把它们放回来
        candidates = [
            recipe for recipe in candidates
            if not _excluded_by(recipe, exclude_list)
        ]

        # 本轮的否定口味（第 77 步）：「川菜里不辣的有吗」「想吃鸡但不要辣」。
        # 和忌口一样是硬约束、同样放在放宽之前 —— 用户说了不要辣，
        # 就不能因为"实在找不到"而给它端上一盘辣的。
        # 复用 _TAG_EXPANSIONS，所以排除「辣」也会排掉「微辣」。
        excluded_tags = [str(tag).strip() for tag in (exclude_tags or []) if str(tag).strip()]
        if excluded_tags:
            tag_words: list[str] = []
            for tag in excluded_tags:
                tag_words.extend(_TAG_EXPANSIONS.get(tag, (tag,)))
            candidates = [
                recipe for recipe in candidates
                if not any(word in recipe.tags for word in tag_words)
            ]

        picked, relaxed = self._apply_filters(
            candidates,
            expansion_rank=expansion_rank,
            tag_groups=tag_groups,
            ingredient_words=ingredient_words,
            min_minutes=min_minutes,
            max_minutes=max_minutes,
            tags=tags or [],
            ingredients=ingredients or [],
            strict=strict,
        )

        payload: dict[str, Any] = {
            "count": len(picked),
            "items": [_recipe_payload(recipe) for recipe in picked],
        }
        if vector_note:
            payload["vector_recall"] = vector_note
        if expansion_used:
            payload["expanded"] = expansion_used
            # 和"放宽"一样如实说明：这不是字面命中，是换了个说法找到的
            source = f"场景词表「{scene_hit}」" if scene_hit else "扩展词"
            relaxed = list(relaxed or []) + [
                f"按{source}扩展检索：{'、'.join(expansion_used)}"
            ]
        if relaxed:
            payload["relaxed"] = relaxed
            payload["hint"] = (
                "库里的菜不能同时满足全部条件，所以**放宽了**："
                + "、".join(relaxed)
                + "。请如实告诉对方放宽了什么，不要假装完全符合。"
            )
        elif not picked and strict:
            payload["hint"] = (
                "**严格按对方勾选的条件**没找到菜，没有放宽。"
                "请如实说没有，并指出放宽哪一项（口味/菜系/时间）可能找得到；"
                "不要编造，也不要把别的菜说成符合条件。"
            )
        elif not picked:
            payload["hint"] = (
                "库里没有符合条件的菜。请如实说没有，不要编造；"
                "可以建议对方换个主料或放宽口味/时间。"
            )

        logger.info(
            "菜谱检索 keyword=%r tags=%r ingredients=%r min=%r max=%r exclude=%r 扩展=%r → %s 道%s",
            keyword, list(tags or []), list(ingredients or []),
            min_minutes, max_minutes,
            exclude_list, expansion_used, len(picked),
            f"（放宽：{relaxed}）" if relaxed else "",
        )
        # 第 196 步（评价15 T2-8）：一行机器可读的诊断，专门为了"从真实日志里抽 holdout"。
        # 为什么非要结构化：真实查询是不可再生的原材料，而上面那行人话日志少两样最关键的东西
        # ——「这次到底走没走向量轨」「裁决选了谁」。抽的时候还得反推，反推就会错。
        # 这里只记**事实**（触发了什么、选了什么、花了多久），不记用户的原话之外任何隐私字段；
        # 原话本来就已经在 query_text 里了，抽 holdout 正需要它。
        logger.info(
            "检索诊断 %s",
            json.dumps(
                {
                    "tool": self.name,
                    "keyword": keyword,
                    "query_text": str(query_text or ""),
                    "count": len(picked),
                    "names": [recipe.name for recipe in picked],
                    "vector": bool(vector_note),
                    "vector_added": vector_added,
                    "judge_picked": list(vector_picked),
                    "expanded": list(expansion_used),
                    "relaxed": list(relaxed or []),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                },
                ensure_ascii=False,
            ),
        )
        return ToolResult(
            ok=True,
            content=_model_content(payload),
            # 卡片要从 id 渲染（`api/recipe_routes.py::_mentioned_ids`），
            # 所以结构化结果原样放进 data，不再给模型塞一遍 JSON（第 211 步）
            data=payload,
            tool_name=self.name,
            arguments={
                "keyword": keyword,
                "tags": list(tags or []),
                "ingredients": list(ingredients or []),
                "min_minutes": min_minutes,
                "max_minutes": max_minutes,
                "exclude": exclude_list,
            },
        )

    # ------------------------------------------------------------- 向量召回

    async def _vector_candidates(self, query_text: str) -> list:
        """用 `query_text` 的向量从库里捞一批"语义接近"的菜（按相似度降序）。

        向量在内存里，库小（几十到几千条），纯 Python 余弦就够。
        换嵌入模型后旧向量自动不参与（`model` 列做隔离）。
        """
        if self._storage is None or self._embedder is None:
            return []

        model = getattr(self._embedder, "model", "unknown")
        vectors = self._vectors.get(model)
        if vectors is None:
            vectors = recipe_vectors.load_vectors(self._storage, model)
            self._vectors[model] = vectors
        if not vectors:
            return []

        embedded = await self._embedder.embed([query_text])
        if not embedded:
            return []

        # 去中心化（第 188 步）：句向量各向异性，不减均值时相似度全挤在窄区间
        dish_vectors, query_vector = recipe_vectors.center_vectors(
            [vector for _recipe_id, vector in vectors], embedded[0]
        )
        centered = [
            (recipe_id, vector)
            for (recipe_id, _original), vector in zip(vectors, dish_vectors)
        ]
        ids = recipe_vectors.search_vectors(
            query_vector, centered, limit=VECTOR_LIMIT, min_score=VECTOR_MIN_SCORE
        )
        if not ids:
            return []

        by_id = {recipe.id: recipe for recipe in list_recipes(self._storage, limit=1000)}
        recalled = [by_id[recipe_id] for recipe_id in ids if recipe_id in by_id]
        if not recalled:
            return []

        # 裁决：向量只负责"召回"，要不要由模型说（它可以直接说"都不合适"）。
        # 没有裁决者就不走向量路 —— 否则"库里没有"会被向量硬凑出结果。
        if self._judge is None:
            return []

        # 第 265 步**试过并放弃**：把候选写成「菜名（标签｜主要食材｜用时）」。
        # 想法是"只给菜名，它没法判断口味淡/二十分钟内"，但 v4 实测反而更差：
        # 只给菜名 9/15、带元数据 8/15（零命中都是 13/15）。差的那一条是
        # 「胃不舒服想喝热汤」—— 带上"猪骨/熬煮"这类标签后它反而判为不合胃。
        # 所以保持只给菜名：更省 token，也更少"多余信息带偏模型"。
        names = [recipe.name for recipe in recalled]
        try:
            picks = await self._judge(query_text, names)
        except Exception as error:  # noqa: BLE001 —— 裁决失败按"没有"处理
            logger.warning("语义裁决异常，本次不用向量结果：%s", error)
            return []

        chosen = [
            recalled[int(index) - 1]
            for index in picks
            if isinstance(index, int) and 1 <= int(index) <= len(recalled)
        ]
        logger.info("语义召回：%s → %s", query_text[:24], [r.name for r in chosen])
        return chosen

    # ------------------------------------------------------------- 内部

    def _apply_filters(
        self,
        candidates: list[Recipe],
        *,
        tag_groups: dict[str, list[str]],
        ingredient_words: Sequence[str],
        min_minutes: int | None,
        max_minutes: int | None,
        tags: Sequence[str],
        ingredients: Sequence[str],
        strict: bool = False,
        expansion_rank: dict[int, int] | None = None,
    ) -> tuple[list[Recipe], list[str]]:
        """按条件筛＋排序；筛空时**按维度逐一放宽**并记录放宽了什么。

        顺序刻意是 口味 → 便捷 → 品类 → 做法 → 功效 → 偏好 → 菜系 → 时间 → 主料：

        - **口味最先让**："想吃辣的"经常只是随口一句，换成不辣的也能接受；
        - **菜系很晚才让**："就想吃川菜"是很强的意图；
        - **主料最后才让**：那通常才是用户真正的诉求（"我想吃鱼"）。

        第 52 步之前的实现是**一次性把所有标签都丢掉**，于是用户勾了
        「微辣 + 粤菜 + 下饭」问「鱼」时，粤菜也被丢了，跑出湖北的蒸鱼。

        `strict=True` 时**完全不放宽**——用户明确勾过的条件不能偷偷丢掉，
        查不到就返回空，让模型如实说"没有"（第 55 步 P0-2）。
        """
        active = {name: list(values) for name, values in tag_groups.items()}
        relaxed: list[str] = []

        def pick() -> list[Recipe]:
            return [
                recipe for recipe in candidates
                if self._matches(recipe, active, ingredient_words, min_minutes, max_minutes)
            ]

        chosen = pick()
        if chosen:
            return self._rank(chosen, active, ingredient_words, expansion_rank), []

        # 严格模式：不放宽。用户勾过的条件就是硬约束，宁可说"没有"。
        if strict:
            return [], []

        # 1) 逐维度放宽
        for dimension in RELAX_ORDER:
            if dimension not in active:
                continue
            dropped = active.pop(dimension)
            relaxed.append(f"{dimension}（{'/'.join(dropped)}）")
            chosen = pick()
            if chosen:
                return self._rank(chosen, active, ingredient_words, expansion_rank), relaxed

        # 2) 放宽时间
        if min_minutes or max_minutes:
            relaxed.append(self._time_label(min_minutes, max_minutes))
            min_minutes = max_minutes = None
            chosen = pick()
            if chosen:
                return self._rank(chosen, active, ingredient_words, expansion_rank), relaxed

        # 3) 最后才动主料
        if ingredient_words:
            relaxed.append(f"主料（{'/'.join(ingredients)}）")
            ingredient_words = []
            chosen = pick()
            if chosen:
                return self._rank(chosen, active, ingredient_words, expansion_rank), relaxed

        return [], []

    @staticmethod
    def _time_label(min_minutes: int | None, max_minutes: int | None) -> str:
        if min_minutes and max_minutes:
            return f"时间（{min_minutes}~{max_minutes} 分钟）"
        if min_minutes:
            return f"时间（{min_minutes} 分钟以上）"
        return f"时间（{max_minutes} 分钟内）"

    def _matches(
        self,
        recipe: Recipe,
        active: dict[str, list[str]],
        ingredient_words: Sequence[str],
        min_minutes: int | None,
        max_minutes: int | None,
    ) -> bool:
        """**跨维度「与」、同维度「或」**。

        勾了「微辣 + 粤菜」就要求两个维度都满足：
        只命中微辣的湘菜不算，只命中粤菜的白切鸡也不算。
        """
        for dimension, tags in active.items():
            words = expand_tags(tags)
            if not _hits_tag(recipe, words):
                return False
        if ingredient_words and not _hits_ingredient(recipe, ingredient_words):
            return False
        if min_minutes and not self._at_least(recipe, min_minutes):
            return False
        if max_minutes and not self._within(recipe, max_minutes):
            return False
        return True

    def _rank(
        self,
        recipes: list[Recipe],
        active: dict[str, list[str]],
        ingredient_words: Sequence[str],
        expansion_rank: dict[int, int] | None = None,
    ) -> list[Recipe]:
        """按相关度排序：主料 > 命中的维度数；同分用**按查询固定的随机**打散。

        为什么不用 `ORDER BY updated_at`：那个顺序取决于"你什么时候导入的"，
        和用户问的东西没关系（第 52 步之前就是这么排的）。

        为什么随机要"固定种子"：用户希望同样的问题别每次都给同一道菜，
        但测试和排查需要可复现。以查询词做种子，两头都满足。
        """
        seed = "|".join(f"{name}:{','.join(values)}" for name, values in sorted(active.items()))
        rng = random.Random(seed)

        # 第 185 步：扩展召回的顺序**必须带进重排**。
        # 以前扩召回按"命中了第几个扩展词"排好序，但这里又按"和查询词的相关度"重排，
        # 而「下酒」这种词在菜名里根本不存在 —— 于是正确命中的白切鸡被排到第 5。
        # 现在：先看扩展词优先级（越小越优先），再看原有的主料/标签分。
        ranked_ids = expansion_rank or {}

        scored: list[tuple[int, int, int, float, Recipe]] = []
        for recipe in recipes:
            score = 0
            if ingredient_words and _hits_ingredient(recipe, ingredient_words):
                score += 4
            score += 2 * sum(
                1 for tags in active.values() if _hits_tag(recipe, expand_tags(tags))
            )
            # 没被扩展命中的排在扩展命中之后（大数）
            expansion = ranked_ids.get(recipe.id, 10 ** 6)
            scored.append((expansion, -score, 0, rng.random(), recipe))

        scored.sort(key=lambda row: (row[0], row[1], row[3]))
        return [row[4] for row in scored[: self._max_results]]

    @staticmethod
    def _within(recipe: Recipe, max_minutes: int) -> bool:
        try:
            limit = int(max_minutes)
        except (TypeError, ValueError):
            # 第 263 步：以前这里直接 `return True`（**静默**把不合法的时间上限当成
            # "没有这个条件"）—— 用户勾了"半小时内"，端上来一道 90 分钟的菜，界面上
            # 看不出任何异常。现在参数在 `BaseTool.check_arguments` 就被拦掉了，
            # 走到这里说明是内部调用方传错，所以要留一条日志，别再无声无息。
            logger.warning(
                "时间上限参数不合法（%r），本次不按时间过滤 —— 调用方应先在参数校验里拦住",
                max_minutes,
            )
            return True
        if limit <= 0:
            return True
        return recipe.time_minutes is None or recipe.time_minutes <= limit

    @staticmethod
    def _at_least(recipe: Recipe, min_minutes: int) -> bool:
        """「30 分钟以上」：慢菜才要。时间未知的菜不排除（不赌）。"""
        try:
            floor = int(min_minutes)
        except (TypeError, ValueError):
            return True
        if floor <= 0:
            return True
        return recipe.time_minutes is None or recipe.time_minutes >= floor


class ConstrainedSearchTool(BaseTool):
    """把右栏勾选**强制**注入检索参数的代理（第 55 步 P0-2）。

    ## 为什么需要它

    勾选是**已知状态**：用户点了"微辣 + 粤菜"，后端明明知道。
    第 50e 那版的做法是把这些写进提示词，让模型"记得"转成工具参数——
    问题是模型忘了传（或传一半）就**静默失效**，而且测试只能断言
    "提示词里有这句话"，没法断言行为。

    这个代理把约束下沉到工具边界：

        Agent → registry.execute("search_recipes", 模型给的参数)
                → 本代理合并 wanted 后再交给真正的工具

    模型仍然可以**追加**条件（听出"想吃鱼"就传 ingredients），
    但不能**丢掉**用户点明的条件。时间上下限直接覆盖，不给模型改的余地。

    ## 为什么是"合并"而不是"替换"

    替换会让模型无法追加（用户说"辣的鸡肉"时，模型必须能补 ingredients）；
    合并只保证用户的勾选一定在里面。
    """

    def __init__(self, inner: BaseTool, constraints: dict[str, Any]) -> None:
        self._inner = inner
        self._tags: list[str] = list(constraints.get("tags") or [])
        self._min = constraints.get("min_minutes")
        self._max = constraints.get("max_minutes")
        #: 长期记住的忌口词条（存库）。**只能并集、不能丢**——
        #: 这是安全约束，放宽链与模型都不许把它去掉。
        self._exclude: list[str] = list(constraints.get("exclude") or [])
        #: 上一轮用过、这轮既没勾也没提的标签（防止模型拿历史条件来补）
        self._forbid_tags: set[str] = set(constraints.get("forbid_tags") or ())
        #: 这一轮用户明说的否定口味（第 77 步）。和忌口一样是硬约束、只并集。
        self._exclude_tags: list[str] = list(constraints.get("exclude_tags") or ())
        #: 这一轮说的「别放葱」这类（第 90 步 / A2）。**不落库**，
        #: 但和长期忌口一样并进 exclude —— 对这次请求而言两者都是硬约束。
        self._turn_exclude: list[str] = list(constraints.get("turn_exclude") or ())
        # 对模型暴露的 schema 与真正的工具完全一致
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.show_in_menu = inner.show_in_menu
        self.label = inner.label
        self.intent_template = inner.intent_template

    @property
    def applied(self) -> dict[str, Any]:
        """本代理强制注入了什么（日志与测试用）。"""
        return {
            "tags": list(self._tags),
            "min_minutes": self._min,
            "max_minutes": self._max,
            "exclude": list(self._exclude),
            "forbid_tags": sorted(self._forbid_tags),
            "exclude_tags": list(self._exclude_tags),
            "turn_exclude": list(self._turn_exclude),
        }

    def merge(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """把约束合并进模型给的参数。"""
        merged = dict(arguments or {})
        if self._tags:
            from core.recipes import tag_dimension

            # **同一维度上以用户勾的为准**：模型说"清淡"、用户勾了"辣"，
            # 不能并集（那会同时返回清淡和辣的菜，卡片自相矛盾——实测出现过）。
            # 跨维度仍然合并：用户勾了辣，模型可以再加"鱼"这类主料/菜系条件。
            enforced_dimensions = {tag_dimension(tag) for tag in self._tags}
            kept = [
                str(tag) for tag in (merged.get("tags") or [])
                if tag_dimension(str(tag)) not in enforced_dimensions
            ]
            for tag in self._tags:
                if tag not in kept:
                    kept.append(tag)
            merged["tags"] = kept

        # 历史泄漏：模型可能把**上一轮**的条件再补进来（用户没勾、这句也没提）。
        # 例如第二轮勾了川菜、第三轮取消并只问"鱼"，模型仍记得川菜。
        if self._forbid_tags:
            merged["tags"] = [
                tag for tag in (merged.get("tags") or [])
                if str(tag) not in self._forbid_tags
            ]

        # 本轮的否定口味：**并集**，永不覆盖、永不丢弃
        if self._exclude_tags:
            existing_tags = [str(tag) for tag in (merged.get("exclude_tags") or [])]
            for tag in self._exclude_tags:
                if tag not in existing_tags:
                    existing_tags.append(tag)
            merged["exclude_tags"] = existing_tags

        # 忌口：**并集**，永不覆盖、永不丢弃
        if self._exclude or self._turn_exclude:
            existing = [str(term) for term in (merged.get("exclude") or [])]
            for term in [*self._exclude, *self._turn_exclude]:
                if term not in existing:
                    existing.append(term)
            merged["exclude"] = existing

        if self._min is not None:
            merged["min_minutes"] = self._min
        if self._max is not None:
            merged["max_minutes"] = self._max
        return merged

    async def run(self, **kwargs: Any) -> ToolResult:
        # strict=True：勾选是用户点明的条件，不允许放宽链把它们丢掉。
        # 查不到就返回 0 条，让模型如实说"没有"，而不是偷偷换一道不辣的菜。
        merged = self.merge({**kwargs, "strict": True})
        result = await self._inner.run(**merged)
        # 记录**实际生效**的参数，而不是模型原本说的那套（第 77 步）：
        # 否则归因时看不见服务端注入的忌口/否定口味，下游按
        # `arguments` 记账的地方（比如"这轮用过哪些标签"）也会漏。
        result.arguments = merged
        return result


def with_enforced_constraints(
    tool: BaseTool,
    wanted: list[str],
    *,
    aversions: list[str] | None = None,
    forbid_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    turn_exclude: list[str] | None = None,
) -> BaseTool:
    """给检索工具套上"本次请求"的强约束；没有任何约束时原样返回。

    - `wanted`：右栏这次勾的标签/时间（用户点明的）
    - `aversions`：**长期记住的忌口**（存库）。这是安全约束，每轮都要注入，
      不能指望模型记得；而且放宽链不许把它去掉。
    - `forbid_tags`：上一轮用过、这轮既没勾也没提的标签。防止模型拿历史条件来补。
    - `exclude_tags`：**这一轮**用户明说的否定口味（第 77 步）。由服务端从这句话里抽，
      不依赖模型传参——实测模型不传、还照样说"已避开"。
    """
    from core.recipes import wanted_constraints

    constraints = wanted_constraints(wanted)
    constraints["exclude"] = [str(term) for term in (aversions or []) if str(term).strip()]
    constraints["forbid_tags"] = list(forbid_tags or [])
    constraints["exclude_tags"] = [str(tag) for tag in (exclude_tags or []) if str(tag).strip()]
    # 第 90 步：这一轮说的「别放葱」不落库，但本次请求照样要生效
    constraints["turn_exclude"] = [
        str(term) for term in (turn_exclude or []) if str(term).strip()
    ]
    if not (
        constraints.get("tags")
        or constraints.get("min_minutes")
        or constraints.get("max_minutes")
        or constraints.get("exclude")
        or constraints.get("forbid_tags")
        or constraints.get("exclude_tags")
        or constraints.get("turn_exclude")
    ):
        return tool
    return ConstrainedSearchTool(tool, constraints)
