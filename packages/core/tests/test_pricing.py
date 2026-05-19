from __future__ import annotations

from lumen_core.pricing import ModelPricing, UsageTokens, compute_breakdown, parse_usage


def test_cost_rounds_to_nearest_micro_rmb() -> None:
    breakdown = compute_breakdown(
        ModelPricing(input_per_1k_micro=1),
        UsageTokens(input_tokens=500, output_tokens=0),
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
