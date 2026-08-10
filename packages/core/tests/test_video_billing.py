from __future__ import annotations

import pytest

from lumen_core import video_billing


def test_video_rounding_always_rounds_up_so_platform_absorbs_nothing() -> None:
    # 纯转嫁：任何非零尾数都进位给用户，平台不吞任何一分零头。
    assert video_billing.round_micro_for_tokens(500_000, 1) == 1
    assert video_billing.round_micro_for_tokens(1_499_999, 1) == 2
    assert video_billing.round_micro_for_tokens(1_500_000, 1) == 2
    # 半 micro 以下曾被 ROUND_HALF_UP 抹平为 0，现在必须收满 1 micro。
    assert video_billing.round_micro_for_tokens(1, 1) == 1
    assert video_billing.round_micro_for_tokens(499_999, 1) == 1
    # 精确整除时不应无谓多收。
    assert video_billing.round_micro_for_tokens(2_000_000, 1) == 2
    # 零用量仍然是零，进位不会凭空造出费用。
    assert video_billing.round_micro_for_tokens(0, 46_000_000) == 0


def test_video_rounding_never_undercharges_relative_to_exact_cost() -> None:
    from decimal import Decimal

    for tokens, price in (
        (50_218, 46_000_000),
        (108_900, 23_000_000),
        (971_924, 26_000_000),
        (3_888_125, 16_000_000),
        (433_334, 14_000_000),
        (1, 999_999),
    ):
        exact = Decimal(tokens) * Decimal(price) / Decimal(1_000_000)
        charged = video_billing.round_micro_for_tokens(tokens, price)
        # 收的钱必须 >= 真实成本（永不少收），且溢出不超过 1 micro（不乱收）。
        assert Decimal(charged) >= exact
        assert Decimal(charged) - exact < 1


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
    # 回归：video-ds-2-0-mini 与 fast 正则对称（_SEEDANCE_20_FAST_RE 已覆盖
    # video-ds-2-0-fast），mini 正则漏配会导致该上游 id 被子串判定落到标准版
    # seedance-2.0，按 20 元/MTok 而非 mini 的 23 元/MTok 结算，平台少收。
    assert (
        video_billing.video_billing_model(
            "video-ds-2.0-mini",
            "video-ds-2.0-mini",
        )
        == "seedance-2.0-mini"
    )
    assert (
        video_billing.video_billing_model(
            "seedance-2.0",
            "video-ds-2-0-mini",
        )
        == "seedance-2.0-mini"
    )


def test_video_billing_model_canonicalizes_seedance_25_identifiers() -> None:
    for model, upstream_model in (
        ("seedance-2.5", None),
        ("seedance-2.0", "doubao-seedance-2-5-260628"),
        ("seedance-2.5", "dreamina-seedance-2-5-260628"),
        ("video-ds-2.5", "video-ds-2.5"),
    ):
        assert (
            video_billing.video_billing_model(model, upstream_model)
            == video_billing.SEEDANCE_25_MODEL
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
    assert video_billing.hold_estimate_duration_s(-1, model="seedance-2.0") == 15
    assert video_billing.hold_estimate_duration_s(-1, model="seedance-2.5") == 30
    assert (
        video_billing.hold_estimate_duration_s(
            -1,
            model="seedance-2.0",
            upstream_model="doubao-seedance-2-5-260628",
        )
        == 30
    )
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
    assert (
        video_billing.token_upper_bound(
            {"seedance-2.5": {"t2v": {"720p:15": 180_000, "720p:30": 360_000}}},
            model="seedance-2.5",
            action="t2v",
            resolution="720p",
            duration_s=-1,
        )
        == 360_000
    )


def test_video_duration_estimates_include_official_three_second_bucket() -> None:
    expanded = video_billing.expand_video_duration_estimates(
        {"happyhorse-1.0": {"t2v": {"720p:3": 3_000_000, "720p:15": 15_000_000}}}
    )

    t2v = expanded["happyhorse-1.0"]["t2v"]
    assert t2v["720p:3"] == 3_000_000
    assert sorted(int(key.rsplit(":", 1)[1]) for key in t2v) == list(range(3, 16))


def test_seedance_25_duration_estimates_expand_through_thirty_seconds() -> None:
    expanded = video_billing.expand_video_duration_estimates(
        {
            "seedance-2.5": {
                "t2v": {
                    "720p:4": 80_000,
                    "720p:30": 600_000,
                }
            }
        }
    )

    t2v = expanded["seedance-2.5"]["t2v"]
    assert t2v["720p:4"] == 80_000
    assert t2v["720p:30"] == 600_000
    assert sorted(int(key.rsplit(":", 1)[1]) for key in t2v) == list(range(4, 31))


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


@pytest.mark.parametrize("raw", ["1080p", "1080P", " 1080p ", "1080P "])
def test_video_pricing_variant_normalizes_resolution_case(raw: str) -> None:
    """分辨率大小写/空白必须归一，否则查不中定价规则就按低价结算。

    F-21：拼接侧只做 strip 而反解析侧还做 lower，两边不对称 —— ``1080P``
    拼出 ``t2v_1080P``，运营录入的规则键却是 ``t2v_1080p``，查表落空后一路
    回退到不带分辨率的基础 variant。高分辨率单价通常更高，回退等于少收，
    差额由平台吸收，踩「纯转嫁」红线。
    """
    variant = video_billing.video_pricing_variant("t2v", resolution=raw)
    assert variant == "t2v_1080p"
    # 拼接 → 反解析必须是无损往返，这正是原先不对称的地方
    assert video_billing.split_video_resolution_pricing_variant(variant) == (
        "t2v",
        "1080p",
    )


def test_video_pricing_variant_keeps_unknown_resolution_suffix() -> None:
    """未知分辨率保留后缀，不做白名单裁剪。

    审计建议「白名单枚举校验」，此处刻意不采纳：``_pricing_fallback_variants``
    已经无条件把基础 variant 放进回退链，未知分辨率本就能优雅降级；而一旦加
    白名单，运营为新分辨率（如 8k）配的规则会因枚举没同步而被直接丢弃，只能
    按基础价结算 —— 又是平台吸收成本。宁可多一个查不中的键，也不丢高价规则。
    """
    assert video_billing.video_pricing_variant("t2v", resolution="8K") == "t2v_8k"


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
    assert (
        video_billing.video_pricing_variant(
            "reference",
            [{"kind": "audio"}],
            resolution="720p",
        )
        == "reference_image_720p"
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
async def test_settle_video_cost_raises_error_when_usage_exceeds_estimate(
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

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.settle_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            actual_total_tokens=100_000_000,
            resolution="720p",
            estimated_micro=5_000,
        )

    assert excinfo.value.code == "video_cost_exceeds_estimate"
    assert excinfo.value.status_code == 500
    # 成本已经算出来了，只是超预估。上层要靠这个值全额转嫁，不能是 None。
    assert excinfo.value.actual_micro == 100_000_000


@pytest.mark.asyncio
async def test_settle_video_cost_rejects_out_of_range_token_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 F-5：因子越界必须在算钱之前拒绝，且不能封顶。

    封顶意味着超出部分平台自己吞，与「上游扣费用户必付」冲突。越界一律抛错，
    交人工核对上游用量——这条路径与「超预估倍数」是两回事，错误码也不同。
    """

    async def fake_price(
        _db, *, scope: str, key: str, unit: str, variant: str
    ) -> int | None:
        return 1_000_000

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.settle_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            actual_total_tokens=video_billing.MAX_BILLABLE_TOKENS + 1,
            resolution="720p",
            estimated_micro=5_000,
        )

    assert excinfo.value.code == "video_cost_factor_out_of_range"
    assert excinfo.value.status_code == 500
    # 越界时成本根本没算出来，不能给上层一个可以拿去扣款的数字。
    assert excinfo.value.actual_micro is None


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
async def test_estimate_video_cost_fails_closed_for_zero_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def zero_price(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(video_billing, "pricing_price_micro", zero_price)

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


@pytest.mark.asyncio
async def test_settle_video_cost_fails_closed_for_zero_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def zero_price(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(video_billing, "pricing_price_micro", zero_price)

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.settle_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            actual_total_tokens=60_000,
            resolution="720p",
        )

    assert excinfo.value.code == "video_pricing_missing"


@pytest.mark.asyncio
async def test_settle_video_cost_rejects_zero_actual_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_price(*_args, **_kwargs):
        return 10_000

    monkeypatch.setattr(video_billing, "pricing_price_micro", fake_price)

    with pytest.raises(video_billing.VideoBillingError) as excinfo:
        await video_billing.settle_video_cost(
            object(),  # type: ignore[arg-type]
            model="seedance-2.0",
            action="t2v",
            actual_total_tokens=0,
            resolution="720p",
        )

    assert excinfo.value.code == "video_invalid_settlement"
