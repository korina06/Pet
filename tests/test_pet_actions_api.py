"""动作目录接口：缺一半文件、json 坏了、字段类型不对都跳过或用默认值，不影响整张列表。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.pet_routes import router

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = PROJECT_ROOT / ".testtmp"


def make_client(directory: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.actions_dir = directory
    return TestClient(app)


def make_action_dir() -> Path:
    path = TMP_ROOT / f"actions-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_action(
    directory: Path, name: str, meta: dict | str | None = None, *, with_webm: bool = True
) -> None:
    if with_webm:
        (directory / f"{name}.webm").write_bytes(b"fake-webm")
    if meta is None:
        meta = {"seconds": 8.0}
    text = meta if isinstance(meta, str) else json.dumps(meta, ensure_ascii=False)
    (directory / f"{name}.json").write_text(text, encoding="utf-8")


def test_lists_actions_and_separates_drag_clips():
    directory = make_action_dir()
    try:
        write_action(
            directory,
            "wipe",
            {"label": "擦擦灰", "icon": "🧹", "pool": "idle", "weight": 2, "seconds": 8.0},
        )
        write_action(
            directory,
            "tea",
            {"label": "喝茶", "icon": "🍵", "pool": "manual", "seconds": 10.13},
        )
        write_action(directory, "drag-lift", {"role": "drag-lift", "seconds": 3.2})
        write_action(directory, "drag-swing", {"role": "drag-swing", "seconds": 1.2})

        body = make_client(directory).get("/api/pet/actions").json()

        ids = [item["id"] for item in body["actions"]]
        assert ids == ["wipe", "tea"], f"顺序应当 idle 优先，实际 {ids}"

        wipe = body["actions"][0]
        assert wipe["label"] == "擦擦灰"
        assert wipe["icon"] == "🧹"
        assert wipe["pool"] == "idle"
        assert wipe["weight"] == 2
        assert wipe["asset"] == "actions/wipe.webm"
        assert wipe["durationMs"] == 8000

        # 拖拽两段不进菜单，单独放在 drag 里
        assert set(body["drag"]) == {"drag-lift", "drag-swing"}
        assert body["drag"]["drag-lift"]["asset"] == "actions/drag-lift.webm"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_broken_entries_are_skipped_not_fatal():
    directory = make_action_dir()
    try:
        write_action(directory, "ok", {"label": "好的", "pool": "idle"})
        write_action(directory, "no-webm", {"pool": "idle"}, with_webm=False)
        write_action(directory, "broken-json", "{ 这不是 json")
        write_action(directory, "not-object", "[1, 2, 3]")

        response = make_client(directory).get("/api/pet/actions")
        assert response.status_code == 200, "一个坏文件不许让整个接口 500"

        ids = [item["id"] for item in response.json()["actions"]]
        assert ids == ["ok"], f"坏条目应当被跳过，实际 {ids}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_missing_or_odd_fields_fall_back_to_defaults():
    directory = make_action_dir()
    try:
        write_action(
            directory,
            "weird",
            {"pool": "idle ", "weight": "很重", "seconds": "3", "icon": ""},
        )
        entry = make_client(directory).get("/api/pet/actions").json()["actions"][0]

        assert entry["label"] == "weird", "没有 label 时用动作名"
        assert entry["icon"] == "✨", "空 icon 用默认值"
        assert entry["pool"] == "idle", "带空格的 pool 也应当归一化"
        assert entry["weight"] == 1.0, "weight 不是数字就退回 1"
        assert entry["durationMs"] == 3000, "没写 durationMs 就按 seconds 折算"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_unknown_pool_is_normalised():
    directory = make_action_dir()
    try:
        write_action(directory, "odd", {"pool": "whenever"})
        entry = make_client(directory).get("/api/pet/actions").json()["actions"][0]
        assert entry["pool"] == "idle", "不认识的池当归 idle，动作不能凭空消失"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_missing_directory_returns_empty_lists():
    """目录不存在时返回空表，而不是 500（比如打包漏了素材目录）。"""
    body = make_client(TMP_ROOT / "definitely-not-here").get("/api/pet/actions").json()
    assert body == {"actions": [], "drag": {}}
