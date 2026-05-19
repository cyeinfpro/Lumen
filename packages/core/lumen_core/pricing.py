"""Cache-aware token pricing primitives.

This module is deliberately IO-free.  API and worker code can parse provider
usage into :class:`UsageTokens`, resolve a :class:`ModelPricing` elsewhere, and
then call :func:`compute_breakdown` with deterministic integer arithmetic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


PRICING_SOURCE_DB = "db"
PRICING_SOURCE_REDIS = "redis"
PRICING_SOURCE_PROCESS = "process"
PRICING_SOURCE_FALLBACK = "fallback"
PRICING_SOURCE_MISSING = "missing"


def _nonnegative(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    cur: Any = mapping
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class UsageTokens:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    reasoning_tokens: int = 0
    image_output_tokens: int = 0

    def normalized(self) -> "UsageTokens":
        return UsageTokens(
            input_tokens=max(0, int(self.input_tokens)),
            output_tokens=max(0, int(self.output_tokens)),
            cache_read_tokens=max(0, int(self.cache_read_tokens)),
            cache_creation_tokens=max(0, int(self.cache_creation_tokens)),
            cache_creation_5m_tokens=max(0, int(self.cache_creation_5m_tokens)),
            cache_creation_1h_tokens=max(0, int(self.cache_creation_1h_tokens)),
            reasoning_tokens=max(0, int(self.reasoning_tokens)),
            image_output_tokens=max(0, int(self.image_output_tokens)),
        )

    def model_dump(self) -> dict[str, int]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_micro: int = 0
    output_per_1k_micro: int = 0
    cache_read_per_1k_micro: int = 0
    cache_creation_per_1k_micro: int = 0
    cache_creation_5m_per_1k_micro: int = 0
    cache_creation_1h_per_1k_micro: int = 0
    image_output_per_1k_micro: int = 0
    reasoning_per_1k_micro: int = 0
    input_priority_per_1k_micro: int = 0
    output_priority_per_1k_micro: int = 0
    cache_read_priority_per_1k_micro: int = 0
    long_context_threshold_tokens: int = 0
    long_context_input_multiplier_x10000: int = 10_000
    long_context_output_multiplier_x10000: int = 10_000
    supports_cache_breakdown: bool = True
    pricing_source: str = PRICING_SOURCE_MISSING

    def with_defaults(self) -> "ModelPricing":
        input_rate = max(0, int(self.input_per_1k_micro))
        output_rate = max(0, int(self.output_per_1k_micro))
        cache_creation = (
            int(self.cache_creation_per_1k_micro)
            if self.cache_creation_per_1k_micro > 0
            else (input_rate * 125) // 100
        )
        cache_read = (
            int(self.cache_read_per_1k_micro)
            if self.cache_read_per_1k_micro > 0
            else input_rate
        )
        cache_5m = (
            int(self.cache_creation_5m_per_1k_micro)
            if self.cache_creation_5m_per_1k_micro > 0
            else cache_creation
        )
        cache_1h = (
            int(self.cache_creation_1h_per_1k_micro)
            if self.cache_creation_1h_per_1k_micro > 0
            else (cache_creation * 160) // 100
        )
        return ModelPricing(
            input_per_1k_micro=input_rate,
            output_per_1k_micro=output_rate,
            cache_read_per_1k_micro=cache_read,
            cache_creation_per_1k_micro=cache_creation,
            cache_creation_5m_per_1k_micro=cache_5m,
            cache_creation_1h_per_1k_micro=cache_1h,
            image_output_per_1k_micro=(
                int(self.image_output_per_1k_micro)
                if self.image_output_per_1k_micro > 0
                else output_rate
            ),
            reasoning_per_1k_micro=(
                int(self.reasoning_per_1k_micro)
                if self.reasoning_per_1k_micro > 0
                else output_rate
            ),
            input_priority_per_1k_micro=max(0, int(self.input_priority_per_1k_micro)),
            output_priority_per_1k_micro=max(0, int(self.output_priority_per_1k_micro)),
            cache_read_priority_per_1k_micro=max(
                0, int(self.cache_read_priority_per_1k_micro)
            ),
            long_context_threshold_tokens=max(
                0, int(self.long_context_threshold_tokens)
            ),
            long_context_input_multiplier_x10000=max(
                0, int(self.long_context_input_multiplier_x10000 or 10_000)
            ),
            long_context_output_multiplier_x10000=max(
                0, int(self.long_context_output_multiplier_x10000 or 10_000)
            ),
            supports_cache_breakdown=bool(self.supports_cache_breakdown),
            pricing_source=self.pricing_source,
        )


@dataclass(frozen=True)
class CostBreakdown:
    input_cost_micro: int
    output_cost_micro: int
    cache_read_cost_micro: int
    cache_creation_cost_micro: int
    image_output_cost_micro: int
    reasoning_cost_micro: int
    long_context_applied: bool
    priority_tier_applied: bool
    rate_multiplier_x10000: int
    total_cost_micro: int
    actual_cost_micro: int
    billing_mode: str = "token"
    pricing_source: str = PRICING_SOURCE_MISSING

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def parse_usage(provider: str, usage: dict[str, Any] | None) -> UsageTokens:
    """Parse provider-specific usage payloads into canonical token buckets.

    OpenAI prompt/cache usage reports cached tokens inside prompt/input tokens,
    so cached tokens are subtracted from billable input. Anthropic reports
    cache read/create buckets separately, so its input token count is left as
    the non-cached input reported by the provider.
    """
    if not isinstance(usage, dict):
        return UsageTokens(0, 0)
    provider_norm = (provider or "").lower()

    raw_input = _nonnegative(
        _first_present(
            usage.get("input_tokens"),
            usage.get("prompt_tokens"),
            usage.get("promptTokenCount"),
        )
    )
    raw_output = _nonnegative(
        _first_present(
            usage.get("output_tokens"),
            usage.get("completion_tokens"),
            usage.get("candidatesTokenCount"),
        )
    )

    raw_anthropic_cache_read = _first_present(
        usage.get("cache_read_input_tokens"),
        usage.get("cache_read_tokens"),
    )
    anthropic_cache_read = _nonnegative(raw_anthropic_cache_read)
    anthropic_cache_create = _nonnegative(
        _first_present(
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_creation_tokens"),
        )
    )
    cache_5m = _nonnegative(
        _first_present(
            _nested(usage, "cache_creation", "ephemeral_5m_input_tokens"),
            usage.get("cache_creation_5m_input_tokens"),
            usage.get("cache_creation_5m_tokens"),
        )
    )
    cache_1h = _nonnegative(
        _first_present(
            _nested(usage, "cache_creation", "ephemeral_1h_input_tokens"),
            usage.get("cache_creation_1h_input_tokens"),
            usage.get("cache_creation_1h_tokens"),
        )
    )
    raw_cached_details = _first_present(
        _nested(usage, "input_tokens_details", "cached_tokens"),
        _nested(usage, "prompt_tokens_details", "cached_tokens"),
        usage.get("cached_tokens"),
        usage.get("cachedContentTokenCount"),
    )
    cached_details = _nonnegative(raw_cached_details)

    reasoning_tokens = _nonnegative(
        _first_present(
            _nested(usage, "output_tokens_details", "reasoning_tokens"),
            _nested(usage, "completion_tokens_details", "reasoning_tokens"),
            usage.get("reasoning_tokens"),
        )
    )
    image_output_tokens = _nonnegative(
        _first_present(
            _nested(usage, "output_tokens_details", "image_tokens"),
            _nested(usage, "completion_tokens_details", "image_tokens"),
            usage.get("image_output_tokens"),
            usage.get("image_tokens"),
        )
    )

    if cache_5m or cache_1h:
        anthropic_cache_create = max(anthropic_cache_create, cache_5m + cache_1h)

    cache_read = (
        anthropic_cache_read if raw_anthropic_cache_read is not None else cached_details
    )
    input_tokens = raw_input
    is_anthropic = "anthropic" in provider_norm or "claude" in provider_norm
    if not is_anthropic:
        total_cached = max(cached_details, anthropic_cache_read)
        if total_cached:
            input_tokens = max(0, raw_input - total_cached)

    # Some gateways use `cache_creation_input_tokens` without duration buckets.
    cache_creation = anthropic_cache_create
    return UsageTokens(
        input_tokens=input_tokens,
        output_tokens=raw_output,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cache_creation_5m_tokens=cache_5m,
        cache_creation_1h_tokens=cache_1h,
        reasoning_tokens=reasoning_tokens,
        image_output_tokens=image_output_tokens,
    ).normalized()


def _cost(tokens: int, rate_per_1k_micro: int) -> int:
    return (max(0, int(tokens)) * max(0, int(rate_per_1k_micro)) + 500) // 1000


def _apply_multiplier(value: int, multiplier_x10000: int) -> int:
    return (max(0, int(value)) * max(0, int(multiplier_x10000))) // 10_000


def compute_breakdown(
    pricing: ModelPricing,
    usage: UsageTokens,
    *,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
) -> CostBreakdown:
    pricing = pricing.with_defaults()
    usage = usage.normalized()
    priority = service_tier.lower() in {"priority", "flex_priority", "premium"}

    input_rate = pricing.input_per_1k_micro
    output_rate = pricing.output_per_1k_micro
    cache_read_rate = pricing.cache_read_per_1k_micro
    if priority and pricing.input_priority_per_1k_micro > 0:
        input_rate = pricing.input_priority_per_1k_micro
    if priority and pricing.output_priority_per_1k_micro > 0:
        output_rate = pricing.output_priority_per_1k_micro
    if priority and pricing.cache_read_priority_per_1k_micro > 0:
        cache_read_rate = pricing.cache_read_priority_per_1k_micro

    total_context = (
        usage.input_tokens
        + usage.output_tokens
        + usage.cache_read_tokens
        + usage.cache_creation_tokens
    )
    long_ctx = (
        pricing.long_context_threshold_tokens > 0
        and total_context > pricing.long_context_threshold_tokens
    )
    if long_ctx:
        input_rate = _apply_multiplier(
            input_rate, pricing.long_context_input_multiplier_x10000
        )
        output_rate = _apply_multiplier(
            output_rate, pricing.long_context_output_multiplier_x10000
        )
        cache_read_rate = _apply_multiplier(
            cache_read_rate, pricing.long_context_input_multiplier_x10000
        )

    input_cost = _cost(usage.input_tokens, input_rate)
    output_text_tokens = max(
        0, usage.output_tokens - usage.image_output_tokens - usage.reasoning_tokens
    )
    output_cost = _cost(output_text_tokens, output_rate)
    cache_read_cost = _cost(usage.cache_read_tokens, cache_read_rate)

    cache_creation_bucketed = (
        usage.cache_creation_5m_tokens + usage.cache_creation_1h_tokens
    )
    cache_creation_unbucketed = max(
        0, usage.cache_creation_tokens - cache_creation_bucketed
    )
    cache_creation_cost = (
        _cost(cache_creation_unbucketed, pricing.cache_creation_per_1k_micro)
        + _cost(usage.cache_creation_5m_tokens, pricing.cache_creation_5m_per_1k_micro)
        + _cost(usage.cache_creation_1h_tokens, pricing.cache_creation_1h_per_1k_micro)
    )
    image_cost = _cost(usage.image_output_tokens, pricing.image_output_per_1k_micro)
    reasoning_cost = _cost(usage.reasoning_tokens, pricing.reasoning_per_1k_micro)
    total = (
        input_cost
        + output_cost
        + cache_read_cost
        + cache_creation_cost
        + image_cost
        + reasoning_cost
    )
    multiplier = max(0, int(rate_multiplier_x10000))
    actual = _apply_multiplier(total, multiplier)
    return CostBreakdown(
        input_cost_micro=input_cost,
        output_cost_micro=output_cost,
        cache_read_cost_micro=cache_read_cost,
        cache_creation_cost_micro=cache_creation_cost,
        image_output_cost_micro=image_cost,
        reasoning_cost_micro=reasoning_cost,
        long_context_applied=long_ctx,
        priority_tier_applied=priority,
        rate_multiplier_x10000=multiplier,
        total_cost_micro=total,
        actual_cost_micro=actual,
        pricing_source=pricing.pricing_source,
    )


def build_request_fingerprint(
    *,
    user_id: str,
    account_type: str,
    api_key_id: str | None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    model: str,
    service_tier: str,
    billing_type: int,
    tokens: UsageTokens,
    cost: CostBreakdown,
    version: str = "v2",
) -> str:
    payload = {
        "v": version,
        "user": user_id,
        "account": account_type,
        "api_key": api_key_id or "",
        "request": request_id or "",
        "idempotency_key": idempotency_key or "",
        "model": model,
        "tier": service_tier,
        "billing_type": int(billing_type),
        "tokens": tokens.normalized().model_dump(),
        "total": int(cost.total_cost_micro),
        "actual": int(cost.actual_cost_micro),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{version}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
