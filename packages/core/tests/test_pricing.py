from __future__ import annotations

import pytest

from lumen_core.pricing import (
    MAX_BILLABLE_TOKENS,
    MAX_RATE_PER_1K_MICRO,
    MAX_MULTIPLIER_X10000,
    ModelPricing,
    PricingOverflowError,
    UsageTokens,
    compute_breakdown,
    parse_canonical_nonnegative_int,
    parse_usage,
)

NONCANONICAL_USAGE_VALUES = (
    True,
    -1,
    2.0,
    2.9,
    float("nan"),
    float("inf"),
    "-1",
    "+2",
    "02",
    "2.0",
    " 2",
    "2 ",
    "NaN",
    "inf",
    "\u0662",
    "9" * 5000,
)


def test_cost_rounds_to_nearest_micro_rmb() -> None:
    breakdown = compute_breakdown(
        ModelPricing(input_per_1k_micro=1),
        UsageTokens(input_tokens=500, output_tokens=0),
    )

    assert breakdown.input_cost_micro == 1
    assert breakdown.total_cost_micro == 1


def test_positive_usage_and_rate_never_round_to_zero() -> None:
    breakdown = compute_breakdown(
        ModelPricing(input_per_1k_micro=1),
        UsageTokens(input_tokens=1, output_tokens=0),
    )

    assert breakdown.input_cost_micro == 1
    assert breakdown.total_cost_micro == 1


def test_parse_usage_preserves_legitimate_zero_fields() -> None:
    usage = parse_usage(
        "openai",
        {
            "input_tokens": 0,
            "prompt_tokens": 50,
            "output_tokens": 0,
            "completion_tokens": 75,
        },
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_parse_usage_subtracts_gateway_cache_read_for_non_anthropic_provider() -> None:
    usage = parse_usage(
        "gateway",
        {
            "input_tokens": 1000,
            "output_tokens": 10,
            "cache_read_input_tokens": 200,
        },
    )

    assert usage.input_tokens == 800
    assert usage.cache_read_tokens == 200


@pytest.mark.parametrize("raw", NONCANONICAL_USAGE_VALUES)
def test_parse_usage_rejects_noncanonical_token_values_without_raising(
    raw: object,
) -> None:
    usage = parse_usage(
        "openai",
        {
            "input_tokens": raw,
            "output_tokens": raw,
            "cache_read_tokens": raw,
            "cache_creation_tokens": raw,
            "cache_creation": {
                "ephemeral_5m_input_tokens": raw,
                "ephemeral_1h_input_tokens": raw,
            },
            "input_tokens_details": {"cached_tokens": raw},
            "output_tokens_details": {
                "reasoning_tokens": raw,
                "image_tokens": raw,
            },
        },
    )

    assert parse_canonical_nonnegative_int(raw) is None
    assert usage == UsageTokens(0, 0)


@pytest.mark.parametrize("raw", NONCANONICAL_USAGE_VALUES)
def test_pricing_normalization_is_fail_safe_for_noncanonical_usage(
    raw: object,
) -> None:
    usage = UsageTokens(
        input_tokens=raw,  # type: ignore[arg-type]
        output_tokens=raw,  # type: ignore[arg-type]
        cache_read_tokens=raw,  # type: ignore[arg-type]
        cache_creation_tokens=raw,  # type: ignore[arg-type]
        cache_creation_5m_tokens=raw,  # type: ignore[arg-type]
        cache_creation_1h_tokens=raw,  # type: ignore[arg-type]
        reasoning_tokens=raw,  # type: ignore[arg-type]
        image_output_tokens=raw,  # type: ignore[arg-type]
    )

    assert usage.normalized() == UsageTokens(0, 0)
    assert (
        compute_breakdown(
            ModelPricing(input_per_1k_micro=1_000, output_per_1k_micro=2_000),
            usage,
        ).total_cost_micro
        == 0
    )


@pytest.mark.parametrize("raw", NONCANONICAL_USAGE_VALUES)
def test_pricing_factors_are_fail_safe_for_noncanonical_values(raw: object) -> None:
    pricing = ModelPricing(
        input_per_1k_micro=raw,  # type: ignore[arg-type]
        output_per_1k_micro=raw,  # type: ignore[arg-type]
        cache_read_per_1k_micro=raw,  # type: ignore[arg-type]
        long_context_threshold_tokens=raw,  # type: ignore[arg-type]
        long_context_input_multiplier_x10000=raw,  # type: ignore[arg-type]
        long_context_output_multiplier_x10000=raw,  # type: ignore[arg-type]
    ).with_defaults()

    assert pricing.input_per_1k_micro == 0
    assert pricing.output_per_1k_micro == 0
    assert pricing.cache_read_per_1k_micro == 0
    assert pricing.long_context_threshold_tokens == 0
    assert pricing.long_context_input_multiplier_x10000 == 10_000
    assert pricing.long_context_output_multiplier_x10000 == 10_000


def test_parse_usage_accepts_canonical_ascii_integer_strings() -> None:
    usage = parse_usage(
        "openai",
        {
            "input_tokens": "12",
            "output_tokens": "34",
            "cache_read_tokens": "0",
        },
    )

    assert usage.input_tokens == 12
    assert usage.output_tokens == 34
    assert usage.cache_read_tokens == 0


def test_compute_breakdown_folds_cache_tokens_into_input_when_unsupported() -> None:
    breakdown = compute_breakdown(
        ModelPricing(
            input_per_1k_micro=1_000,
            output_per_1k_micro=2_000,
            cache_read_per_1k_micro=100,
            cache_creation_per_1k_micro=1_250,
            supports_cache_breakdown=False,
            pricing_source="db",
        ),
        UsageTokens(
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=200,
            cache_creation_tokens=300,
        ),
    )

    assert breakdown.input_cost_micro == 600
    assert breakdown.cache_read_cost_micro == 0
    assert breakdown.cache_creation_cost_micro == 0
    assert breakdown.output_cost_micro == 20
    assert breakdown.total_cost_micro == 620


def test_out_of_range_tokens_are_rejected_not_capped() -> None:
    """审计 F-4：因子越界必须抛错。

    封顶（把 token 数压到上限再算）等于让平台吞掉超出部分的上游成本，
    与「上游扣费用户必付」冲突。Python 的 int 不会回绕，真正的风险是金额
    大到写不进 BigInteger 列、直到 commit 才炸——所以要在算之前就拒绝。
    """
    with pytest.raises(PricingOverflowError) as excinfo:
        compute_breakdown(
            ModelPricing(input_per_1k_micro=1_000),
            UsageTokens(input_tokens=MAX_BILLABLE_TOKENS + 1, output_tokens=0),
        )

    assert excinfo.value.field == "tokens"
    assert excinfo.value.limit == MAX_BILLABLE_TOKENS


def test_out_of_range_rate_is_rejected() -> None:
    with pytest.raises(PricingOverflowError) as excinfo:
        compute_breakdown(
            ModelPricing(input_per_1k_micro=MAX_RATE_PER_1K_MICRO + 1),
            UsageTokens(input_tokens=1_000, output_tokens=0),
        )

    assert excinfo.value.field == "rate_per_1k_micro"


def test_out_of_range_multiplier_is_rejected() -> None:
    with pytest.raises(PricingOverflowError) as excinfo:
        compute_breakdown(
            ModelPricing(input_per_1k_micro=1_000),
            UsageTokens(input_tokens=1_000, output_tokens=0),
            rate_multiplier_x10000=MAX_MULTIPLIER_X10000 + 1,
        )

    assert excinfo.value.field == "multiplier_x10000"


def test_realistic_usage_stays_well_below_the_guard() -> None:
    """上界要远高于真实用量，否则会误伤正常请求。"""
    breakdown = compute_breakdown(
        ModelPricing(input_per_1k_micro=15_000, output_per_1k_micro=75_000),
        UsageTokens(input_tokens=2_000_000, output_tokens=200_000),
    )

    assert breakdown.total_cost_micro > 0
