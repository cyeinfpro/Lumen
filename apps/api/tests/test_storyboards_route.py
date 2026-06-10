from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.routes import storyboards


def test_decode_cursor_requires_timezone_aware_timestamp() -> None:
    assert storyboards._decode_cursor(None) is None  # noqa: SLF001

    timestamp = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)
    assert storyboards._decode_cursor(f"{timestamp.isoformat()}|run-1") == (  # noqa: SLF001
        timestamp,
        "run-1",
    )

    with pytest.raises(HTTPException) as excinfo:
        storyboards._decode_cursor("2026-06-11T09:30:00|run-1")  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_shots_from_script_splits_script_and_provides_fallback() -> None:
    shots = storyboards._shots_from_script("开场建立场景。展示产品动作！结尾收束")  # noqa: SLF001

    assert [shot.title for shot in shots] == ["镜头 01", "镜头 02", "镜头 03"]
    assert shots[0].shot_type == "establishing shot"
    assert all(shot.duration_s == storyboards.STORYBOARD_DEFAULT_DURATION_S for shot in shots)

    fallback = storyboards._shots_from_script("")  # noqa: SLF001
    assert len(fallback) == 3
    assert fallback[0].visual


def test_shot_source_hash_changes_when_asset_reference_changes() -> None:
    shot = SimpleNamespace(
        input_json={
            "title": "镜头 01",
            "visual": "主角拿起产品",
            "reference_notes": "保持白色包装",
            "keyframe_prompt": "电影感首帧",
            "asset_ids": ["asset-1"],
        }
    )
    asset = SimpleNamespace(
        id="asset-1",
        input_json={"revision": 1},
        output_json={"image_id": "image-1"},
        approved_at=None,
    )

    original = storyboards._shot_source_hash(shot, {"asset-1": asset})  # noqa: SLF001
    revision_changed = storyboards._shot_source_hash(  # noqa: SLF001
        shot,
        {
            "asset-1": SimpleNamespace(
                id="asset-1",
                input_json={"revision": 2},
                output_json={"image_id": "image-1"},
                approved_at=None,
            )
        },
    )
    image_changed = storyboards._shot_source_hash(  # noqa: SLF001
        shot,
        {
            "asset-1": SimpleNamespace(
                id="asset-1",
                input_json={"revision": 1},
                output_json={"image_id": "image-2"},
                approved_at=None,
            )
        },
    )

    assert revision_changed != original
    assert image_changed != original


def test_storyboard_image_task_helper_does_not_commit_before_step_link() -> None:
    source = inspect.getsource(storyboards._create_storyboard_image_task)  # noqa: SLF001

    assert "await db.commit()" not in source
    assert "StoryboardImageTask(" in source


def test_generate_all_keyframes_does_not_call_single_route_handler() -> None:
    source = inspect.getsource(storyboards.generate_all_keyframes)
    publish_source = inspect.getsource(storyboards._publish_storyboard_image_tasks)  # noqa: SLF001

    assert "generate_shot_keyframe(" not in source
    assert "_publish_storyboard_image_tasks" in source
    assert "Semaphore(STORYBOARD_KEYFRAME_PARALLELISM)" in publish_source


def test_storyboards_router_is_mounted_for_desktop_runtime() -> None:
    source = inspect.getsource(main._include_desktop_routers)  # noqa: SLF001

    assert "storyboards" in source
    assert "include_router(storyboards.router)" in source
