from __future__ import annotations

from lumen_core.pricing import ModelPricing, UsageTokens, compute_breakdown, parse_usage


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
