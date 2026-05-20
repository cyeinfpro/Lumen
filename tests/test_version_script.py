from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "scripts" / "version.py"


def _write_minimal_version_tree(tmp_path: Path, version: str = "1.2.3") -> Path:
    root = tmp_path / "repo"
    for path in (
        "scripts",
        "apps/api",
        "apps/worker",
        "apps/tgbot",
        "apps/web",
        "packages/core/lumen_core",
    ):
        (root / path).mkdir(parents=True, exist_ok=True)

    shutil.copy2(VERSION_SCRIPT, root / "scripts" / "version.py")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    for rel in (
        "pyproject.toml",
        "apps/api/pyproject.toml",
        "apps/worker/pyproject.toml",
        "apps/tgbot/pyproject.toml",
        "packages/core/pyproject.toml",
    ):
        (root / rel).write_text(
            f'[project]\nname = "sample"\nversion = "{version}"\n',
            encoding="utf-8",
        )
    (root / "packages/core/lumen_core/__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "apps/web/package.json").write_text(
        json.dumps({"version": version}) + "\n",
        encoding="utf-8",
    )
    (root / "apps/web/package-lock.json").write_text(
        json.dumps({"version": version, "packages": {"": {"version": version}}})
        + "\n",
        encoding="utf-8",
    )
    return root


def _run_check(root: Path, *, allow_rolling: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if allow_rolling:
        env["LUMEN_ALLOW_ROLLING_TAG"] = "1"
    else:
        env.pop("LUMEN_ALLOW_ROLLING_TAG", None)
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "version.py"), "check"],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_version_check_prefers_release_json_next_to_current_script(
    tmp_path: Path,
) -> None:
    root = _write_minimal_version_tree(tmp_path)
    (root / "current").mkdir()
    (root / ".lumen_release.json").write_text(
        json.dumps({"image_tag": "v1.2.3"}) + "\n",
        encoding="utf-8",
    )
    (root / "current/.lumen_release.json").write_text(
        json.dumps({"image_tag": "main"}) + "\n",
        encoding="utf-8",
    )

    result = _run_check(root)

    assert result.returncode == 0, result.stderr + result.stdout


def test_version_check_requires_explicit_gate_for_main_image_tag(
    tmp_path: Path,
) -> None:
    root = _write_minimal_version_tree(tmp_path)
    (root / ".lumen_release.json").write_text(
        json.dumps({"image_tag": "main"}) + "\n",
        encoding="utf-8",
    )

    denied = _run_check(root)
    allowed = _run_check(root, allow_rolling=True)

    assert denied.returncode == 1
    assert "image_tag" in denied.stderr
    assert "LUMEN_ALLOW_ROLLING_TAG=1" in denied.stderr
    assert allowed.returncode == 0, allowed.stderr + allowed.stdout
