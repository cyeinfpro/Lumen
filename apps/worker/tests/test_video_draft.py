from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import asyncio

import pytest

from app.video_upstream_parts.adapters import (
    VolcanoSeedanceAdapter, VolcanoThirdPartySeedanceAdapter,
    VolcanoNewApiVideoAdapter, UnifiedVideoCreateAdapter, FakeVideoAdapter,
)
from app.video_upstream_parts.contracts import VideoSubmitRequest, VideoUpstreamError
from app.tasks.video_generation_parts import submission


def request(**kwargs):
    return replace(VideoSubmitRequest(
        task_id="final", user_id="user", action="reference",
        model="seedance-2.5", upstream_model="doubao-seedance-2-5-260628",
        prompt="original prompt", duration_s=8, resolution="480p",
        aspect_ratio="16:9", seed=42,
    ), **kwargs)


def adapter(cls=VolcanoSeedanceAdapter, kind="volcano"):
    return cls(SimpleNamespace(kind=kind), runtime=SimpleNamespace(
        seedance_content=lambda *a, **kw: [{"type": "text", "text": "original prompt"}]
    ))


def test_draft_payload_contains_official_switch():
    body = adapter()._submit_body(request(draft=True))
    assert body["draft"] is True
    assert body["resolution"] == "480p"
    assert body["seed"] == 42
    assert body["duration"] == 8


def test_finalization_only_sends_draft_reference_and_output_options():
    body = adapter()._submit_body(request(draft_task_id="cgt-source", resolution="1080p"))
    assert body["content"] == [{"type": "draft_task", "draft_task": {"id": "cgt-source"}}]
    assert body["resolution"] == "1080p"
    assert not ({"draft", "ratio", "duration", "seed", "generate_audio", "omni_reference_task_type", "prompt"} & body.keys())


@pytest.mark.parametrize("overrides", [
    {"draft": True, "resolution": "720p"},
    {"draft": True, "draft_task_id": "source"},
    {"draft_task_id": "source", "resolution": "480p"},
    {"draft": True, "model": "seedance-2.5-flash", "upstream_model": "doubao-seedance-2-5-flash-261115"},
])
def test_invalid_drafts_rejected_before_network(overrides):
    with pytest.raises(VideoUpstreamError):
        adapter()._submit_body(request(**overrides))


@pytest.mark.parametrize("cls", [VolcanoThirdPartySeedanceAdapter, VolcanoNewApiVideoAdapter, UnifiedVideoCreateAdapter, FakeVideoAdapter])
def test_unsupported_adapters_never_drop_draft(cls):
    with pytest.raises(VideoUpstreamError):
        asyncio.run(adapter(cls, "unsupported").submit(request(draft=True)))


def source_request():
    return {
        "draft_task_id": "cgt-source", "source_provider_name": "official",
        "source_upstream_model": "doubao-seedance-2-5-260628",
        "source_provider_snapshot": {"base_url": "https://ark.example", "binding_fingerprint": "bound"},
        "draft_expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


@pytest.mark.parametrize("field,value", [
    ("source_provider_name", "other"), ("source_upstream_model", "other"),
    ("source_provider_snapshot", {}), ("draft_expires_at", "invalid"),
    ("draft_expires_at", "2020-01-01T00:00:00+00:00"),
])
def test_finalization_revalidates_binding_and_expiry(monkeypatch, field, value):
    monkeypatch.setattr(submission, "video_ports", lambda: SimpleNamespace(
        provider=SimpleNamespace(_provider_binding_fingerprint=lambda p: "bound")
    ))
    raw = source_request()
    raw[field] = value
    with pytest.raises(VideoUpstreamError):
        submission.validate_draft_submission(
            SimpleNamespace(upstream_request=raw),
            SimpleNamespace(kind="volcano", name="official", base_url="https://ark.example"),
            "doubao-seedance-2-5-260628",
        )


def test_finalization_retains_valid_source_binding(monkeypatch):
    monkeypatch.setattr(submission, "video_ports", lambda: SimpleNamespace(
        provider=SimpleNamespace(_provider_binding_fingerprint=lambda p: "bound")
    ))
    raw = source_request()
    assert submission.validate_draft_submission(
        SimpleNamespace(upstream_request=raw),
        SimpleNamespace(kind="volcano", name="official", base_url="https://ark.example"),
        "doubao-seedance-2-5-260628",
    ) == raw
