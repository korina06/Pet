"""内置种子菜谱：数据质量、四类覆盖面、导入的幂等与「一条都不导」语义。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core.recipe_library import count_recipes, create_recipe, get_recipe, search_recipes  # noqa: E402
from core.recipe_seed import (  # noqa: E402
    SEED_PATH,
    TAG_VOCABULARY,
    SeedError,
    import_seed,
    load_seed,
    seed_recipes,
    validate_seed,
)
from core.storage import Storage  # noqa: E402


def _memory_db() -> Storage:
    """建一个**已应用迁移**的内存库。

    必须调 ensure_schema：不建表的话第一次 create_recipe 就 OperationalError
    （"no such table: recipes"），测出来的会是环境问题而不是被测行为。
    """
    from core.recipe_library import ensure_schema

    db = Storage(":memory:")
    ensure_schema(db)
    return db


def _seed_items() -> list[dict]:
    return load_seed()


# ------------------------------------------------------------------ 数据质量


def test_seed_file_exists_and_is_readable():
    assert SEED_PATH.exists(), f"种子文件不存在：{SEED_PATH}"
    items = _seed_items()
    # 第 50 步（50f）换成《中国菜谱大全》后是 96 道；这里给一个区间而不是死数，
    # 免得每次改数据都要动断言，但也不能让种子悄悄缩水到几十条。
    assert 90 <= len(items) <= 130, f"种子条数应在 96 上下，实际 {len(items)}"
    assert all(isinstance(item, dict) for item in items)


def test_seed_passes_its_own_validator():
    """最要紧的一条：整份种子必须零问题。失败时把清单打出来。"""
    problems = validate_seed(_seed_items())
    assert problems == [], "种子数据有问题：\n- " + "\n- ".join(problems)


def test_seed_has_no_duplicate_names():
    names = [item["name"].strip() for item in _seed_items()]
    assert len(set(names)) == len(names), "菜名有重复"
    # 归一化后也不能撞（"西红柿 炒蛋" vs "西红柿炒蛋"）
    from core.recipe_library import normalize_name_key

    keys = [normalize_name_key(name) for name in names]
    assert len(set(keys)) == len(keys)


def test_every_ingredient_carries_an_amount():
    """用量必须能看出来：没有用量，将来的采购清单没法算。

    **口径与校验层共用一份 `has_amount`**：这里以前自己写了个"必须含阿拉伯数字"，
    结果真实菜谱里的"冰水一盆""盐适量"全被误伤——两处各写一套阈值就会漂移。
    """
    from core.recipe_seed import has_amount

    for item in _seed_items():
        for entry in item["ingredients"]:
            assert has_amount(entry), f"{item['name']}：{entry!r} 没有用量"


def test_steps_are_actually_actionable():
    """步骤不能是"炒熟即可"这种空话。阈值同样取自校验层，不在这里另写一个数字。"""
    from core.recipe_seed import MIN_STEP_CHARS

    for item in _seed_items():
        for step in item["steps"]:
            assert len(step.strip()) >= MIN_STEP_CHARS, (
                f"{item['name']}：步骤太短 {step!r}"
            )


def test_all_tags_come_from_the_vocabulary():
    allowed = set(TAG_VOCABULARY)
    for item in _seed_items():
        unknown = [tag for tag in item["tags"] if tag not in allowed]
        assert not unknown, f"{item['name']} 用了词表外的标签：{unknown}"


def test_seed_json_carries_content_only_not_provenance():
    """JSON 里**不写** source / origin_url：来源由导入器统一盖章。

    这样 96 条里不会出现"漏写 source"的漏网之鱼，也避免有人手改成别的来源。
    """
    for item in _seed_items():
        assert "source" not in item, f"{item['name']} 不该在 JSON 里写 source"
        assert "origin_url" not in item
        assert set(item) <= {"name", "ingredients", "steps", "tags", "time_minutes", "note"}


def test_seed_covers_the_promised_spread():
    """覆盖面：第 50 步要靠这份库出候选，某类太少就会"搜什么都是那几道"。

    50f 换库后按**新数据**重立口径：一份按省份整理的地方菜谱，
    覆盖的维度自然变成"菜系 + 口味 + 主食/汤"，
    不该再拿旧示例菜谱的"小电锅/电饭煲"去要求它（那是另一个产品假设）。
    """
    items = _seed_items()

    def hits(*names: str) -> int:
        return len([item for item in items if set(item["tags"]) & set(names)])

    # 八大菜系每个都要有菜（"想吃川菜"必须真有川菜）
    for cuisine in ("粤菜", "川菜", "鲁菜", "苏菜", "浙菜", "闽菜", "湘菜", "徽菜"):
        assert hits(cuisine) >= 4, f"{cuisine} 太少（{hits(cuisine)} 道）"
    assert hits("清淡") >= 3, "清淡类太少"
    assert hits("辣", "微辣") >= 10, "辣味类太少（川湘菜应当撑起这一档）"
    assert hits("汤") >= 5, "汤类太少"
    assert hits("主食") >= 10, "主食类太少"
    assert hits("下饭") >= 10, "下饭类太少"
    # 口味这一档必须齐（右栏「口味」那组要能点出菜来）
    for taste in ("清淡", "微辣", "辣", "酸甜"):
        assert hits(taste) >= 2, f"口味「{taste}」几乎没有菜"


def test_seed_recipes_preview_does_not_touch_the_database():
    """`seed_recipes()` 只解析、不落库（"导入前预览"用）。"""
    recipes = seed_recipes()
    assert len(recipes) >= 25
    assert all(recipe.source == "seed" for recipe in recipes)
    assert all(recipe.ingredients and recipe.steps for recipe in recipes)


# ------------------------------------------------------------------ 导入语义


def test_import_adds_everything_and_is_idempotent():
    db = _memory_db()
    try:
        items = _seed_items()
        first = import_seed(db)
        assert first["added"] == len(items)
        assert first["skipped"] == 0
        assert count_recipes(db) == len(items)

        second = import_seed(db)
        assert second["added"] == 0, "重复导入不该再新增"
        assert second["skipped"] == len(items)
        assert count_recipes(db) == len(items), "总数不该变"
    finally:
        db.close()


def test_import_stamps_the_source_as_seed():
    db = _memory_db()
    try:
        import_seed(db)
        from core.recipe_library import list_recipes

        rows = list_recipes(db)
        assert rows, "导入后应有数据"
        assert {row.source for row in rows} == {"seed"}
        assert {row.origin_url for row in rows} == {""}
    finally:
        db.close()


def test_import_never_overwrites_a_user_recipe_with_the_same_name():
    """用户自己录的同名菜**必须保住**：导入是补充，不是覆盖。

    （用户录的版本更贴合他自己的口味，被示例数据盖掉会很恼火。）
    """
    db = _memory_db()
    try:
        items = _seed_items()
        target = items[0]["name"]
        mine = create_recipe(
            db, name=target, ingredients=["我的 1 份"], steps=["我的做法"], source="user"
        )

        result = import_seed(db)
        assert result["skipped"] >= 1

        kept = get_recipe(db, mine.id)
        assert kept is not None
        assert kept.source == "user"
        assert kept.ingredients == ("我的 1 份",), "用户录的内容被覆盖了"
        assert count_recipes(db, source="user") == 1
    finally:
        db.close()


def test_imported_seed_is_searchable_including_alias():
    """种子导进去之后要真的能被搜到——包括别名（番茄 ↔ 西红柿）。"""
    db = _memory_db()
    try:
        import_seed(db)
        # 别名用例：搜任一种叫法都该命中含另一种叫法的菜
        for keyword in ("番茄", "西红柿", "鸡蛋", "汤"):
            hits = search_recipes(db, keyword)
            assert hits, f"搜「{keyword}」一条都没命中，种子内容或检索有问题"
    finally:
        db.close()


# ------------------------------------------------------------------ 校验器本身


def _good_recipe(name: str = "测试菜") -> dict:
    """一条**合法**的种子条目（各条坏数据用例都在它身上改一处）。"""
    return {
        "name": name,
        "ingredients": ["鸡蛋 2 个", "盐 1g"],
        "steps": ["鸡蛋打散加盐拌匀", "热锅下油炒到凝固"],
        "tags": ["下饭", "炒"],
        "time_minutes": 10,
        "note": "",
    }


def _write_seed(tmp_path: Path, recipes: list[dict]) -> Path:
    """把给定条目写成临时种子文件（用 pytest 的 tmp_path，用完真删）。"""
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"recipes": recipes}, ensure_ascii=False), encoding="utf-8")
    return path


def test_validator_reports_each_kind_of_problem():
    """校验器要真的挑得出毛病（不然"种子零问题"这条断言是空的）。"""
    cases = {
        "缺 name": {**_good_recipe(), "name": ""},
        "用量没数字": {**_good_recipe(), "ingredients": ["鸡蛋", "盐 1g"]},
        "步骤太短": {**_good_recipe(), "steps": ["炒熟", "装盘"]},
        "自创标签": {**_good_recipe(), "tags": ["我自创的标签"]},
        "时间不是整数": {**_good_recipe(), "time_minutes": "十分钟"},
        "时间越界": {**_good_recipe(), "time_minutes": 9999},
        "ingredients 不是数组": {**_good_recipe(), "ingredients": "鸡蛋 2 个"},
        "tags 为空": {**_good_recipe(), "tags": []},
    }
    for label, item in cases.items():
        problems = validate_seed([item])
        assert problems, f"「{label}」这种坏数据居然没被挑出来"


def test_validator_catches_duplicate_names():
    problems = validate_seed([_good_recipe("西红柿炒蛋"), _good_recipe("西红柿 炒蛋")])
    assert any("重复" in problem for problem in problems), problems


def test_invalid_seed_is_rejected_atomically(tmp_path):
    """数据有毛病时**一条都不导**：不许把半套种子灌进库里。"""
    broken = [_good_recipe("好菜"), {**_good_recipe("坏菜"), "tags": ["不存在"]}]
    path = _write_seed(tmp_path, broken)
    db = _memory_db()
    try:
        with pytest.raises(SeedError):
            import_seed(db, path)
        assert count_recipes(db) == 0, "校验失败却已经写进去了"
    finally:
        db.close()


def test_seed_error_is_readable_when_file_is_missing(tmp_path):
    db = _memory_db()
    try:
        with pytest.raises(SeedError) as caught:
            import_seed(db, tmp_path / "不存在的种子.json")
        assert "读不到种子文件" in str(caught.value)
    finally:
        db.close()


def test_seed_error_when_json_is_malformed(tmp_path):
    path = tmp_path / "bad_seed.json"
    path.write_text("{这不是 JSON", encoding="utf-8")
    db = _memory_db()
    try:
        with pytest.raises(SeedError) as caught:
            import_seed(db, path)
        assert "合法 JSON" in str(caught.value)
    finally:
        db.close()


# ------------------------------------------------------------ 整体替换（50f）


def test_replace_seed_swaps_the_seed_but_keeps_user_recipes(tmp_path):
    """换整套菜谱时**只删 `source="seed"`**，用户自己录的一条不能动。

    用户原话："原先那 30 道就不要了"——指的是示例菜谱。
    如果这里把 user 也删了，等于把用户手工录的东西一起清了。
    """
    from core.recipe_library import create_recipe, list_recipes
    from core.recipe_seed import import_seed, replace_seed

    # 两份种子要**放在不同目录**：`_write_seed` 固定写 seed.json，
    # 放同一目录会互相覆盖（第一版测试就是这么把自己搞挂的）。
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_seed = _write_seed(old_dir, [{**_good_recipe("旧示例菜"), "tags": ["下饭"]}])
    new_seed = _write_seed(
        new_dir,
        [
            {**_good_recipe("新菜甲"), "tags": ["川菜"]},
            {**_good_recipe("新菜乙"), "tags": ["粤菜"]},
        ],
    )

    db = _memory_db()
    try:
        import_seed(db, old_seed)
        create_recipe(
            db,
            name="我自己录的菜",
            ingredients=["土豆 2 个"],
            steps=["土豆切丝泡水", "下锅炒熟调味"],
            tags=[],
            time_minutes=10,
            source="user",
        )
        assert count_recipes(db) == 2

        result = replace_seed(db, new_seed)
        assert result["removed"] == 1, "只该删掉那条旧的 seed"
        assert result["added"] == 2

        names = {recipe.name for recipe in list_recipes(db, limit=50)}
        assert names == {"新菜甲", "新菜乙", "我自己录的菜"}
        assert count_recipes(db) == 3
    finally:
        db.close()


def test_replace_seed_refuses_and_keeps_old_data_when_new_seed_is_broken(tmp_path):
    """新种子不合格时**先报错、旧数据原样保留**，绝不能删完再发现导不进去。"""
    from core.recipe_seed import import_seed, replace_seed

    good_dir, broken_dir = tmp_path / "good", tmp_path / "broken"
    good_dir.mkdir()
    broken_dir.mkdir()
    good = _write_seed(good_dir, [_good_recipe("旧示例菜")])
    broken = _write_seed(broken_dir, [{**_good_recipe("坏数据"), "tags": ["自创标签"]}])

    db = _memory_db()
    try:
        import_seed(db, good)
        with pytest.raises(SeedError) as caught:
            replace_seed(db, broken)
        assert "拒绝替换" in str(caught.value), (
            "应当报「新种子有问题」，而不是导入器的通用错误"
        )
        assert count_recipes(db) == 1, "校验失败时不该删掉旧数据"
    finally:
        db.close()


# ------------------------------------------------ 标签血缘（第 56 步 P1-1）


def test_labels_file_carries_provenance():
    """标签必须能追溯：谁标的、哪版提示词、什么时候、复核了几条。

    没有这些字段，半年后没人说得清"这道菜为什么标成微辣"。
    """
    import json

    from core.recipe_seed import LABELS_PATH

    assert LABELS_PATH.exists(), "缺少 core/recipe_labels.json"
    payload = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    provenance = payload["provenance"]
    for key in ("model", "prompt_sha", "prompt_source", "ran_at", "reviewed", "note"):
        assert key in provenance, f"血缘缺字段 {key}"
    assert len(provenance["prompt_sha"]) >= 8, "提示词哈希太短，认不出是哪一版"
    # reviewed=0 是诚实的现状：只抽查过，没有逐条复核
    assert isinstance(provenance["reviewed"], int)
    assert len(payload["labels"]) >= 90, "标签条目数对不上"


def test_labels_file_wins_over_stale_tags_in_the_seed_file(tmp_path):
    """标签拆出去之后，种子里的 tags 只是历史遗留——加载时必须让标签文件优先。

    这条保证"重跑导入脚本"不会把模型标签带回来变成规则标签。
    """
    import json

    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps({"recipes": [{**_good_recipe("测试菜"), "tags": ["下饭"]}]},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {"schema": 1, "provenance": {}, "labels": {"测试菜": ["川菜", "辣"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from core.recipe_seed import load_seed

    items = load_seed(seed_path, labels_path=labels_path)
    assert items[0]["tags"] == ["川菜", "辣"], items[0]["tags"]


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "scripts" / "import_recipe_md.py").is_file(),
    reason="import_recipe_md.py 是一次性导入脚本，第 271 步起只在本机",
)
def test_import_script_refuses_to_overwrite_existing_seed(tmp_path):
    """护栏：目标文件已存在时，导入脚本必须拒绝覆盖（除非 --force）。

    这条是给一个**真实踩过两次**的坑留的：重跑导入脚本会把模型标签
    静默冲回规则标签（清淡 19→4、辣 16→27）。
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    import import_recipe_md

    source = tmp_path / "src.md"
    source.write_text("# 一、粤菜（广东）\n\n## 1. 白切鸡\n\n**食材**：鸡 1 只、姜 1 块。\n\n"
                      "**做法**：\n1. 鸡洗净冷水下锅煮 20 分钟。\n2. 捞出放冰水浸凉切件。\n",
                      encoding="utf-8")
    target = tmp_path / "seed.json"
    target.write_text('{"recipes": []}', encoding="utf-8")

    code = import_recipe_md.main(["x", str(source), str(target)])
    assert code == 1, "目标已存在时必须拒绝"
    assert target.read_text(encoding="utf-8") == '{"recipes": []}', "拒绝时不能写文件"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "scripts" / "import_recipe_md.py").is_file(),
    reason="import_recipe_md.py 是一次性导入脚本，第 271 步起只在本机",
)
def test_import_script_does_not_replace_sys_stdout(tmp_path):
    """`import_recipe_md.main()` **不许**替换全局 `sys.stdout`（评价4 P0-2）。

    真事：那行 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 原来写在
    `main()` 里，于是任何调用方（含 pytest）的捕获流都被换掉；pytest 在 teardown
    关闭那个 buffer 时抛 `ValueError: I/O operation on closed file`，
    表现为"这条测试失败 + 一串 teardown ERROR"，而根因根本不在被测代码里。

    编码包装只该在"当脚本执行"时做（见 `__main__` 块）。
    """
    import sys

    from scripts.import_recipe_md import main

    before = sys.stdout
    target = tmp_path / "seed.json"
    target.write_text('{"recipes": []}', encoding="utf-8")

    try:
        main(["import_recipe_md.py", str(target)])   # 目标已存在、无 --force -> 只打印拒绝
    finally:
        pass

    assert sys.stdout is before, "main() 不该替换调用方的 sys.stdout"
