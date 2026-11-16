from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.video.draft_workflow import (
    draft_expiry, validate_official_source, load_workflow_source,
    official_completion_body, workflow_metadata,
)
from app.services.video.submission import request_fingerprint
from lumen_core.schemas import VideoCreateIn

NOW = datetime(2026, 11, 16, tzinfo=UTC)


def body(**kw):
    data = dict(action="t2v", model="seedance-2.5", prompt="original",
                duration_s=5, resolution="480p", aspect_ratio="16:9",
                idempotency_key="key")
    data.update(kw)
    return VideoCreateIn(**data)


def source(**kw):
    data = dict(id="source", user_id="owner", status="succeeded",
                model="seedance-2.5", action="t2v", prompt="original prompt",
                provider_name="native", provider_kind="volcano", provider_task_id="cgt-id",
                created_at=NOW, submitted_at=NOW, upstream_response={},
                input_image_id=None, duration_s=5, aspect_ratio="16:9",
                seed=123, generate_audio=True,
                upstream_request={"draft": True, "upstream_model": "doubao-seedance-2-5-260628"})
    data.update(kw)
    return SimpleNamespace(**data)


@pytest.mark.parametrize("kw", [
    {"draft": True, "resolution": "720p"},
    {"draft": True, "reference_draft": True},
    {"draft_generation_id": "x"},
    {"draft_generation_id": "x", "resolution": "1080p", "reference_generation_id": "x"},
    {"reference_generation_id": "x"},
])
def test_schema_rejects_incompatible_modes(kw):
    with pytest.raises(ValidationError):
        body(**kw)


def test_draft_and_reference_draft_valid():
    assert body(draft=True).draft
    assert body(reference_draft=True, model="seedance-2.5-flash").reference_draft
    assert body(draft_generation_id="source", resolution="1080p").draft_generation_id


def test_official_ttl_cutover_and_upstream_created_at():
    old = datetime(2026, 10, 28, 1, 59, tzinfo=UTC)
    assert draft_expiry(source(created_at=old, submitted_at=old)) == old + timedelta(days=7)
    assert draft_expiry(source()) == NOW + timedelta(days=14)
    assert draft_expiry(source(upstream_response={"created_at": old.timestamp()})) == old + timedelta(days=7)
    assert draft_expiry(source(upstream_request={})) is None


@pytest.mark.parametrize("kw", [
    {"status": "running"}, {"provider_kind": "volcano_thirdparty"},
    {"provider_task_id": None}, {"upstream_request": {"draft": False}},
    {"model": "seedance-2.5-flash"},
])
def test_reject_unusable_sources(kw):
    with pytest.raises(HTTPException):
        validate_official_source(source(**kw), now=NOW)


def test_expired_source_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_official_source(source(), now=NOW + timedelta(days=14))
    assert exc.value.status_code == 410


def test_completion_inherits_original_fields_not_client_changes():
    original = source()
    result = official_completion_body(
        body(draft_generation_id="source", resolution="1080p", prompt="changed", duration_s=20),
        original,
    )
    assert result.prompt == "original prompt"
    assert result.duration_s == 5 and result.seed == 123 and result.resolution == "1080p"
    assert not result.reference_media
    metadata = workflow_metadata(result, original)
    assert metadata["draft_task_id"] == "cgt-id"
    assert metadata["source_provider_name"] == "native"
    assert "api_key" not in metadata


def db_results(*rows):
    db = SimpleNamespace(execute=AsyncMock())
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda r=r: r) for r in rows]
    return db


@pytest.mark.asyncio
async def test_source_lookup_is_owner_scoped():
    db = db_results(None)
    with pytest.raises(HTTPException) as exc:
        await load_workflow_source(db, body(draft_generation_id="source", resolution="1080p"), "owner")
    assert exc.value.status_code == 404
    sql = str(db.execute.call_args.args[0])
    assert "user_id" in sql and "FOR UPDATE" in sql


@pytest.mark.asyncio
async def test_reference_completion_requires_actual_source_video_attachment():
    request = body(action="reference", reference_generation_id="source",
                   reference_media=[{"kind": "video", "video_id": "wrong"}])
    with pytest.raises(HTTPException):
        await load_workflow_source(db_results(source(), SimpleNamespace(id="video")), request, "owner")
    request.reference_media[0].video_id = "video"
    assert await load_workflow_source(db_results(source(), SimpleNamespace(id="video")), request, "owner")


def test_mode_is_part_of_idempotency_fingerprint():
    assert request_fingerprint(body()) != request_fingerprint(body(draft=True))
    assert request_fingerprint(body()) != request_fingerprint(body(reference_draft=True))

@pytest.mark.asyncio
async def test_official_completion_orchestration_keeps_billing_media_but_skips_reload(monkeypatch):
    from app.services.video import submission
    original = source(
        input_image_storage_key=None, input_image_sha256=None,
        upstream_request={"draft": True, "upstream_model": "doubao-seedance-2-5-260628",
                          "reference_media": [{"kind": "video", "video_id": "old", "duration_s": 7}]},
    )
    provider = SimpleNamespace(name="native", kind="volcano")
    cost = SimpleNamespace(estimated_tokens=10, hold_micro=20, unit_price_micro=2)
    admission = SimpleNamespace(provider=provider, upstream_model="doubao-seedance-2-5-260628",
                                billing_model="seedance-2.5", pricing_variant="reference_video",
                                cost=cost, allow_negative=False)
    monkeypatch.setattr(submission, "_find_or_serialize_video_submission", AsyncMock(return_value=None))
    monkeypatch.setattr(submission, "_lock_video_submission_user", AsyncMock())
    monkeypatch.setattr(submission, "load_workflow_source", AsyncMock(return_value=original))
    prepare_admission = AsyncMock(return_value=admission)
    monkeypatch.setattr(submission, "_prepare_video_billing_admission", prepare_admission)
    monkeypatch.setattr(submission, "_ensure_video_wallet_admission", AsyncMock())
    media_loader = AsyncMock(side_effect=AssertionError("official completion must not reload old media"))
    monkeypatch.setattr(submission, "_prepare_video_submission", media_loader)
    hold = AsyncMock()
    monkeypatch.setattr(submission.billing_core, "hold", hold)
    db = SimpleNamespace(add=lambda row: None, flush=AsyncMock())
    async def render(db, row):
        return row
    result = await submission.create_video_generation_record(
        db, body(draft_generation_id="source", resolution="1080p", prompt="tampered"),
        SimpleNamespace(id="owner", account_mode="wallet"),
        context=submission.VideoSubmissionContext(defer_commit=True),
        services=submission.VideoSubmissionServices(generation_renderer=render),
    )
    assert result.prompt == "original prompt"
    assert result.upstream_request["draft_task_id"] == "cgt-id"
    assert result.upstream_request["reference_media"][0]["video_id"] == "old"
    assert prepare_admission.call_args.kwargs["reference_media_snapshot"][0]["duration_s"] == 7
    media_loader.assert_not_called()
    hold.assert_awaited_once()
