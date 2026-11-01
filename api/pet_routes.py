"""桌宠动作目录：扫描 `static/actions/` 下的 `.webm` 与同名 `.json`，返回 label、图标、
池、权重与角色。

缺文件、json 坏了或字段类型不对一律跳过，一个坏动作不影响整张列表。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pet", tags=["pet"])

#: 动作素材目录（测试里可以用 `app.state.actions_dir` 指到临时目录）
DEFAULT_ACTIONS_DIR = Path(__file__).resolve().parent.parent / "static" / "actions"

#: 允许的池。写错的（比如 "manual " 带空格）一律当归 idle，避免动作凭空消失。
VALID_POOLS = frozenset({"idle", "manual"})

#: 拖拽两段的 role
DRAG_ROLES = ("drag-lift", "drag-swing")


def _as_number(value: object, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _entry(name: str, meta: dict) -> dict:
    """把一份侧车元数据转成前端要的形状（字段全都兜底）。"""
    pool = str(meta.get("pool") or "idle").strip().lower()
    if pool not in VALID_POOLS:
        pool = "idle"

    seconds = _as_number(meta.get("seconds"), 0.0)
    duration = meta.get("durationMs")
    if duration is None:
        duration = round(seconds * 1000)
    duration_ms = int(_as_number(duration, 0.0))

    return {
        "id": name,
        "label": str(meta.get("label") or name),
        "icon": str(meta.get("icon") or "✨"),
        "pool": pool,
        "weight": _as_number(meta.get("weight"), 1.0),
        "asset": f"actions/{name}.webm",
        "durationMs": duration_ms,
    }


@router.get("/actions")
async def list_actions(request: Request) -> dict:
    """返回 `{"actions": [...], "drag": {"drag-lift": {...}, "drag-swing": {...}}}`。"""
    configured = getattr(request.app.state, "actions_dir", None)
    directory = Path(configured) if configured else DEFAULT_ACTIONS_DIR

    actions: list[dict] = []
    drag: dict[str, dict] = {}

    if not directory.is_dir():
        logger.warning("动作目录不存在：%s", directory)
        return {"actions": [], "drag": {}}

    for meta_path in sorted(directory.glob("*.json")):
        name = meta_path.stem
        if not (directory / f"{name}.webm").exists():
            logger.warning("动作 %s 只有 json 没有 webm，跳过", name)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("动作 %s 的元数据读不出来（%s），跳过", name, error)
            continue
        if not isinstance(meta, dict):
            logger.warning("动作 %s 的元数据不是对象，跳过", name)
            continue

        entry = _entry(name, meta)
        role = str(meta.get("role") or "action").strip().lower()
        if role in DRAG_ROLES:
            drag[role] = entry
        else:
            actions.append(entry)

    # idle 池排前面（菜单里先看到"会自动演"的），其余按名字，保证顺序稳定
    actions.sort(key=lambda item: (item["pool"] != "idle", item["id"]))
    return {"actions": actions, "drag": drag}
