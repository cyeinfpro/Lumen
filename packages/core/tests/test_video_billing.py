from __future__ import annotations

import pytest

from lumen_core import video_billing


def test_video_rounding_uses_round_half_up() -> None:
    assert video_billing.round_micro_for_tokens(500_000, 1) == 1
    assert video_billing.round_micro_for_tokens(1_499_999, 1) == 1
    assert video_billing.round_micro_for_tokens(1_500_000, 1) == 2


def test_video_token_upper_bound_rejects_invalid_values() -> None:
    estimates = {
        "seedance-2.0": {
            "t2v": {
                "720p:5": 60_000,
                "1080p:5": True,
                "1080p:10": -1,
            }
        }
    }

    assert (
        video_billing.token_upper_bound(
            estimates,
            model="seedance-2.0",
            action="t2v",
            resolution="720p",
            duration_s=5,
        )
        == 60_000
    )
    assert (
        video_billing.token_upper_bound(
            estimates,
            model="seedance-2.0",
            action="t2v",
            resolution="1080p",
            duration_s=5,
        )
        is None
    )
    assert (
        video_billing.token_upper_bound(
            estimates,
            model="seedance-2.0",
            action="t2v",
            resolution="1080p",
            duration_s=10,
        )
        is None
    )


@pytest.mark.asyncio
async def test_estimate_video_cost_uses_pricing_and_hold_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_price(_db, *, scope: str, key: str, unit: str, variant: str) -> int:
        assert (scope, key, unit, variant) == (
            "video",
            "seedance-2.0",
            "per_mtoken",
            "i2v",
        )
        return 12_345

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    estimate = await video_billing.estimate_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="i2v",
        resolution="720p",
        duration_s=5,
        estimates={"seedance-2.0": {"i2v": {"720p:5": 60_000}}},
    )

    assert estimate.estimated_tokens == 60_000
    assert estimate.unit_price_micro == 12_345
    assert estimate.hold_micro == 741
    assert estimate.source == "video.token_hold_estimates"


@pytest.mark.asyncio
async def test_estimate_video_cost_fails_closed_without_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_price(*_args, **_kwargs):
        return None

    monkeypatch.setattr(video_billing, "pricing_price_micro", missing_price)

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.estimate_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            resolution="720p",
            duration_s=5,
            estimates={"seedance-2.0": {"t2v": {"720p:5": 60_000}}},
        )

    assert excinfo.value.code == "video_pricing_missing"
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_estimate_video_cost_fails_closed_without_hold_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_price(*_args, **_kwargs):
        return 10_000

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.estimate_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            resolution="1080p",
            duration_s=10,
            estimates={"seedance-2.0": {"t2v": {"720p:5": 60_000}}},
        )

    assert excinfo.value.code == "video_estimate_missing"
    assert excinfo.value.status_code == 503
