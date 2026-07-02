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
    assert all(
        shot.duration_s == storyboards.STORYBOARD_DEFAULT_DURATION_S for shot in shots
    )

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


def test_clear_shot_video_output_removes_stale_video_fields() -> None:
    cleaned = storyboards._clear_shot_video_output(  # noqa: SLF001
        {
            "video_generation_id": "video-gen-1",
            "video_id": "video-1",
            "video_status": "running",
            "video_progress_stage": "fetching",
            "video_progress_pct": 80,
            "video_submission": {"idempotency_key": "sb:old"},
            "keyframe_image_id": "image-1",
            "notes": "keep",
        }
    )

    assert cleaned == {"keyframe_image_id": "image-1", "notes": "keep"}


def test_storyboard_video_idempotency_uses_submission_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storyboards, "new_uuid7", lambda: "nonce-1")
    step = SimpleNamespace(
        id="shot-1",
        input_json={"keyframe_source_hash": "mutable-source-hash"},
        output_json={
            "keyframe_generation_id": "keyframe-gen-1",
            "keyframe_image_id": "image-1",
        },
    )

    key, fingerprint = storyboards._resolve_storyboard_video_idempotency_key(  # noqa: SLF001
        run_id="run-1",
        step=step,  # type: ignore[arg-type]
        keyframe_image_id="image-1",
        requested_key=None,
    )

    assert key.startswith("sb:run-1:shot-1:v:")
    assert "mutable-source-hash" not in key
    assert len(key) <= 96

    step.output_json["video_submission"] = {
        "fingerprint": fingerprint,
        "idempotency_key": key,
    }
    replay_key, replay_fingerprint = (
        storyboards._resolve_storyboard_video_idempotency_key(  # noqa: SLF001
            run_id="run-1",
            step=step,  # type: ignore[arg-type]
            keyframe_image_id="image-1",
            requested_key=None,
        )
    )

    assert replay_key == key
    assert replay_fingerprint == fingerprint


def test_generate_all_keyframes_validates_batch_before_creating_tasks() -> None:
    source = inspect.getsource(storyboards.generate_all_keyframes)

    assert "shots_not_approved" in source
    assert source.index("shots_not_approved") < source.index(
        "_create_storyboard_image_task"
    )


def test_storyboard_status_sync_recovers_orphan_video_generation() -> None:
    source = inspect.getsource(storyboards._recover_storyboard_video_generations)  # noqa: SLF001

    assert "workflow_run_id" in source
    assert "workflow_step_key" in source
    assert "as_string()" in source
    assert "storyboard_video_submission_fingerprint" in source


@pytest.mark.asyncio
async def test_storyboard_video_recovery_requires_current_submission_fingerprint() -> None:
    step = SimpleNamespace(
        step_key="shot:1",
        input_json={"keyframe_source_hash": "source-new"},
        output_json={
            "keyframe_generation_id": "keyframe-new",
            "keyframe_image_id": "image-new",
        },
    )
    expected_fingerprint = storyboards._storyboard_video_submission_fingerprint(  # noqa: SLF001
        step=step,  # type: ignore[arg-type]
        keyframe_image_id="image-new",
    )
    rows = [
        SimpleNamespace(
            id="video-old",
            upstream_request={
                "workflow_step_key": "shot:1",
                "storyboard_video_submission_fingerprint": "old-fingerprint",
            },
        ),
        SimpleNamespace(
            id="video-new",
            upstream_request={
                "workflow_step_key": "shot:1",
                "storyboard_video_submission_fingerprint": expected_fingerprint,
            },
        ),
    ]

    class Result:
        def scalars(self) -> "Result":
            return self

        def all(self) -> list[SimpleNamespace]:
            return rows

    class Db:
        async def execute(self, _stmt):
            return Result()

    recovered = await storyboards._recover_storyboard_video_generations(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1", user_id="user-1"),  # type: ignore[arg-type]
        steps=[step],  # type: ignore[list-item]
    )

    assert recovered == {"shot:1": rows[1]}


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
