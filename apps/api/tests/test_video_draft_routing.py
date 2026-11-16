"""Regression tests for official draft routing, aliases, expiry and retries."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.services.video import options
from app.services.video.draft_workflow import draft_expiry
from lumen_core.schemas import VideoCreateIn

MODEL = "doubao-seedance-2-5-260628"


def body(**changes):
    data = dict(action="t2v", model="custom-alias", prompt="original", duration_s=5,
                resolution="480p", aspect_ratio="16:9", idempotency_key="test", draft=True)
    data.update(changes)
    return VideoCreateIn(**data)


def provider(name, kind="volcano", upstream=MODEL):
    return SimpleNamespace(name=name, kind=kind, supports=lambda *_: True,
                           upstream_model_for=lambda *_: upstream)


async def ready(monkeypatch, request, providers, **kw):
    monkeypatch.setattr(options, "select_video_provider", lambda pool, **_: next(iter(pool), None))
    monkeypatch.setattr(options, "estimate_pairs", lambda _: ([5], ["480p", "1080p"]))
    monkeypatch.setattr(options, "duration_options_for_provider_action", lambda *a, **kw: [5])
    return await options.require_video_create_ready(
        None, request, video_enabled_loader=AsyncMock(return_value=True),
        billing_enabled_loader=AsyncMock(return_value=True),
        estimates_loader=AsyncMock(return_value={}),
        provider_loader=AsyncMock(return_value=(providers, [])), **kw)


@pytest.mark.asyncio
async def test_alias_draft_selects_native_despite_third_party_priority(monkeypatch):
    native = provider("native")
    selected, _ = await ready(monkeypatch, body(), [provider("third", "volcano_third_party"), native])
    assert selected is native


@pytest.mark.asyncio
async def test_completion_pins_original_provider(monkeypatch):
    original = provider("original")
    selected, _ = await ready(
        monkeypatch, body(draft=False, draft_generation_id="source", resolution="1080p"),
        [provider("new-preferred"), original], source_provider_name="original")
    assert selected is original


@pytest.mark.asyncio
@pytest.mark.parametrize("providers", [
    [provider("third", "volcano_third_party")],
    [provider("flash", upstream="doubao-seedance-2-5-flash-261115")],
])
async def test_mapped_unsupported_provider_rejected(monkeypatch, providers):
    with pytest.raises(HTTPException) as exc:
        await ready(monkeypatch, body(), providers)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_missing_original_never_falls_back(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await ready(monkeypatch, body(draft=False, draft_generation_id="source", resolution="1080p"),
                    [provider("other")], source_provider_name="missing")
    assert exc.value.status_code == 409


def test_missing_upstream_clock_uses_earliest_local_time():
    early = datetime(2026, 10, 28, 1, tzinfo=UTC)
    source = SimpleNamespace(upstream_request={"draft": True}, upstream_response={},
                             created_at=early.replace(tzinfo=None),
                             submitted_at=early + timedelta(hours=3),
                             submit_started_at=early + timedelta(hours=1))
    assert draft_expiry(source) == early + timedelta(days=7)


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow,resolution", [
    ({"draft": True}, "480p"),
    ({"draft_generation_id": "source"}, "1080p"),
    ({"reference_draft": True}, "480p"),
])
async def test_manual_retry_preserves_workflow(workflow, resolution):
    from app.routes.video_generation_routes import retry_video_generation

    row = SimpleNamespace(id="failed", user_id="owner", status="failed",
        upstream_request=workflow, action="t2v", model="seedance-2.5", prompt="original",
        input_image_id=None, duration_s=5, resolution=resolution, aspect_ratio="16:9",
        generate_audio=True, seed=123, watermark=False, updated_at=datetime.now(UTC),
        input_image_storage_key=None, input_image_sha256=None)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)))
    deps = SimpleNamespace(reject_canvas_retry=Mock(), create_record=AsyncMock(return_value="result"),
                           http_error=lambda *a, **kw: AssertionError(a), logger=Mock())
    assert await retry_video_generation(generation_id="failed", request=None,
        user=SimpleNamespace(id="owner"), db=db, deps=deps) == "result"
    submitted = deps.create_record.call_args.args[1]
    for key, value in workflow.items():
        assert getattr(submitted, key) == value
