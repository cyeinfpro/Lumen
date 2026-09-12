"""Regression coverage for effective output-rate inheritance and persisted amounts."""

import unittest

from lumen_core.pricing import (
    ModelPricing,
    PricingOverflowError,
    UsageTokens,
    compute_breakdown,
    missing_pricing_buckets,
    model_pricing_from_snapshot,
)


class PricingInheritedRateTests(unittest.TestCase):
    def test_priority_only_rate_satisfies_inherited_buckets(self):
        pricing = ModelPricing(output_priority_per_1k_micro=200)
        usage = UsageTokens(0, 2000, reasoning_tokens=1000, image_output_tokens=1000)
        self.assertEqual(missing_pricing_buckets(pricing, usage, service_tier="priority"), ())
        self.assertEqual(compute_breakdown(pricing, usage, service_tier="priority").actual_cost_micro, 400)

    def test_snapshot_preserves_inheritance(self):
        pricing = ModelPricing(output_priority_per_1k_micro=200).with_defaults()
        restored = model_pricing_from_snapshot(pricing.model_dump())
        usage = UsageTokens(0, 1000, reasoning_tokens=1000)
        self.assertEqual(missing_pricing_buckets(restored, usage, service_tier="priority"), ())
        self.assertEqual(compute_breakdown(restored, usage, service_tier="priority").reasoning_cost_micro, 200)

    def test_explicit_independent_rates_are_not_overridden(self):
        pricing = ModelPricing(output_per_1k_micro=100, output_priority_per_1k_micro=200,
                               reasoning_per_1k_micro=77, image_output_per_1k_micro=33)
        usage = UsageTokens(0, 2000, reasoning_tokens=1000, image_output_tokens=1000)
        cost = compute_breakdown(pricing, usage, service_tier="priority")
        self.assertEqual((cost.reasoning_cost_micro, cost.image_output_cost_micro), (77, 33))

    def test_long_context_applies_to_inherited_output_rates(self):
        pricing = ModelPricing(output_per_1k_micro=100, long_context_threshold_tokens=1,
                               long_context_output_multiplier_x10000=20000)
        usage = UsageTokens(0, 1000, reasoning_tokens=1000)
        self.assertEqual(compute_breakdown(pricing, usage).reasoning_cost_micro, 200)

    def test_legacy_materialized_snapshot_is_not_repriced(self):
        pricing = model_pricing_from_snapshot({"output_per_1k_micro": 100,
                                              "output_priority_per_1k_micro": 200,
                                              "reasoning_per_1k_micro": 100})
        self.assertEqual(compute_breakdown(pricing, UsageTokens(0, 1000, reasoning_tokens=1000),
                                           service_tier="priority").actual_cost_micro, 100)

    def test_final_amount_overflow_is_rejected_before_persistence(self):
        with self.assertRaises(PricingOverflowError) as raised:
            compute_breakdown(ModelPricing(input_per_1k_micro=10**9), UsageTokens(10**9, 0),
                              rate_multiplier_x10000=10**9)
        self.assertEqual(raised.exception.field, "actual_cost_micro")
