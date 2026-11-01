"""菜谱库的数据层：建表与迁移、增删改查、按条件检索、标签与忌口信息。
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.storage import Storage, register_migration

#: 来源取值：用户手输 / 粘贴解析 / 模型发挥 / 网络卡 / 内置种子
#: `seed` 是第 48 步加的：项目自带的一批示例菜谱（AI 按常识整理、未逐条核对），
#: 与"用户自己录的（user）"必须区分开——界面上要能一眼看出哪条是示例数据，
#: 将来清理/替换种子也不该误伤用户自己录的。
SOURCES = ("user", "parsed", "generated", "web", "seed")

#: 数组字段的清洗口径：单项最长 300 字、最多 50 项
MAX_ITEM_LENGTH = 300
MAX_ITEMS = 50

#: 列表/检索的分页默认值与上限
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 检索会去匹配的字段（LIKE 路径用；FTS 侧对应影子表的四列）
SEARCH_COLUMNS = ("name", "ingredients", "tags", "note")

_UNSET = object()


class DuplicateRecipe(Exception):
    """撞名（name_key 已存在）。带上 existing_id，接口层可以提示"去打那份"。

    name_key 已经过归一化，所以"西红柿 炒蛋"与"西红柿炒蛋"算同一道菜。
    """

    def __init__(self, name: str, existing_id: int | None) -> None:
        super().__init__(f"菜谱已存在：{name}")
        self.name = name
        self.existing_id = existing_id


@dataclass(frozen=True)
class Recipe:
    """一份菜谱（数据库一行的领域形态；数组字段是元组，防手脚）。"""

    id: int
    name: str
    ingredients: tuple[str, ...]
    steps: tuple[str, ...]
    tags: tuple[str, ...]
    time_minutes: int | None
    source: str
    origin_url: str
    note: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """给接口层/前端序列化用（JSON 可直传）。"""
        return {
            "id": self.id,
            "name": self.name,
            "ingredients": list(self.ingredients),
            "steps": list(self.steps),
            "tags": list(self.tags),
            "time_minutes": self.time_minutes,
            "source": self.source,
            "origin_url": self.origin_url,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ------------------------------------------------------------------ 清洗与工具


def normalize_name_key(name: str) -> str:
    """菜名归一化：NFKC（全角→半角）→ 去掉所有空白 → casefold（小写）。"""
    return "".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()


def clean_items(value: Iterable[str] | None, *, what: str = "列表") -> tuple[str, ...]:
    """数组字段清洗：去空、去重（保留首次出现顺序）、单项截断、整表截断。

    刻意拒绝裸字符串：`steps="第一步"` 会被逐字拆开，是个安静的灾难，
    所以直接报 TypeError（接口层负责把表单文本拆成列表再传进来）。
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{what}必须是字符串序列（列表），不能是单个字符串")

    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > MAX_ITEM_LENGTH:
            text = text[:MAX_ITEM_LENGTH]
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_ITEMS:
            break
    return tuple(out)


def _clean_name(name: str) -> str:
    text = str(name).strip()
    if not text:
        raise ValueError("菜名不能为空")
    return text


def _clean_time(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"time_minutes 必须是正整数或 None，收到：{value!r}") from None
    if minutes < 1:
        raise ValueError(f"time_minutes 必须大于 0，收到：{minutes}")
    return minutes


def _clean_text(value: Any) -> str:
    return str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_dump(items: Iterable[str]) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _row_to_recipe(row: sqlite3.Row) -> Recipe:
    return Recipe(
        id=row["id"],
        name=row["name"],
        ingredients=tuple(json.loads(row["ingredients"])),
        steps=tuple(json.loads(row["steps"])),
        tags=tuple(json.loads(row["tags"])),
        time_minutes=row["time_minutes"],
        source=row["source"],
        origin_url=row["origin_url"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ------------------------------------------------------------------ 增删改查


def create_recipe(
    storage: Storage,
    *,
    name: str,
    ingredients: Iterable[str] | None = (),
    steps: Iterable[str] | None = (),
    tags: Iterable[str] | None = (),
    time_minutes: int | None = None,
    source: str = "user",
    origin_url: str = "",
    note: str = "",
) -> Recipe:
    """入库一份菜谱。撞名（归一化后）抛 DuplicateRecipe。"""
    clean_name = _clean_name(name)
    name_key = normalize_name_key(clean_name)
    if source not in SOURCES:
        raise ValueError(f"source 只能是 {SOURCES} 之一，收到：{source!r}")
    clean_minutes = _clean_time(time_minutes)
    clean_ingredients = clean_items(ingredients, what="ingredients")
    clean_steps = clean_items(steps, what="steps")
    clean_tags = clean_items(tags, what="tags")

    now = _now()
    try:
        cursor = storage.execute(
            """
            INSERT INTO recipes
                (name, name_key, ingredients, steps, tags, time_minutes,
                 source, origin_url, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_name,
                name_key,
                _json_dump(clean_ingredients),
                _json_dump(clean_steps),
                _json_dump(clean_tags),
                clean_minutes,
                source,
                _clean_text(origin_url),
                _clean_text(note),
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        existing = storage.query_one(
            "SELECT id FROM recipes WHERE name_key = ?", (name_key,)
        )
        raise DuplicateRecipe(clean_name, existing["id"] if existing else None) from None

    return Recipe(
        id=int(cursor.lastrowid),
        name=clean_name,
        ingredients=clean_ingredients,
        steps=clean_steps,
        tags=clean_tags,
        time_minutes=clean_minutes,
        source=source,
        origin_url=_clean_text(origin_url),
        note=_clean_text(note),
        created_at=now,
        updated_at=now,
    )


def get_recipe(storage: Storage, recipe_id: int) -> Recipe | None:
    row = storage.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    return _row_to_recipe(row) if row is not None else None


def update_recipe(
    storage: Storage,
    recipe_id: int,
    *,
    name: Any = _UNSET,
    ingredients: Any = _UNSET,
    steps: Any = _UNSET,
    tags: Any = _UNSET,
    time_minutes: Any = _UNSET,
    source: Any = _UNSET,
    origin_url: Any = _UNSET,
    note: Any = _UNSET,
) -> Recipe | None:
    """局部更新：只有显式传了的字段才会被改；记录不存在返回 None。

    `time_minutes=None` 是"清掉时间"，不是"没传"——区分用 _UNSET 哨兵。
    改名字改到撞名会抛 DuplicateRecipe 且原记录不动（UPDATE 失败即未执行）。
    """
    row = storage.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if row is None:
        return None

    values: dict[str, Any] = {
        "name": row["name"],
        "name_key": row["name_key"],
        "ingredients": row["ingredients"],
        "steps": row["steps"],
        "tags": row["tags"],
        "time_minutes": row["time_minutes"],
        "source": row["source"],
        "origin_url": row["origin_url"],
        "note": row["note"],
    }
    if name is not _UNSET:
        values["name"] = _clean_name(name)
        values["name_key"] = normalize_name_key(values["name"])
    if ingredients is not _UNSET:
        values["ingredients"] = _json_dump(clean_items(ingredients, what="ingredients"))
    if steps is not _UNSET:
        values["steps"] = _json_dump(clean_items(steps, what="steps"))
    if tags is not _UNSET:
        values["tags"] = _json_dump(clean_items(tags, what="tags"))
    if time_minutes is not _UNSET:
        values["time_minutes"] = _clean_time(time_minutes)
    if source is not _UNSET:
        if source not in SOURCES:
            raise ValueError(f"source 只能是 {SOURCES} 之一，收到：{source!r}")
        values["source"] = source
    if origin_url is not _UNSET:
        values["origin_url"] = _clean_text(origin_url)
    if note is not _UNSET:
        values["note"] = _clean_text(note)

    try:
        storage.execute(
            """
            UPDATE recipes SET name = ?, name_key = ?, ingredients = ?, steps = ?,
                tags = ?, time_minutes = ?, source = ?, origin_url = ?, note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                values["name"],
                values["name_key"],
                values["ingredients"],
                values["steps"],
                values["tags"],
                values["time_minutes"],
                values["source"],
                values["origin_url"],
                values["note"],
                _now(),
                recipe_id,
            ),
        )
    except sqlite3.IntegrityError:
        existing = storage.query_one(
            "SELECT id FROM recipes WHERE name_key = ?", (values["name_key"],)
        )
        raise DuplicateRecipe(values["name"], existing["id"] if existing else None) from None
    return get_recipe(storage, recipe_id)


def delete_recipe(storage: Storage, recipe_id: int) -> bool:
    """删除；返回是否真的删掉了（不存在返回 False，不抛）。"""
    cursor = storage.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return cursor.rowcount > 0


def _where_clause(source: str | None, tag: str | None) -> tuple[str, list[Any]]:
    """source / tag 过滤的 WHERE 子句。

    tag 匹配 JSON 数组里的**完整一项**（`%"家常"%` 的形状），
    不是子串匹配；tag 里含引号的位置不保证语义（标签本身很罕见这种写法）。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if tag is not None:
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))


def list_recipes(
    storage: Storage,
    *,
    source: str | None = None,
    tag: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    shuffle: bool = False,
) -> list[Recipe]:
    """列表，支持 source / tag 过滤与分页。

    `shuffle=True` 时改用 `ORDER BY random()`：第 48 步的首页「换一批」要的就是
    "再给我 6 道不一样的"。个人菜谱库是几十到几千行，`random()` 的一次
    全表排序完全可接受；真到几万行再换成"按 id 哈希取模"之类的便宜抽样。
    """
    where, params = _where_clause(source, tag)
    limit, offset = _page(limit, offset)
    order = "random()" if shuffle else "updated_at DESC, id DESC"
    rows = storage.query(
        f"SELECT * FROM recipes{where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_row_to_recipe(row) for row in rows]


def count_recipes(
    storage: Storage, *, source: str | None = None, tag: str | None = None
) -> int:
    where, params = _where_clause(source, tag)
    row = storage.query_one(f"SELECT count(*) AS n FROM recipes{where}", params)
    return int(row["n"])


# ------------------------------------------------------------------ 检索


def expand_terms(storage: Storage, term: str) -> tuple[str, ...]:
    """一个检索词 → 同组词（自身 + 别名/主名），按拼音无关的字典序返回。

    例：`番茄` → `("西红柿", "番茄")`；`土豆` → `("土豆", "洋芋", "马铃薯")`。
    没登记的普通词原样返回。别名表只有几十行，整表读没有性能问题。
    """
    term = str(term).strip()
    if not term:
        return ()
    pairs = storage.query("SELECT term, canonical FROM ingredient_aliases")
    canonical = term
    for row in pairs:
        if row["term"] == term:
            canonical = row["canonical"]
            break
    group = {term, canonical}
    for row in pairs:
        if row["canonical"] == canonical:
            group.add(row["term"])
    return tuple(sorted(group))


def _fts_phrase(term: str) -> str:
    """FTS5 前缀短语：`"西红柿"*`（引号内加倍转义）。"""
    escaped = term.replace('"', '""')
    return f'"{escaped}"*'


def _fts_match(groups: list[tuple[str, ...]]) -> str:
    """词组列表 → FTS5 MATCH 表达式：组内 OR（别名），组间 AND（多词）。"""
    return " AND ".join(
        "(" + " OR ".join(_fts_phrase(term) for term in group) + ")"
        for group in groups
    )


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _like_condition(groups: list[tuple[str, ...]]) -> tuple[str, list[Any]]:
    """LIKE 回退的 WHERE：每个词组内 OR（别名），组间 AND（多词）。"""
    clauses: list[str] = []
    params: list[Any] = []
    for group in groups:
        alternatives: list[str] = []
        for term in group:
            pattern = f"%{_escape_like(term)}%"
            alternatives.append(
                "(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in SEARCH_COLUMNS) + ")"
            )
            params.extend([pattern] * len(SEARCH_COLUMNS))
        clauses.append("(" + " OR ".join(alternatives) + ")")
    return " AND ".join(clauses), params


def _search_terms(storage: Storage, text: str) -> list[tuple[str, ...]]:
    """检索文本 → 词组列表（每个词展开别名）；空输入返回空列表。"""
    words = [word for word in str(text).split() if word.strip()]
    if not words:
        return []
    return [group for group in (expand_terms(storage, w) for w in words) if group]


def _search_recipe_ids(storage: Storage, groups: list[tuple[str, ...]]) -> list[int]:
    """命中的菜谱 id：**FTS 前缀命中优先，LIKE 子串命中补全**（去重、保序）。

    为什么必须两条路一起用（第 48 步实测到的真实缺陷）：
    unicode61 分词器把连续 CJK 当成**一个词**，`紫菜蛋花汤`就是一个 token，
    而 FTS 的前缀短语只能匹配"以该词开头"。于是：

        搜「紫菜」→ 命中     （是 token 的前缀）
        搜「蛋花」→ **不命中**（词在中间）
        搜「蛋」  → **不命中**

    对"这顿吃什么"的搜索框来说，"搜不到"就等于坏了；更糟的是
    这条表现**取决于环境**——没有 FTS5 时走 LIKE（`%蛋花%`，没有位置限制）
    反而能搜到。同一个库换个解释器结果就不一样，是最难排查的那种 bug。

    所以这里合并两条路：FTS 给出带 bm25 排序的前缀命中，LIKE 再补上
    "词在中间"的那些。个人菜谱库是几百到几千条，LIKE 全表扫一遍的代价
    可以接受；真要上几万条，再改成 SQL 层 UNION + 索引。
    """
    ordered: list[int] = []
    seen: set[int] = set()

    if storage.fts_available:
        rows = storage.query(
            "SELECT r.id AS id FROM recipes_fts "
            "JOIN recipes r ON r.id = recipes_fts.rowid "
            "WHERE recipes_fts MATCH ? "
            "ORDER BY bm25(recipes_fts)",
            (_fts_match(groups),),
        )
        for row in rows:
            recipe_id = int(row["id"])
            if recipe_id not in seen:
                seen.add(recipe_id)
                ordered.append(recipe_id)

    where, params = _like_condition(groups)
    rows = storage.query(
        f"SELECT id FROM recipes WHERE {where} ORDER BY updated_at DESC, id DESC",
        params,
    )
    for row in rows:
        recipe_id = int(row["id"])
        if recipe_id not in seen:
            seen.add(recipe_id)
            ordered.append(recipe_id)

    return _rank_name_hits_first(storage, ordered, groups)


def _rank_name_hits_first(
    storage: Storage, ordered: list[int], groups: list[tuple[str, ...]]
) -> list[int]:
    """把"菜名里含关键词"的排到前面（同层内保持原有顺序）。

    为什么需要这一步（第 50 步换库后实测到的真问题）：

    FTS 的前缀命中天然排在 LIKE 子串命中之前，于是**名字里就有关键词的菜
    反而垫底**。搜「豆腐」时的真实顺序是：

        过桥米线（食材里的"豆腐皮"，前缀命中）→ … → 麻婆豆腐（第 14 位）

    对"我想吃豆腐"的人来说这是错的：他要的是豆腐菜，不是配料里有豆腐皮的面。
    所以合并两条路之后再按"名字命中 > 其它命中"稳定重排一次。

    不直接用 FTS 列权重（`bm25(fts, 10, 1, 1, 1)`）的原因：名字里含关键词
    的菜可能**根本没进 FTS 结果**（词在中间时不命中），列权重对它无效。
    """
    if not ordered:
        return ordered

    terms = [word for group in groups for word in group if word]
    if not terms:
        return ordered

    placeholders = ",".join("?" * len(ordered))
    rows = storage.query(
        f"SELECT id, name FROM recipes WHERE id IN ({placeholders})", ordered
    )
    names = {int(row["id"]): str(row["name"]) for row in rows}

    def sort_key(item: tuple[int, int]) -> tuple[int, int]:
        position, recipe_id = item
        name = names.get(recipe_id, "")
        hit = any(term in name for term in terms)
        return (0 if hit else 1, position)

    ranked = sorted(enumerate(ordered), key=sort_key)
    return [recipe_id for _, recipe_id in ranked]


def search_recipes(
    storage: Storage,
    text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[Recipe]:
    """按关键词检索知识库：前缀命中走 FTS 排序，子串命中由 LIKE 补全。

    多词 = AND（"番茄 牛肉"要两个都在）；每个词自动带别名展开
    （搜"番茄"命中"西红柿炒蛋"）。空白/无结果都返回空列表，不抛。
    """
    groups = _search_terms(storage, text)
    if not groups:
        return []
    limit, offset = _page(limit, offset)

    # 先切页再取行：只有这一页的 id 会进 SQL，不用把整个命中集合搬进内存
    page_ids = _search_recipe_ids(storage, groups)[offset:offset + limit]
    if not page_ids:
        return []

    placeholders = ",".join("?" * len(page_ids))
    rows = storage.query(
        f"SELECT * FROM recipes WHERE id IN ({placeholders})", page_ids
    )
    by_id = {int(row["id"]): _row_to_recipe(row) for row in rows}
    # 按命中顺序还原（IN 查询不保证顺序）
    return [by_id[recipe_id] for recipe_id in page_ids if recipe_id in by_id]


def count_search(storage: Storage, text: str) -> int:
    """检索命中的总数（第 48 步加的）。

    列表接口要回一个可信的 `total`：没有它就只能拿"库里一共多少道"当总数，
    于是界面会出现"共 30 道"却只列 3 条的怪象。

    与 `search_recipes` **共用** `_search_recipe_ids`，
    所以"命中"的定义只有一处，改了检索不会忘记改计数。
    """
    groups = _search_terms(storage, text)
    if not groups:
        return 0
    return len(_search_recipe_ids(storage, groups))


# ------------------------------------------------------------------ schema


#: 别名种子（term → canonical）：约 25 条常见"一物多名"。
#: 只说"同一样东西的两种叫法"，口味相近但不同的（如青椒/尖椒）不收，
#: 免得把检索结果搅浑。
_ALIAS_SEED = """
INSERT INTO ingredient_aliases (term, canonical) VALUES
    ('西红柿', '番茄'),
    ('马铃薯', '土豆'),
    ('洋芋', '土豆'),
    ('芫荽', '香菜'),
    ('猪脚', '猪蹄'),
    ('蹄髈', '肘子'),
    ('抄手', '馄饨'),
    ('云吞', '馄饨'),
    ('花菜', '菜花'),
    ('花椰菜', '菜花'),
    ('西蓝花', '西兰花'),
    ('芝士', '奶酪'),
    ('起司', '奶酪'),
    ('忌廉', '奶油'),
    ('生抽', '酱油'),
    ('老抽', '酱油'),
    ('圆白菜', '卷心菜'),
    ('包菜', '卷心菜'),
    ('冬菇', '香菇'),
    ('淮山', '山药'),
    ('蛋白', '蛋清'),
    ('奇异果', '猕猴桃'),
    ('青瓜', '黄瓜'),
    ('凉瓜', '苦瓜'),
    ('藕', '莲藕')
"""

#: schema v1：基础表（FTS 影子表刻意不在迁移里，见模块 docstring）
SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE,
        ingredients TEXT NOT NULL DEFAULT '[]',
        steps TEXT NOT NULL DEFAULT '[]',
        tags TEXT NOT NULL DEFAULT '[]',
        time_minutes INTEGER,
        source TEXT NOT NULL DEFAULT 'user',
        origin_url TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_recipes_source ON recipes(source)",
    "CREATE INDEX idx_recipes_updated ON recipes(updated_at DESC)",
    """
    CREATE TABLE ingredient_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL UNIQUE,
        canonical TEXT NOT NULL
    )
    """,
    _ALIAS_SEED,
)

#: FTS5 影子表与同步触发器（外部内容表：索引读 recipes 本体，不做第二份真源）
FTS_V1: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
        name, ingredients, tags, note,
        content='recipes', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
        INSERT INTO recipes_fts(rowid, name, ingredients, tags, note)
        VALUES (new.id, new.name, new.ingredients, new.tags, new.note);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, name, ingredients, tags, note)
        VALUES ('delete', old.id, old.name, old.ingredients, old.tags, old.note);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, name, ingredients, tags, note)
        VALUES ('delete', old.id, old.name, old.ingredients, old.tags, old.note);
        INSERT INTO recipes_fts(rowid, name, ingredients, tags, note)
        VALUES (new.id, new.name, new.ingredients, new.tags, new.note);
    END
    """,
)

FTS_TRIGGERS = ("recipes_ai", "recipes_ad", "recipes_au")


def _sync_fts(storage: Storage) -> None:
    """建好 FTS 影子表与触发器；索引可能缺行时重建。

    "可能缺行"的两种情况：影子表是刚建的（老数据都在它出生之前）；
    触发器缺着（环境一度没有 FTS5，这期间的增改没被索引）。
    两种情况都靠 `'rebuild'` 命令整体重建，宁可贵一次、不赌一致性。
    """
    names = {
        row["name"]
        for row in storage.query(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        )
    }
    need_rebuild = "recipes_fts" not in names or not set(FTS_TRIGGERS) <= names
    for statement in FTS_V1:
        storage.execute(statement)
    if need_rebuild:
        storage.execute("INSERT INTO recipes_fts(recipes_fts) VALUES ('rebuild')")


def ensure_schema(storage: Storage) -> None:
    """把库升到最新结构并同步 FTS 影子表；幂等，可每次启动都调。

    FTS5 不可用时：不建影子表、清掉可能残留的触发器
    （触发器指着不存在的 FTS 模块会让普通写入也报错），检索自动走 LIKE。
    """
    # **先把所有登记迁移的模块导入进来**（第 91 步）。
    #
    # 迁移表是靠 `import` 时 `register_migration()` 登记的，所以"注册表里有没有 v3"
    # 取决于谁先 import 过 —— 实测踩过：只导入了 recipe_library 而没导入 core.history 时，
    # 真库停在 v2、`chat_messages` 根本没建出来，写消息直接 "no such table"。
    # 这一层不能依赖调用方的导入顺序。
    from core import history as _history

    _history.register()      # 幂等；显式登记，不依赖"谁先 import 过"
    from core import app_settings as _app_settings

    _app_settings.register()  # v4：应用级小配置（当前选中的模型）
    from core import recipe_session_meta as _recipe_meta

    _recipe_meta.register()   # v5：食谱会话的小状态（recent_tags / 忌口快照）
    from core import usage as _usage

    _usage.register()         # v6：模型调用用量账本
    from core import checkin as _checkin

    _checkin.register()       # v8：签到记录与补签卡（第 40 步补做落盘）

    storage.apply_migrations()
    # 第 182 步：向量表也在这里建（幂等）。放在 ensure_schema 而不是 main，
    # 这样"用内存库跑测试/评测"的场景同样有这张表。
    from core import recipe_vectors as _vectors

    _vectors.ensure_vector_schema(storage)
    if storage.fts_available:
        _sync_fts(storage)
    else:
        for trigger in FTS_TRIGGERS:
            storage.execute(f"DROP TRIGGER IF EXISTS {trigger}")


# import 时把 v1 登记进全局迁移表（main.py 打开库后 ensure_schema 应用）。
register_migration(SCHEMA_V1, version=1)


# ================================================================== 忌口词条（v2）
#
# 第 50 步：右栏改成"点标签"，其中**只把忌口/不吃的东西长期记住**
# （用户的理由：口味有时很随机，记住了反而碍事）。
#
# 刻意存成**一颗颗词条**而不是一段自由文本：
#   - 词条能直接拿去拼 SQL（排除 / 过滤），一段话只能靠字符串匹配猜；
#   - 词条能做"点选"交互，一段话不能。
# 这也和之前讨论过的"食材实体化"方向一致。

#: 忌口词条的**大众选项**（右栏第一个标签组用这一份，不做标签墙）。
#: 只收家常菜里最常见、且能对上 `CATEGORY_TERMS` 大类的那些。
AVOID_OPTIONS: tuple[str, ...] = (
    "香菜", "海鲜", "内脏", "牛羊肉", "辣", "蛋", "奶", "坚果",
)

#: 「这次想要」的标签组。分组只用于排版，值都是菜谱库里真实存在的 tag
#: （点击后能真的筛出结果，不会点了没菜）。
#:
#: 50f 换库后重新对过一遍：原来的「工具」组（小电锅/电饭煲/明火/没冰箱）是照着
#: 旧示例菜谱写的，新库是《中国菜谱大全》96 道家常/地方菜，**没有一道带这些标签**，
#: 点了一定空——所以换成「菜系」（每个菜系都有 4~7 道，点了有菜）。
#: 「怎么吃」不是标签而是行为偏好（外卖/自己做会改变推荐口径），保留。
#:
#: 第 52 步：每组第三项是 **exclusive（互斥）**——为 True 时组内只能亮一个
#: （用户要求"三个标签只能亮一个，不能一起选"）。
#: 「时间」「怎么吃」天然互斥：15 分钟内 和 30 分钟以上 不可能同时成立。
#: 「口味」「菜系」「场景」可以多选（又辣又下饭是正常的）。
#: 同时按用户要求给「时间」加了「30 分钟以上」——有时间慢慢做的时候用。
WANT_GROUPS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("口味", ("清淡", "微辣", "辣", "酸甜"), False),
    ("菜系", ("川菜", "粤菜", "湘菜", "鲁菜", "苏菜", "浙菜", "闽菜", "徽菜"), False),
    ("场景", ("下饭", "主食", "汤", "素"), False),
    ("时间", ("15 分钟内", "30 分钟内", "30 分钟以上"), True),
    ("怎么吃", ("自己做", "外卖", "都行"), True),
)

SCHEMA_V2: tuple[str, ...] = (
    # ⚠️ **这一版已经发布过，内容不许再改。**
    #
    # 第 50 步中途我犯过一次错：v2 最初写成 `term TEXT PRIMARY KEY`，
    # 后来为了"保住点选顺序"又改成带 `id INTEGER PRIMARY KEY AUTOINCREMENT`。
    # 但迁移只按 user_version 跑一次——已经升到 2 的库（用户的 data/pet.db）
    # 不会重跑，于是代码里 `ORDER BY id` 撞上"没有 id 列"，
    # `/preferences` 直接 500，右栏一片空白（用户截图里就是空的）。
    #
    # 正确做法：**改形状只能加新版本，不能改已发布的迁移**。
    # 这里退回最初发布的形状；"插入顺序"改用 SQLite 自带的 `rowid` 来排——
    # `term TEXT PRIMARY KEY` 的表同样有递增 rowid，两种历史形状都能用，
    # 所以这次**不需要 v3**，老库与新库都能跑。
    """
    CREATE TABLE recipe_aversions (
        term TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
)

register_migration(SCHEMA_V2, version=2)


def _clean_avoid_term(term: str) -> str:
    """忌口词条归一化：去掉空白与首尾标点；空串返回空。"""
    text = str(term).strip().strip("、，,。. ")
    return text[:40]


def list_aversions(storage: Storage) -> list[str]:
    """当前记住的忌口词条，**按加入顺序**（= 用户点选的先后）。

    用 `rowid` 而不是自建 id 列：迁移发布后不能改形状，
    而 rowid 是 SQLite 每张普通表都有的插入序，历史上两种形状都能读。
    """
    rows = storage.query("SELECT term FROM recipe_aversions ORDER BY rowid")
    return [row["term"] for row in rows]


def set_aversions(storage: Storage, terms: Sequence[str]) -> list[str]:
    """**整份替换**忌口词条（幂等；返回替换后的结果）。

    为什么是整份替换而不是逐条增删：右栏是一排可以点亮的按钮，
    前端每次提交的就是"当前亮着的那些"。逐条增删会让"取消勾选"变成
    一个容易漏掉的 DELETE，整份替换只需要一次比较。
    """
    now = _now()
    cleaned: list[str] = []
    for term in terms:
        text = _clean_avoid_term(term)
        if text and text not in cleaned:
            cleaned.append(text)

    with storage.transaction() as conn:
        conn.execute("DELETE FROM recipe_aversions")
        for term in cleaned:
            conn.execute(
                "INSERT INTO recipe_aversions (term, created_at) VALUES (?, ?)",
                (term, now),
            )
    return cleaned


def add_aversions(storage: Storage, terms: Sequence[str]) -> list[str]:
    """把词条**追加**进忌口库（已有的不重复插），返回追加后的完整列表。

    与 `set_aversions` 的分工（第 63 步）：
    - `set_aversions` 是**整份替换**，服务右栏"点亮 / 取消"——**删除只走它**；
    - `add_aversions` 只增不删，服务"用户在对话里说出过敏原"这条路径。

    为什么不能拿 `set_aversions` 来加：喂给它一份只含新词的列表，会把用户
    在右栏点亮的忌口**全部抹掉**。所以这两条路径必须分开，但**共用同一张表**，
    于是"说出来的"和"勾选的"享有同一套语义：在右栏取消勾选就能删掉。
    """
    now = _now()
    with storage.transaction() as conn:
        existing = {
            row["term"] for row in conn.execute("SELECT term FROM recipe_aversions")
        }
        for term in terms:
            text = _clean_avoid_term(term)
            if not text or text in existing:
                continue
            conn.execute(
                "INSERT INTO recipe_aversions (term, created_at) VALUES (?, ?)",
                (text, now),
            )
            existing.add(text)
    return list_aversions(storage)
