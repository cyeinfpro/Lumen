from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from lumen_core.pricing_resolver import _select_rule_group, pricing_from_rules


def _rule(
    key: str,
    unit: str,
    price_micro: int,
    *,
    priority: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        unit=unit,
        price_micro=price_micro,
        priority=priority,
    )


def test_pricing_from_rules_rejects_cross_pattern_merges() -> None:
    with pytest.raises(ValueError, match="one model pattern"):
        pricing_from_rules(
            [
                _rule("gpt-*", "per_1k_tokens_in", 100),
                _rule("gpt-4*", "per_1k_tokens_out", 200),
            ]  # type: ignore[arg-type]
        )


def test_pricing_from_rules_rejects_mixed_group_priorities() -> None:
    with pytest.raises(ValueError, match="share one priority"):
        pricing_from_rules(
            [
                _rule("gpt-*", "per_1k_tokens_in", 100, priority=1),
                _rule("gpt-*", "per_1k_tokens_out", 200, priority=2),
            ]  # type: ignore[arg-type]
        )


def test_wildcard_selection_is_order_independent_and_keeps_one_group() -> None:
    rules = [
        _rule("gpt-*", "per_1k_tokens_in", 100),
        _rule("gpt-*", "per_1k_tokens_out", 200),
        _rule("gpt-4*", "per_1k_tokens_in", 400),
        _rule("gpt-4*", "per_1k_tokens_out", 800),
    ]

    for seed in range(100):
        shuffled = list(rules)
        random.Random(seed).shuffle(shuffled)
        selected = _select_rule_group(shuffled, "gpt-4.1")  # type: ignore[arg-type]
        assert {row.key for row in selected} == {"gpt-4*"}
        pricing = pricing_from_rules(selected)  # type: ignore[arg-type]
        assert pricing is not None
        assert pricing.input_per_1k_micro == 400
        assert pricing.output_per_1k_micro == 800


def test_wildcard_priority_wins_before_literal_specificity() -> None:
    rules = [
        _rule("gpt-*", "per_1k_tokens_in", 100, priority=10),
        _rule("gpt-4*", "per_1k_tokens_in", 400, priority=0),
    ]

    selected = _select_rule_group(rules, "gpt-4.1")  # type: ignore[arg-type]

    assert [row.key for row in selected] == ["gpt-*"]


def test_exact_match_wins_over_higher_priority_wildcard() -> None:
    rules = [
        _rule("gpt-*", "per_1k_tokens_in", 100, priority=100),
        _rule("gpt-4.1", "per_1k_tokens_in", 400, priority=0),
    ]

    selected = _select_rule_group(rules, "gpt-4.1")  # type: ignore[arg-type]

    assert [row.key for row in selected] == ["gpt-4.1"]
