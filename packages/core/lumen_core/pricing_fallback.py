"""Built-in fallback pricing for common chat/image-capable models.

Values are stored as micro-RMB per 1k tokens.  They are intentionally fallback
only: database pricing rules remain authoritative and should be kept current by
operators for production billing.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from fnmatch import fnmatchcase

from .pricing import ModelPricing, PRICING_SOURCE_FALLBACK


DEFAULT_USD_TO_CNY = Decimal("7.2")


def _usd_per_1k_to_micro(value: str, usd_to_cny: Decimal = DEFAULT_USD_TO_CNY) -> int:
    micro = (Decimal(value) * usd_to_cny * Decimal(1_000_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(micro)


def _pricing(
    input_usd_per_1k: str,
    output_usd_per_1k: str,
    *,
    cache_read_ratio: Decimal = Decimal("0.10"),
    cache_creation_ratio: Decimal = Decimal("1.25"),
    image_output_usd_per_1k: str | None = None,
    reasoning_usd_per_1k: str | None = None,
    threshold: int = 0,
    long_input_x: int = 10_000,
    long_output_x: int = 10_000,
) -> ModelPricing:
    input_micro = _usd_per_1k_to_micro(input_usd_per_1k)
    output_micro = _usd_per_1k_to_micro(output_usd_per_1k)
    cache_creation = int((Decimal(input_micro) * cache_creation_ratio).to_integral_value())
    return ModelPricing(
        input_per_1k_micro=input_micro,
        output_per_1k_micro=output_micro,
        cache_read_per_1k_micro=int(
            (Decimal(input_micro) * cache_read_ratio).to_integral_value()
        ),
        cache_creation_per_1k_micro=cache_creation,
        cache_creation_5m_per_1k_micro=cache_creation,
        cache_creation_1h_per_1k_micro=int(
            (Decimal(cache_creation) * Decimal("1.6")).to_integral_value()
        ),
        image_output_per_1k_micro=(
            _usd_per_1k_to_micro(image_output_usd_per_1k)
            if image_output_usd_per_1k is not None
            else output_micro
        ),
        reasoning_per_1k_micro=(
            _usd_per_1k_to_micro(reasoning_usd_per_1k)
            if reasoning_usd_per_1k is not None
            else output_micro
        ),
        long_context_threshold_tokens=threshold,
        long_context_input_multiplier_x10000=long_input_x,
        long_context_output_multiplier_x10000=long_output_x,
        pricing_source=PRICING_SOURCE_FALLBACK,
    ).with_defaults()


FALLBACK_PRICING: dict[str, ModelPricing] = {
    # OpenAI family
    "gpt-5.5": _pricing("0.005", "0.015", threshold=200_000, long_input_x=20_000, long_output_x=20_000),
    "gpt-5.4": _pricing("0.004", "0.012", threshold=200_000, long_input_x=20_000, long_output_x=20_000),
    "gpt-5.4-mini": _pricing("0.0008", "0.0032"),
    "gpt-5.3-codex": _pricing("0.003", "0.012"),
    "gpt-4o": _pricing("0.0025", "0.010", image_output_usd_per_1k="0.400"),
    "gpt-4o-mini": _pricing("0.00015", "0.00060"),
    "o3": _pricing("0.002", "0.008", reasoning_usd_per_1k="0.008"),
    "o3-mini": _pricing("0.0011", "0.0044", reasoning_usd_per_1k="0.0044"),
    "o4-mini": _pricing("0.0011", "0.0044", reasoning_usd_per_1k="0.0044"),
    # Anthropic family
    "claude-opus-4-7": _pricing("0.015", "0.075"),
    "claude-sonnet-4-6": _pricing("0.003", "0.015"),
    "claude-haiku-4-5": _pricing("0.0008", "0.004"),
    "claude-3-5-sonnet*": _pricing("0.003", "0.015"),
    "claude-3-5-haiku*": _pricing("0.0008", "0.004"),
    "claude-3-opus*": _pricing("0.015", "0.075"),
    # Gemini family
    "gemini-2.5-pro": _pricing("0.00125", "0.010", threshold=200_000, long_input_x=20_000, long_output_x=15_000),
    "gemini-2.5-flash": _pricing("0.00030", "0.00250"),
    "gemini-2.0-flash": _pricing("0.00010", "0.00040"),
    "gemini-1.5-pro*": _pricing("0.00125", "0.005"),
    "gemini-1.5-flash*": _pricing("0.000075", "0.00030"),
    # Common aliases / wildcards seen through compatible gateways.
    "gpt-4o-*": _pricing("0.0025", "0.010", image_output_usd_per_1k="0.400"),
    "gpt-5.4-*": _pricing("0.004", "0.012", threshold=200_000, long_input_x=20_000, long_output_x=20_000),
    "claude-*sonnet*": _pricing("0.003", "0.015"),
    "claude-*haiku*": _pricing("0.0008", "0.004"),
}


def fallback_pricing_for(model: str) -> ModelPricing | None:
    normalized = (model or "").strip()
    if not normalized:
        return None
    exact = FALLBACK_PRICING.get(normalized)
    if exact is not None:
        return exact
    lowered = normalized.lower()
    for pattern, pricing in FALLBACK_PRICING.items():
        if "*" in pattern and fnmatchcase(lowered, pattern.lower()):
            return replace(pricing, pricing_source=PRICING_SOURCE_FALLBACK)
    return None
