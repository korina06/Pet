"""本地菜谱库的接口层：参数校验与响应组装，增删改查与检索都在 `core/recipe_library.py`。

撞名返回 409 并带上已有记录的 id；数组字段由 pydantic 的 `list[str]` 在入口挡住类型错。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from core.recipe_library import (
    AVOID_OPTIONS,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SOURCES,
    WANT_GROUPS,
    DuplicateRecipe,
    Recipe,
    count_recipes,
    count_search,
    create_recipe,
    delete_recipe,
    get_recipe,
    list_aversions,
    list_recipes,
    search_recipes,
    set_aversions,
    update_recipe,
)
from core.recipe_seed import SeedError, import_seed
from core.storage import Storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recipe/library", tags=["recipe-library"])


# --------------------------------------------------------------- 请求/响应


class RecipeOut(BaseModel):
    id: int
    name: str
    ingredients: list[str] = []
    steps: list[str] = []
    tags: list[str] = []
    time_minutes: int | None = None
    source: str = "user"
    origin_url: str = ""
    note: str = ""

    @classmethod
    def of(cls, recipe: Recipe) -> RecipeOut:
        return cls(**recipe.to_dict())


class RecipeListResponse(BaseModel):
    recipes: list[RecipeOut]
    total: int
    limit: int
    offset: int
    #: 按来源的计数，给界面顶部那行"共 N 道（其中 M 道是示例）"用。
    #:
    #: 刻意**不叫 stats**：`GET /stats` 返回的是 `{total, by_source, by_tag}`，
    #: 两个接口里同名的字段形状却不同，是留给前端的坑（第 48 步自查时发现）。
    #: 这里只回"按来源"，总数上方已有 `total`，不重复给。
    source_counts: dict[str, int] = {}


class RecipeCreateRequest(BaseModel):
    #: 菜名必填；长度上限照 FIELD 的口径给足，清洗在 core 里做
    name: str = Field(min_length=1, max_length=200)
    #: **列表**而不是字符串：见模块 docstring 第 2 条
    ingredients: list[str] = []
    steps: list[str] = []
    tags: list[str] = []
    time_minutes: int | None = Field(default=None, ge=1, le=600)
    note: str = ""
    #: 手动录入固定 user；粘贴解析（第 49 步）会传 parsed
    source: str = "user"
    origin_url: str = ""


class RecipeUpdateRequest(BaseModel):
    """局部更新：**没传的字段不改**。

    与 core 的 `_UNSET` 哨兵对齐靠 `model_fields_set`——
    `time_minutes=None` 是"清掉时间"，不是"没传"。
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    ingredients: list[str] | None = None
    steps: list[str] | None = None
    tags: list[str] | None = None
    time_minutes: int | None = Field(default=None, ge=1, le=600)
    note: str | None = None
    source: str | None = None
    origin_url: str | None = None


class SeedResponse(BaseModel):
    added: int
    skipped: int
    total: int
    #: 导入后库里的总数（前端一次刷新就能显示"现在有 N 道"）
    library_total: int


class StatsResponse(BaseModel):
    total: int
    by_source: dict[str, int]
    #: 标签分布（按出现次数降序），给第 50 步的标签面板打底
    by_tag: dict[str, int]


class PreferencesResponse(BaseModel):
    """右栏那个"装标签的框"需要的全部数据，一次取回。"""

    #: 已经记住的忌口词条（存库，重开窗口还在）
    aversions: list[str] = []
    #: 忌口的大众选项（后端定，前端不硬编码中文）
    avoid_options: list[str] = []
    #: 「这次想要」的分组标签（这些 tag 在库里真实存在，点了能筛出结果）
    want_groups: list[dict[str, Any]] = []


class AversionsRequest(BaseModel):
    #: **整份替换**：前端提交"当前亮着的那些"
    aversions: list[str] = Field(default_factory=list, max_length=64)


# --------------------------------------------------------------- 依赖


def _storage(request: Request) -> Storage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        # 正常情况下 main.py 的 lifespan 一定建好了；这里防的是"直接构造 app"的测试
        raise HTTPException(status_code=503, detail="存储未初始化")
    return storage


def _recipe_or_404(storage: Storage, recipe_id: int) -> Recipe:
    recipe = get_recipe(storage, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"菜谱不存在（id={recipe_id}）")
    return recipe


def _check_source(source: str) -> str:
    if source not in SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"来源只能是 {'、'.join(SOURCES)} 之一，收到：{source}",
        )
    return source


def _source_counts(storage: Storage) -> dict[str, int]:
    """按来源的计数，形状是平铺的 ``{"seed": 30, "user": 2}``。

    只列**非零**的来源：库里没有 parsed/generated 时不出现这些键，
    前端一行就能渲染，不用先过滤零值。
    """
    counts: dict[str, int] = {}
    for source in SOURCES:
        count = count_recipes(storage, source=source)
        if count:
            counts[source] = count
    return counts


# --------------------------------------------------------------- 读写


@router.get("", response_model=RecipeListResponse)
async def list_library(
    request: Request,
    source: str | None = Query(default=None, max_length=32),
    tag: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=200, description="关键词检索；给了就走 search"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    #: 首页「换一批」用：随机顺序取前 N 条（与 q 同时给时以检索为准，忽略它）
    shuffle: bool = Query(default=False),
) -> RecipeListResponse:
    """列表 / 检索二合一。

    合成一个接口是有意的：界面就是"一个搜索框 + 一个列表"，
    分成两个接口会让前端在"清空搜索框"时还要判断该回退到哪个接口。
    给了 `q` 走检索（FTS 优先、别名展开），否则走列表（按最近更新倒序）。
    """
    storage = _storage(request)
    if source is not None:
        _check_source(source)

    keyword = (q or "").strip()
    if keyword:
        recipes = search_recipes(storage, keyword, limit=limit, offset=offset)
        # total 必须反映**检索**命中数：否则界面上会出现
        # "共 30 道"却只列出 3 条的怪象（用户会以为分页坏了）
        total = count_search(storage, keyword)
    else:
        recipes = list_recipes(
            storage, source=source, tag=tag, limit=limit, offset=offset, shuffle=shuffle
        )
        total = count_recipes(storage, source=source, tag=tag)

    return RecipeListResponse(
        recipes=[RecipeOut.of(recipe) for recipe in recipes],
        total=total,
        limit=limit,
        offset=offset,
        source_counts=_source_counts(storage),
    )


@router.get("/stats", response_model=StatsResponse)
async def library_stats(request: Request) -> StatsResponse:
    """总数 / 按来源 / 按标签——给界面顶部那行汇总与第 50 步的标签面板用。"""
    storage = _storage(request)
    by_tag: dict[str, int] = {}
    for row in storage.query("SELECT tags FROM recipes WHERE tags <> '[]'"):
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):      # 脏数据不该让统计接口 500
            continue
        for tag in tags if isinstance(tags, list) else []:
            by_tag[str(tag)] = by_tag.get(str(tag), 0) + 1

    by_source = _source_counts(storage)
    return StatsResponse(
        total=count_recipes(storage),
        by_source=by_source,
        # 次数降序，同次数按名字稳定排序（前端渲染才不会每次换位置）
        by_tag=dict(sorted(by_tag.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


@router.get("/preferences")
async def read_preferences(request: Request) -> PreferencesResponse:
    """右栏那个"装标签的框"需要的全部数据（一次取回，省几次往返）。

    `want_groups` 里的标签是**后端定的分组 + 库里真实存在的 tag**：
    这样"点了没菜"的情况不会出现（除非库被删空）。
    """
    storage = _storage(request)
    return PreferencesResponse(
        aversions=list_aversions(storage),
        avoid_options=list(AVOID_OPTIONS),
        want_groups=[
            {"label": label, "tags": list(tags), "exclusive": exclusive}
            for label, tags, exclusive in WANT_GROUPS
        ],
    )


@router.put("/preferences")
async def write_preferences(
    payload: AversionsRequest, request: Request
) -> PreferencesResponse:
    """整份替换忌口词条（点亮的那些）。其余选项原样回，前端一次重画整个框。"""
    storage = _storage(request)
    saved = set_aversions(storage, payload.aversions)
    logger.info("更新忌口词条：%s", saved)
    return PreferencesResponse(
        aversions=saved,
        avoid_options=list(AVOID_OPTIONS),
        want_groups=[
            {"label": label, "tags": list(tags), "exclusive": exclusive}
            for label, tags, exclusive in WANT_GROUPS
        ],
    )


@router.get("/by-ids", response_model=list[RecipeOut])
async def read_recipes_by_ids(
    request: Request, ids: str = Query(default="")
) -> list[RecipeOut]:
    """按 id 批量取菜（第 137 步）：前端恢复会话时用它把卡片画回来。

    `ids` 是逗号分隔的 id（`?ids=12,34`）。**按传入顺序返回**，缺的跳过——
    菜谱被删掉时不该让整段历史恢复失败。
    """
    storage = _storage(request)
    wanted: list[int] = []
    for chunk in str(ids or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            wanted.append(int(chunk))
        except ValueError:
            continue

    out: list[RecipeOut] = []
    for recipe_id in wanted:
        recipe = get_recipe(storage, recipe_id)
        if recipe is not None:
            out.append(RecipeOut.of(recipe))
    return out


@router.get("/{recipe_id}", response_model=RecipeOut)
async def read_recipe(recipe_id: int, request: Request) -> RecipeOut:
    return RecipeOut.of(_recipe_or_404(_storage(request), recipe_id))


@router.get("/{recipe_id}/image")
async def read_recipe_image(recipe_id: int, request: Request) -> Response:
    """这道菜的实拍图（来自独立图片库，按**菜名**关联，第 58 步）。

    三个刻意的决定：

    - **图片不在菜谱库里**：它是只读资产（4.4 MB），菜谱库是可变的；
      按 `name_key` 关联而不是 id——id 重导入一次就全变（实测 3 → 99）。
    - **404 而不是 500**：没有图是正常状态（用户自己录的菜就没图），
      前端据此静默隐藏 `<img>`。
    - **ETag + Cache-Control**：图片内容不变，刷新详情页走 304 不重复下载。
    """
    recipe = _recipe_or_404(_storage(request), recipe_id)
    store = getattr(request.app.state, "image_store", None)
    if store is None:
        raise HTTPException(status_code=404, detail="这道菜还没有图片")

    hit = store.get(recipe.name)
    if hit is None:
        raise HTTPException(status_code=404, detail="这道菜还没有图片")

    etag = f'"{hit["etag"]}"'
    cache = "public, max-age=604800"      # 一周：图片是静态资产
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})
    return Response(
        content=hit["data"],
        media_type=hit["mime"],
        headers={"ETag": etag, "Cache-Control": cache},
    )


@router.post("", response_model=RecipeOut, status_code=201)
async def add_recipe(payload: RecipeCreateRequest, request: Request) -> RecipeOut:
    """手动录入一道菜。

    撞名（归一化后同名）返回 **409** 且带上已存在那条的 id，
    前端据此提示"库里已经有这道菜了"。
    """
    storage = _storage(request)
    try:
        recipe = create_recipe(
            storage,
            name=payload.name,
            ingredients=payload.ingredients,
            steps=payload.steps,
            tags=payload.tags,
            time_minutes=payload.time_minutes,
            source=_check_source(payload.source),
            origin_url=payload.origin_url,
            note=payload.note,
        )
    except DuplicateRecipe as exc:
        raise HTTPException(
            status_code=409,
            detail=f"库里已经有「{exc.name}」了",
            headers={"X-Existing-Recipe-Id": str(exc.existing_id or "")},
        ) from exc
    except ValueError as exc:                # 字段清洗失败（如空菜名、时间非法）
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("菜谱入库：%s（id=%s，来源=%s）", recipe.name, recipe.id, recipe.source)
    return RecipeOut.of(recipe)


@router.patch("/{recipe_id}", response_model=RecipeOut)
async def edit_recipe(
    recipe_id: int, payload: RecipeUpdateRequest, request: Request
) -> RecipeOut:
    """局部更新：只有请求体里**出现过**的字段才改。

    这里有一处**必须小心**：`{"name": null}` 这种输入。
    core 的清洗是 `str(value).strip()`，直接放行会把菜名写成字符串 `"None"`
    ——一个安静又荒唐的坏数据。所以除了 `time_minutes`（它的 None 有"清掉时间"的
    明确含义），其余字段一律**拒绝显式 null**。
    """
    storage = _storage(request)
    nullable = {"time_minutes"}
    changes: dict[str, Any] = {}
    for name in payload.model_fields_set:
        value = getattr(payload, name)
        if value is None and name not in nullable:
            raise HTTPException(
                status_code=400,
                detail=f"字段 {name} 不能是 null（想清空请传空字符串或空数组）",
            )
        changes[name] = value

    if "source" in changes and changes["source"] is not None:
        _check_source(changes["source"])

    try:
        recipe = update_recipe(storage, recipe_id, **changes)
    except DuplicateRecipe as exc:
        raise HTTPException(
            status_code=409,
            detail=f"改名失败：库里已经有「{exc.name}」了",
            headers={"X-Existing-Recipe-Id": str(exc.existing_id or "")},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if recipe is None:
        raise HTTPException(status_code=404, detail=f"菜谱不存在（id={recipe_id}）")
    return RecipeOut.of(recipe)


@router.delete("/{recipe_id}")
async def remove_recipe(recipe_id: int, request: Request) -> dict[str, Any]:
    removed = delete_recipe(_storage(request), recipe_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"菜谱不存在（id={recipe_id}）")
    return {"status": "deleted", "id": recipe_id}


# --------------------------------------------------------------- 种子


@router.post("/seed", response_model=SeedResponse)
async def seed_library(request: Request) -> SeedResponse:
    """导入内置示例菜谱（**幂等**，已存在的跳过、不覆盖）。

    由用户点按钮触发，不在启动时自动灌：否则用户删掉的示例菜
    会在下次开机又长回来。
    """
    storage = _storage(request)
    try:
        result = import_seed(storage)
    except SeedError as exc:
        # 种子是我们自己发布的数据，坏了要明说，别让用户以为是自己操作错
        logger.warning("种子导入被拒绝：%s", exc)
        raise HTTPException(status_code=500, detail=f"内置示例菜谱有问题，已拒绝导入。{exc}") from exc

    return SeedResponse(
        added=result["added"],
        skipped=result["skipped"],
        total=result["total"],
        library_total=count_recipes(storage),
    )


# --------------------------------------------------------------- 买菜清单


class ShoppingListRequest(BaseModel):
    """挑中的几道菜（前端勾选后传 id）。"""

    recipe_ids: list[int] = Field(default_factory=list)


class ShoppingItemOut(BaseModel):
    name: str
    amounts: list[str] = []
    recipes: list[str] = []


class ShoppingListResponse(BaseModel):
    items: list[ShoppingItemOut] = []
    #: 复制到剪贴板用的纯文本（服务端生成，前后端口径一致）
    text: str = ""
    #: 找不到的 id（用户删了菜 / 传错 id）
    missing: list[int] = []


@router.post("/shopping-list", response_model=ShoppingListResponse)
async def shopping_list(
    payload: ShoppingListRequest, request: Request
) -> ShoppingListResponse:
    """把挑中的几道菜的食材合并成买菜清单（第 126 步）。

    **不做数量加法**：真实食材是"姜 1 大块 / 葱 2 根 / 适量"这种混在一起的文本，
    加起来只会得到错数字。这里只按名字归组、数量原样保留。

    找不到的 id 会照实返回，不静默忽略 —— 用户会以为自己选上了。
    """
    from core.recipe_library import get_recipe
    from core.shopping_list import build_shopping_list, render_text

    storage = _storage(request)
    picked: list[tuple[str, tuple[str, ...]]] = []
    missing: list[int] = []
    for recipe_id in dict.fromkeys(int(value) for value in payload.recipe_ids):
        recipe = get_recipe(storage, recipe_id)
        if recipe is None:
            missing.append(recipe_id)
            continue
        picked.append((recipe.name, recipe.ingredients))

    items = build_shopping_list(picked)
    return ShoppingListResponse(
        items=[ShoppingItemOut(**item.to_dict()) for item in items],
        text=render_text(items) if items else "",
        missing=missing,
    )
