from __future__ import annotations

import ast
from pathlib import Path

from app.tasks import video_generation
from app.tasks.video_generation_parts import (
    lifecycle,
    persistence,
    polling,
    providers,
    reconciliation,
    submission,
)


_TASKS_DIR = Path(__file__).parents[1] / "app" / "tasks"
_PARTS_DIR = _TASKS_DIR / "video_generation_parts"


def test_video_generation_production_modules_stay_below_line_budget() -> None:
    paths = [_TASKS_DIR / "video_generation.py", *_PARTS_DIR.glob("*.py")]
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in paths
        if len(path.read_text(encoding="utf-8").splitlines()) > 1500
    }

    assert oversized == {}


def test_video_generation_parts_do_not_import_compatibility_facade() -> None:
    forbidden: list[str] = []
    for path in _PARTS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
                if "app.tasks.video_generation" in names:
                    forbidden.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module in {
                "app.tasks",
                "app.tasks.video_generation",
            }:
                forbidden.append(path.name)
            if (
                isinstance(node, ast.ImportFrom)
                and node.level > 0
                and any(alias.name == "video_generation" for alias in node.names)
            ):
                forbidden.append(path.name)

    assert forbidden == []


def test_facade_reexports_decomposed_task_entrypoints() -> None:
    assert video_generation.run_video_generation is submission.run_video_generation
    assert video_generation.run_video_poll is polling.run_video_poll
    assert (
        video_generation.reconcile_video_tasks is reconciliation.reconcile_video_tasks
    )
    assert video_generation._acquire_lease is lifecycle.acquire_lease
    assert (
        video_generation._provider_for_generation is providers.provider_for_generation
    )
    assert video_generation._finish_success is persistence.finish_success


def test_reference_mime_falls_back_when_upstream_override_is_invalid() -> None:
    assert (
        providers._reference_mime(  # noqa: SLF001
            {
                "mime": "video/mp4",
                "upstream_reference_mime": 123,
            }
        )
        == "video/mp4"
    )
