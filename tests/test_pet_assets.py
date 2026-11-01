"""动作素材的元数据检查：同一角色各段素材的包围盒不许差太多，否则切换时人会忽大忽小。

只比 `static/actions/*.json` 里的数字，不解码视频。
"""

from __future__ import annotations

import json
from pathlib import Path

ACTIONS_DIR = Path(__file__).resolve().parent.parent / "static" / "actions"


def _meta(name: str) -> dict:
    path = ACTIONS_DIR / f"{name}.json"
    assert path.exists(), f"缺少元数据 {path.name}（重新跑 scripts/import_action.py 即可）"
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_action_has_sidecar_metadata():
    """每个 .webm 都要有同名 .json（否则"多大、脚底在哪"就无从校验）。"""
    webms = sorted(ACTIONS_DIR.glob("*.webm"))
    assert webms, "一个动作素材都没有？"

    missing = [path.name for path in webms if not path.with_suffix(".json").exists()]
    assert not missing, f"这些素材缺少元数据：{missing}"


def test_all_actions_share_one_canvas():
    """跨动作必须是同一块画布 —— 否则切换动作时整只宠物会跳大小。"""
    sizes = {}
    for path in sorted(ACTIONS_DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        sizes[meta["name"]] = tuple(meta["canvas"])

    distinct = sorted(set(sizes.values()))
    assert len(distinct) == 1, f"画布不一致：{sizes}"
    assert distinct[0] == (300, 300), f"画布应当是 300x300，实际 {distinct[0]}"


def test_no_action_is_clipped_by_the_canvas():
    """任何动作的**并集包围盒**都不能超出画布 —— 超了就是被裁掉了。

    用户报过两次"场景不完整"：第 165 步摸鱼裁掉了尾巴和笔记本（`--fit height` 的副作用）、
    第 161 步喝茶裁掉了茶桌左侧。画布够宽才不会裁：现在按最宽的场景定了 1.0 方形。
    """
    for path in sorted(ACTIONS_DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        union = (meta["size"] or {}).get("union") or {}
        canvas_w, canvas_h = meta["canvas"]
        assert union["width"] <= canvas_w, (
            f"{meta['name']} 的并集宽 {union['width']} 超过画布 {canvas_w}（会被裁）"
        )
        assert union["height"] <= canvas_h, (
            f"{meta['name']} 的并集高 {union['height']} 超过画布 {canvas_h}（会被裁）"
        )


def test_all_actions_share_the_same_character_height():
    """所有动作的角色**身高必须一致**（第 173 步，学 dsh-pet）。

    dsh-pet 的 `normalize_step03.py` 是"站立高度定缩放"：`scale = 900 / 站立帧高`。
    我们照做（`scale = target_height / 首帧高`），所以"首帧身高"这一列应当一模一样。

    这一条能直接抓住"某段素材里道具把包围盒撑大 → 角色被缩小"的回归 ——
    用户前后三次报"她变小了"，根因就是旧实现用**全片并集**定缩放。
    """
    heights = {}
    for path in sorted(ACTIONS_DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if (meta.get("role") or "action") != "action":
            continue        # 拖拽两段单独对齐（它们不是站姿基准）
        heights[meta["name"]] = meta["size"]["first"]["height"]

    assert heights, "一个普通动作都没找到？"
    lowest = min(heights.values())
    highest = max(heights.values())
    # 容差 8px：`idle` 为了不裁掉她下方那一小块（会超出画布 6px）特意矮了 6px，
    # 这是"宁可小一点也不能裁"的取舍，不算回归。
    assert highest - lowest <= 8, f"各动作角色身高不一致：{heights}"


def test_all_actions_put_the_feet_on_the_same_line():
    """脚底也要落在同一行 —— 否则切动作时她会"跳一下"。"""
    feet = {}
    for path in sorted(ACTIONS_DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if (meta.get("role") or "action") != "action":
            continue
        feet[meta["name"]] = meta["size"]["first"]["bottom"]

    lowest = min(feet.values())
    highest = max(feet.values())
    # 容差 8px：同上（idle 的脚底在 279，其余 284）。
    assert highest - lowest <= 8, f"各动作脚底不一致：{feet}"
