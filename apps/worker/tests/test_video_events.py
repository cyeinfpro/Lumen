from __future__ import annotations

from datetime import datetime, timezone

from lumen_core.models import VideoGeneration

from app.video_events import video_event_data


def _generation() -> VideoGeneration:
    return VideoGeneration(
        id="video-gen-1",
        user_id="user-1",
        action="t2v",
        model="seedance-2.0",
        provider_name="volcano-main",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        prompt="make a clip",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        deadline_at=datetime.now(timezone.utc),
        idempotency_key="idem-1",
        request_fingerprint="f" * 64,
        est_token_upper=60_000,
        est_cost_micro=1_000,
    )


def test_video_event_extra_cannot_override_canonical_fields() -> None:
    generation = _generation()
    generation.status = "running"
    generation.progress_stage = "polling"
    generation.progress_pct = 42
    generation.submission_epoch = 3
    generation.error_code = "real-error"
    generation.error_message = "real-message"

    data = video_event_data(
        generation,
        video_id="video-1",
        video_generation_id="forged-generation",
        kind="forged-kind",
        status="forged-status",
        stage="forged-stage",
        progress_pct=999,
        submission_epoch=999,
        error_code="forged-error",
        error_message="forged-message",
        provider="volcano-main",
    )

    assert data["video_generation_id"] == "video-gen-1"
    assert data["kind"] == "video_generation"
    assert data["status"] == "running"
    assert data["stage"] == "polling"
    assert data["progress_pct"] == 42
    assert data["submission_epoch"] == 3
    assert data["error_code"] == "real-error"
    assert data["error_message"] == "real-message"
    assert data["video_id"] == "video-1"
    assert data["provider"] == "volcano-main"
