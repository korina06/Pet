"""容器化产物：用纯标准库读文本，钉住基础镜像、依赖安装与「不许把 `.env` 拷进镜像」这些承诺。

Dockerfile 平时没人跑，坏了也看不出来，所以要有人守着。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str) -> list[str]:
    return [
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerfile_exists_and_pins_the_python_version() -> None:
    """基础镜像必须是 `python:3.13-*`，不许 `latest`（第 195 步的版本策略）。"""
    lines = _lines("Dockerfile")
    from_lines = [line for line in lines if line.upper().startswith("FROM ")]
    assert len(from_lines) == 1, f"应当只有一个 FROM，实际 {from_lines}"

    image = from_lines[0].split()[1]
    assert image.startswith("python:3.13"), f"基础镜像没钉 3.13：{image}"
    assert "latest" not in image, f"不许用 latest：{image}"


def test_dockerfile_installs_the_pinned_requirements() -> None:
    """依赖必须来自 `requirements.txt`（那份文件是全钉死的），不许在镜像里另写一套。"""
    joined = "\n".join(_lines("Dockerfile"))
    assert "requirements.txt" in joined
    assert "pip install" in joined, "没有装依赖"

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    # 依赖全钉版本这件事由 test_ci_workflow 守；这里只确认镜像用的是同一份
    assert "fastapi==" in requirements


def test_dockerfile_starts_uvicorn_on_main_app() -> None:
    """启动命令要是 `uvicorn main:app`（`python -m uvicorn`，不依赖 shell PATH）。"""
    joined = " ".join(_lines("Dockerfile"))
    assert "uvicorn" in joined and "main:app" in joined, joined[-200:]
    assert '"--host", "127.0.0.1"' in joined, "容器里也绑 127.0.0.1（与第 157 步的取舍一致）"


def test_dockerfile_declares_a_data_volume_and_healthcheck() -> None:
    """`/app/data` 要挂出来（库/日志/备份都在里面），健康检查要打 /api/health。"""
    joined = "\n".join(_lines("Dockerfile"))
    assert "/app/data" in joined, "数据目录没挂出来，容器一重建数据就没了"
    assert "HEALTHCHECK" in joined
    assert "/api/health" in joined


def test_dockerignore_keeps_secrets_and_local_state_out() -> None:
    """`.env`、`data/`、`.testtmp/` 绝不能进镜像层。"""
    entries = set(_lines(".dockerignore"))
    for pattern in (".env", "data/", ".testtmp/", "__pycache__/"):
        assert pattern in entries, f".dockerignore 少了 {pattern}"


def test_dockerignore_keeps_what_the_app_needs() -> None:
    """反过来：`static/`、`requirements.txt`、`tests/` 不能被排除掉。

    这类错最隐蔽 —— 镜像能 build 成功，但跑起来是白屏（静态目录没了）。
    """
    entries = set(_lines(".dockerignore"))
    for needed in ("requirements.txt", "tests/", "scripts/"):
        assert needed not in entries, f"{needed} 被排除了，镜像会缺文件"

    # static/ 要么不出现，要么显式用 ! 加回来
    assert "static/" not in entries, "static/ 被排除 → 页面白屏"
