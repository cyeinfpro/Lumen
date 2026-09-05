from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.video import presentation
from lumen_core.schema_models.video import VideoGenerationOut


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_requested", [False, True])
async def test_media_generation_response_preserves_cancellation_intent(
    monkeypatch: pytest.MonkeyPatch, cancel_requested: bool
) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    requested_at = now if cancel_requested else None
    row = SimpleNamespace(
        id="media-video-task",
        action="t2v",
        model="test-model",
        prompt="A scene",
        input_image_id=None,
        upstream_request={},
        upstream_response={},
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        fps=24,
        generate_audio=True,
        seed=None,
        status="running",
        progress_stage="rendering",
        progress_pct=42,
        submission_epoch=3,
        cancel_requested_at=requested_at,
        provider_name="test",
        provider_kind="dashscope",
        est_token_upper=1,
        est_cost_micro=100,
        billed_tokens=None,
        billed_cost_micro=None,
        error_code=None,
        error_message=None,
        diagnostics={},
        created_at=now,
        updated_at=now,
        started_at=now,
        submit_started_at=now,
        submitted_at=now,
        finished_at=None,
    )

    async def no_video(_db, generation_id):
        assert generation_id == row.id
        return None

    monkeypatch.setattr(presentation, "video_for_generation", no_video)
    output = await presentation.generation_out(None, row)
    assert output.cancel_requested_at == requested_at
    assert output.status == "running"
    assert output.submission_epoch == 3
    payload = output.model_dump(mode="json")
    assert payload["cancel_requested_at"] == (
        "2026-09-05T12:00:00Z" if cancel_requested else None
    )
    assert VideoGenerationOut.model_validate(payload).cancel_requested_at == requested_at
    payload.pop("cancel_requested_at")
    assert VideoGenerationOut.model_validate(payload).cancel_requested_at is None
