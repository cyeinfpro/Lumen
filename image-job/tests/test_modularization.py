from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
import site
import subprocess
import sys
from typing import Any

IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = IMAGE_JOB_DIR / "image_job"
MOVED_MODULES = {
    "image_artifacts.py": "artifacts.py",
    "image_candidates.py": "candidates.py",
    "image_url_security.py": "url_security.py",
    "job_persistence.py": "persistence.py",
    "payload_helpers.py": "payloads.py",
    "request_bodies.py": "http_bodies.py",
    "upstream_runtime.py": "upstream.py",
}


def load_app_module() -> Any:
    asyncio.set_event_loop(asyncio.new_event_loop())
    from .support import load_harness

    return load_harness()


def test_app_stays_below_modularization_limit() -> None:
    line_count = len((IMAGE_JOB_DIR / "app.py").read_text().splitlines())

    assert line_count < 200


def test_moved_modules_live_only_in_package() -> None:
    for old_name, package_name in MOVED_MODULES.items():
        assert not (IMAGE_JOB_DIR / old_name).exists()
        assert (PACKAGE_DIR / package_name).is_file()


def test_moved_modules_do_not_import_app() -> None:
    for package_name in MOVED_MODULES.values():
        tree = ast.parse((PACKAGE_DIR / package_name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "app" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "app"


def test_package_does_not_import_legacy_top_level_modules() -> None:
    legacy_modules = {Path(name).stem for name in MOVED_MODULES}
    for path in PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0] not in legacy_modules
                    for alias in node.names
                ), path
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".", 1)[0] not in legacy_modules, path


def test_payload_policy_reads_monkeypatched_app_setting(monkeypatch) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "RESPONSES_STRIP_PARTIAL_IMAGES", False)

    payload = app.validate_payload(
        {
            "endpoint": "/v1/responses",
            "body": {
                "tools": [
                    {
                        "type": "image_generation",
                        "partial_images": 2,
                    }
                ]
            },
        }
    )

    assert payload["body"]["tools"][0]["partial_images"] == 2


def test_persistence_facade_reads_monkeypatched_db_exec(monkeypatch) -> None:
    app = load_app_module()
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fake_db_exec(
        sql: str,
        params: tuple[object, ...] = (),
    ) -> int:
        calls.append((sql, params))
        return 1

    monkeypatch.setattr(app, "db_exec", fake_db_exec)

    assert asyncio.run(app.mark_running("job-late-bound"))
    assert calls
    assert "status = 'running'" in calls[0][0]
    assert calls[0][1][-1] == "job-late-bound"


def test_artifact_facade_reads_monkeypatched_data_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = load_app_module()
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)

    image_dir, relative = app.job_image_dir(
        "job-late-bound",
        "2026-07-11T00:00:00+00:00",
    )

    assert image_dir == tmp_path / relative
    assert relative.endswith("/job-late-bound")


def test_package_imports_without_random_dynamic_loader() -> None:
    app_source = (IMAGE_JOB_DIR / "app.py").read_text()

    forbidden = (
        "spec_from_" + "file_location",
        "import" + "lib",
        "sys." + "modules",
    )
    assert all(value not in app_source for value in forbidden)


def test_wheel_installs_and_imports_from_empty_directory(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-build-isolation",
            "--out-dir",
            str(dist_dir),
            str(IMAGE_JOB_DIR),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(dist_dir.glob("lumen_image_job-*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    venv_python = venv_dir / bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    parent_site = next(path for path in site.getsitepackages() if Path(path).is_dir())
    venv_site = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (Path(venv_site) / "lumen-test-dependencies.pth").write_text(
        f"{parent_site}\n",
        encoding="utf-8",
    )

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    smoke = subprocess.run(
        [
            str(venv_python),
            "-I",
            "-c",
            (
                "from image_job.app_factory import create_app; "
                "app = create_app(); "
                "assert app.title == 'sub2api image job sidecar'"
            ),
        ],
        cwd=empty_dir,
        env={
            **os.environ,
            "IMAGE_JOB_CREDENTIAL_ACTIVE_KEY_ID": "test-v1",
            "IMAGE_JOB_CREDENTIAL_MASTER_SECRET": "test-master-secret-" + "x" * 32,
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr


def test_package_has_no_module_level_runtime_primitives() -> None:
    runtime_primitives = {"Queue", "Event", "Lock"}
    for path in PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            if isinstance(value.func, ast.Attribute):
                assert value.func.attr not in runtime_primitives, path


def test_processing_has_no_dynamic_facade_or_runtime_heartbeat_patch() -> None:
    processing_tree = ast.parse((PACKAGE_DIR / "processing.py").read_text())
    method_names = {
        node.name
        for node in ast.walk(processing_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    runtime_source = (PACKAGE_DIR / "runtime.py").read_text()

    assert "__getattr__" not in method_names
    assert ".processing.touch_running =" not in runtime_source
