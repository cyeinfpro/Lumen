"""Offline contract tests for the official Seedance 2.5 Flash model."""
import pytest

from lumen_core import video_billing
from lumen_core.video_providers import (
    is_seedance_25_flash_identifier,
    is_seedance_25_identifier,
    parse_video_provider_item,
    seedance_allowed_resolutions,
    seedance_allows_audio_only_reference,
    seedance_duration_is_valid,
    seedance_reference_media_limits,
    seedance_supports_draft,
)


@pytest.mark.parametrize("model", [
    "seedance-2.5-flash",
    "doubao-seedance-2-5-flash-261115",
    "dreamina-seedance-2-5-flash-261115",
    "namespace/video-ds-2.5-flash",
])
def test_flash_official_capabilities(model):
    assert is_seedance_25_identifier(model)
    assert is_seedance_25_flash_identifier(model)
    assert seedance_allowed_resolutions(model) == ("480p", "720p")
    assert seedance_reference_media_limits(model) == {"image": 10, "video": 5, "audio": 5}
    assert seedance_allows_audio_only_reference(model)
    assert all(seedance_duration_is_valid(n, model) for n in (-1, 4, 30))
    assert not seedance_duration_is_valid(31, model)
    assert not seedance_duration_is_valid(3, model)
    assert not seedance_supports_draft(model)
    assert video_billing.video_billing_model(model) == "seedance-2.5-flash"
    assert video_billing.hold_estimate_duration_s(-1, model=model) == 30


@pytest.mark.parametrize("model", [
    "not-seedance-2.5-flash", "seedance-2.5-flasher", "seedance-2.5-flash-preview",
])
def test_flash_identifier_boundaries(model):
    assert not is_seedance_25_flash_identifier(model)


def test_standard_draft_and_flash_upstream_capability_override():
    assert seedance_supports_draft("doubao-seedance-2-5-260628")
    assert not seedance_supports_draft("seedance-2.0")
    assert not seedance_supports_draft("seedance-2.5", "doubao-seedance-2-5-flash-261115")
    assert seedance_allowed_resolutions("seedance-2.5", "doubao-seedance-2-5-flash-261115") == ("480p", "720p")
    assert video_billing.video_billing_model("seedance-2.5", "doubao-seedance-2-5-flash-261115") == "seedance-2.5-flash"


def test_flash_domestic_alias():
    provider = parse_video_provider_item({
        "name": "test", "kind": "volcano", "base_url": "https://example.com",
        "api_key": "test", "models": {"seedance-2.5-flash": "dreamina-seedance-2-5-flash-261115"},
    }, index=0)
    assert provider.upstream_model_for("seedance-2.5-flash", "t2v") == "doubao-seedance-2-5-flash-261115"


def test_flash_estimates_expand_to_thirty_seconds():
    expanded = video_billing.expand_video_duration_estimates({
        "seedance-2.5-flash": {"t2v": {"720p:5": 100000}},
    })
    assert expanded["seedance-2.5-flash"]["t2v"]["720p:30"] == 600000


@pytest.mark.asyncio
async def test_flash_missing_price_never_falls_back_to_standard(monkeypatch):
    keys = []

    async def lookup(_db, *, scope, key, variant, unit):
        keys.append(key)
        return 70000000 if key == "seedance-2.5" else None

    monkeypatch.setattr(video_billing, "pricing_price_micro", lookup)
    with pytest.raises(video_billing.VideoBillingError, match="missing enabled video pricing"):
        await video_billing.estimate_video_cost(
            None, model="seedance-2.5-flash", action="t2v", resolution="720p",
            duration_s=5, estimates={},
        )
    assert keys and set(keys) == {"seedance-2.5-flash"}
