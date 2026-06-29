from __future__ import annotations

import pytest

from lumen_core import video_billing


def test_video_rounding_uses_round_half_up() -> None:
    assert video_billing.round_micro_for_tokens(500_000, 1) == 1
    assert video_billing.round_micro_for_tokens(1_499_999, 1) == 1
    assert video_billing.round_micro_for_tokens(1_500_000, 1) == 2


def test_video_billing_model_uses_fast_when_upstream_is_fast() -> None:
    assert (
        video_billing.video_billing_model(
            "seedance-2.0",
            "doubao-seedance-2-0-fast-260128",
        )
        == "seedance-2.0-fast"
    )
    assert (
        video_billing.video_billing_model(
            "seedance-2.0-fast",
            "doubao-seedance-2-0-fast-260128",
        )
        == "seedance-2.0-fast"
    )
    assert (
        video_billing.video_billing_model(
            "video-ds-2.0-fast",
            "video-ds-2.0-fast",
        )
        == "seedance-2.0-fast"
    )
    assert (
        video_billing.video_billing_model(
            "seedance-2.0",
            "doubao-seedance-2-0-260128",
        )
        == "seedance-2.0"
    )
    assert (
        video_billing.video_billing_model(
            "video-ds-2.0",
            "video-ds-2.0",
        )
        == "seedance-2.0"
    )


def test_video_billing_model_uses_mini_when_upstream_or_model_is_mini() -> None:
    assert (
        video_billing.video_billing_model(
            "seedance-2.0",
            "doubao-seedance-2-0-mini-260615",
        )
        == "seedance-2.0-mini"
    )
    assert (
        video_billing.video_billing_model(
            "seedance-2.0",
            "doubao-seedance-2-0-mini-260128",
        )
        == "seedance-2.0-mini"
    )
    assert (
        video_billing.video_billing_model(
            "seedance-2.0-mini",
            "doubao-seedance-2-0-mini-260615",
        )
        == "seedance-2.0-mini"
    )


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


def test_smart_duration_uses_max_duration_hold_estimate() -> None:
    assert video_billing.hold_estimate_duration_s(-1) == 15
    assert (
        video_billing.token_upper_bound(
            {"seedance-2.0": {"t2v": {"720p:15": 180_000}}},
            model="seedance-2.0",
            action="t2v",
            resolution="720p",
            duration_s=-1,
        )
        == 180_000
    )


def test_video_duration_estimates_include_official_three_second_bucket() -> None:
    expanded = video_billing.expand_video_duration_estimates(
        {"happyhorse-1.0": {"t2v": {"720p:3": 3_000_000, "720p:15": 15_000_000}}}
    )

    t2v = expanded["happyhorse-1.0"]["t2v"]
    assert t2v["720p:3"] == 3_000_000
    assert sorted(int(key.rsplit(":", 1)[1]) for key in t2v) == list(range(3, 16))


def test_happyhorse_seconds_map_to_internal_video_tokens() -> None:
    assert video_billing.VIDEO_BILLING_TOKENS_PER_SECOND == 1_000_000
    assert (
        video_billing.round_micro_for_tokens(
            3 * video_billing.VIDEO_BILLING_TOKENS_PER_SECOND,
            1_008_000,
        )
        == 3_024_000
    )


def test_video_token_upper_bound_uses_pricing_variant_specific_reference_video() -> (
    None
):
    estimates = {
        "seedance-2.0": {
            "reference": {"720p:5": 108_044},
            "reference_video": {"720p:5": 432_143},
        }
    }

    assert (
        video_billing.token_upper_bound(
            estimates,
            model="seedance-2.0",
            action="reference",
            resolution="720p",
            duration_s=5,
            pricing_variant="reference_video_720p",
        )
        == 432_143
    )


def test_video_token_upper_bound_fails_closed_for_missing_reference_video_estimate() -> (
    None
):
    assert (
        video_billing.token_upper_bound(
            {"seedance-2.0": {"reference": {"720p:5": 108_044}}},
            model="seedance-2.0",
            action="reference",
            resolution="720p",
            duration_s=5,
            pricing_variant="reference_video_720p",
        )
        is None
    )


def test_official_seedance_480p_and_720p_hold_estimates_are_not_equal() -> None:
    price_per_mtoken_micro = 46_000_000

    assert video_billing.round_micro_for_tokens(50_218, price_per_mtoken_micro) >= (
        2_310_000
    )
    assert video_billing.round_micro_for_tokens(108_900, price_per_mtoken_micro) >= (
        4_970_000
    )
    assert 108_900 > 50_218


def test_official_seedance_4k_hold_estimates_cover_current_price_table() -> None:
    assert video_billing.round_micro_for_tokens(971_924, 26_000_000) >= 25_270_000
    assert video_billing.round_micro_for_tokens(3_888_125, 16_000_000) >= 62_210_000


def test_official_seedance_mini_hold_estimates_cover_current_price_table() -> None:
    assert video_billing.round_micro_for_tokens(51_429, 23_000_000) >= 1_180_000
    assert video_billing.round_micro_for_tokens(108_900, 23_000_000) >= 2_494_000
    assert video_billing.round_micro_for_tokens(433_334, 14_000_000) >= 6_066_676


def test_video_pricing_variant_splits_reference_media_kind() -> None:
    assert video_billing.video_pricing_variant("t2v") == "t2v"
    assert video_billing.video_pricing_variant("t2v", resolution="720p") == "t2v_720p"
    assert video_billing.video_pricing_variant("t2v", resolution="4k") == "t2v_4k"
    assert video_billing.split_video_resolution_pricing_variant("t2v_4k") == (
        "t2v",
        "4k",
    )
    assert video_billing.split_video_resolution_pricing_variant("t2v_1080P") == (
        "t2v",
        "1080p",
    )
    assert (
        video_billing.video_pricing_variant(
            "reference",
            [{"kind": "image"}, {"kind": "image"}],
            resolution="1080p",
        )
        == "reference_image_1080p"
    )
    assert (
        video_billing.video_pricing_variant(
            "reference",
            [{"kind": "image"}, {"kind": "video"}],
        )
        == "reference_video"
    )


def test_expand_video_duration_estimates_fills_one_second_steps_conservatively() -> (
    None
):
    expanded = video_billing.expand_video_duration_estimates(
        {
            "seedance-2.0": {
                "t2v": {
                    "720p:5": 60_000,
                    "1080p:5": 130_000,
                    "1080p:10": 280_000,
                }
            }
        }
    )

    t2v = expanded["seedance-2.0"]["t2v"]
    assert t2v["720p:4"] == 60_000
    assert t2v["720p:6"] == 72_000
    assert t2v["720p:15"] == 180_000
    assert t2v["1080p:6"] == 280_000
    assert t2v["1080p:15"] == 420_000
    durations = sorted(
        int(key.rsplit(":", 1)[1]) for key in t2v if key.startswith("720p:")
    )
    assert durations == list(range(3, 16))


@pytest.mark.asyncio
async def test_estimate_video_cost_uses_pricing_and_hold_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit) == ("video", "seedance-2.0", "per_mtoken")
        return 12_345 if variant == "i2v_720p" else None

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
    assert estimate.source == "video.token_hold_estimates:i2v_720p"


@pytest.mark.asyncio
async def test_estimate_video_cost_uses_reference_video_pricing_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit) == ("video", "seedance-2.0", "per_mtoken")
        calls.append(variant)
        return 20_000 if variant == "reference_video_720p" else None

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    estimate = await video_billing.estimate_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="reference",
        resolution="720p",
        duration_s=5,
        estimates={"seedance-2.0": {"reference_video": {"720p:5": 194_286}}},
        pricing_variant="reference_video",
    )

    assert calls == ["reference_video_720p"]
    assert estimate.hold_micro == 3_886
    assert estimate.source == "video.token_hold_estimates:reference_video_720p"


@pytest.mark.asyncio
async def test_estimate_video_cost_derives_reference_video_variant_without_explicit_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit) == ("video", "seedance-2.0", "per_mtoken")
        calls.append(variant)
        return 20_000 if variant == "reference_video_720p" else None

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    estimate = await video_billing.estimate_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="reference",
        resolution="720p",
        duration_s=5,
        estimates={"seedance-2.0": {"reference_video": {"720p:5": 194_286}}},
        reference_media=[{"kind": "video"}],
    )

    assert calls == ["reference_video_720p"]
    assert estimate.hold_micro == 3_886
    assert estimate.source == "video.token_hold_estimates:reference_video_720p"


@pytest.mark.asyncio
async def test_settle_video_cost_derives_reference_video_variant_without_explicit_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit) == ("video", "seedance-2.0", "per_mtoken")
        calls.append(variant)
        return 20_000 if variant == "reference_video_720p" else None

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    charged = await video_billing.settle_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="reference",
        actual_total_tokens=194_286,
        resolution="720p",
        reference_media=[{"kind": "video"}],
    )

    assert calls == ["reference_video_720p"]
    assert charged == 3_886


@pytest.mark.asyncio
async def test_settle_video_cost_caps_implausible_usage_to_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit, variant) == (
            "video",
            "seedance-2.0",
            "per_mtoken",
            "t2v_720p",
        )
        return 1_000_000

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    charged = await video_billing.settle_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="t2v",
        actual_total_tokens=10_000_000_000,
        resolution="720p",
        estimated_micro=5_000,
    )

    assert charged == 5_000


@pytest.mark.asyncio
async def test_estimate_video_cost_falls_back_to_legacy_video_pricing_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        assert (scope, key, unit) == ("video", "seedance-2.0", "per_mtoken")
        calls.append(variant)
        return 10_000 if variant == "t2v" else None

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    estimate = await video_billing.estimate_video_cost(
        object(),  # type: ignore[arg-type]
        model="seedance-2.0",
        action="t2v",
        resolution="720p",
        duration_s=5,
        estimates={"seedance-2.0": {"t2v": {"720p:5": 60_000}}},
    )

    assert calls == ["t2v_720p", "t2v"]
    assert estimate.hold_micro == 600
    assert estimate.source == "video.token_hold_estimates:t2v"


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
