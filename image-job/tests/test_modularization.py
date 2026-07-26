from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
EXTRACTED_MODULES = (
    "payload_helpers.py",
    "job_persistence.py",
    "image_artifacts.py",
    "image_candidates.py",
    "image_url_security.py",
    "request_bodies.py",
    "upstream_runtime.py",
)


def load_app_module() -> Any:
    asyncio.set_event_loop(asyncio.new_event_loop())
    from .support import load_harness

    return load_harness()


def test_app_stays_below_modularization_limit() -> None:
    line_count = len((IMAGE_JOB_DIR / "app.py").read_text().splitlines())

    assert line_count < 200


def test_extracted_modules_do_not_import_app() -> None:
    for filename in EXTRACTED_MODULES:
        tree = ast.parse((IMAGE_JOB_DIR / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "app" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "app"


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

    assert asyncio.run(app.mark_running("job-late-bound")) is True
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


def test_package_has_no_module_level_runtime_primitives() -> None:
    runtime_primitives = {"Queue", "Event", "Lock"}
    for path in (IMAGE_JOB_DIR / "image_job").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            if isinstance(value.func, ast.Attribute):
                assert value.func.attr not in runtime_primitives, path
