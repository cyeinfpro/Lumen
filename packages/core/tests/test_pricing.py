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
    parse_usage,
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
