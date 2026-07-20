from __future__ import annotations

import ast
import asyncio
import importlib.util
import shutil
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image


IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
EXTRACTED_MODULES = (
    "runtime_config.py",
    "payload_helpers.py",
    "job_persistence.py",
    "image_artifacts.py",
    "image_candidates.py",
    "image_url_security.py",
    "request_bodies.py",
    "upstream_runtime.py",
)
RUNTIME_MODULES = ("app.py", *EXTRACTED_MODULES)


def load_app_module() -> Any:
    asyncio.set_event_loop(asyncio.new_event_loop())
    path = IMAGE_JOB_DIR / "app.py"
    module_dir = str(path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location(
        "image_job_modularization_under_test",
        path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_app_from(path: Path, module_name: str) -> Any:
    asyncio.set_event_loop(asyncio.new_event_loop())
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_app_stays_below_modularization_limit() -> None:
    line_count = len((IMAGE_JOB_DIR / "app.py").read_text().splitlines())

    assert line_count < 1500


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


def test_copied_app_loads_only_sibling_modules_and_keeps_pixel_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    copy_a = tmp_path / "copy-a"
    copy_b = tmp_path / "copy-b"
    copy_a.mkdir()
    copy_b.mkdir()
    for filename in RUNTIME_MODULES:
        shutil.copy2(IMAGE_JOB_DIR / filename, copy_a / filename)
        shutil.copy2(IMAGE_JOB_DIR / filename, copy_b / filename)

    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    try:
        monkeypatch.setenv("IMAGE_JOB_MAX_IMAGE_PIXELS", "200")
        app_a = load_app_from(copy_a / "app.py", "image_job_copy_a")
        monkeypatch.setenv("IMAGE_JOB_MAX_IMAGE_PIXELS", "50")
        app_b = load_app_from(copy_b / "app.py", "image_job_copy_b")

        assert Path(app_a._runtime_config.__file__).parent == copy_a
        assert Path(app_a._image_artifacts_module.__file__).parent == copy_a
        assert Path(app_a._image_candidates_module.__file__).parent == copy_a
        assert Path(app_a._upstream_runtime_module.__file__).parent == copy_a
        assert Path(app_b._runtime_config.__file__).parent == copy_b
        assert Path(app_b._image_artifacts_module.__file__).parent == copy_b
        assert Path(app_b._image_candidates_module.__file__).parent == copy_b
        assert Path(app_b._upstream_runtime_module.__file__).parent == copy_b

        buffer = BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        raw = buffer.getvalue()

        assert app_a.image_metadata(raw, "image/png")[:2] == (10, 10)
        with pytest.raises(app_b.JobFailure):
            app_b.image_metadata(raw, "image/png")
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels
