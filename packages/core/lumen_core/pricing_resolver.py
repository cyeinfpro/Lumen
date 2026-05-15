"""Resolve model pricing from DB, optional Redis, process cache, and fallback."""

from __future__ import annotations

import json
import time
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PricingRule
from .pricing import (
    ModelPricing,
    PRICING_SOURCE_DB,
    PRICING_SOURCE_MISSING,
    PRICING_SOURCE_PROCESS,
    PRICING_SOURCE_REDIS,
)
from .pricing_fallback import fallback_pricing_for


UNIT_FIELD_MAP: dict[str, str] = {
    "per_1k_tokens_in": "input_per_1k_micro",
    "per_1k_tokens_out": "output_per_1k_micro",
    "per_1k_tokens_cache_read": "cache_read_per_1k_micro",
    "per_1k_tokens_cache_creation": "cache_creation_per_1k_micro",
    "per_1k_tokens_cache_creation_5m": "cache_creation_5m_per_1k_micro",
    "per_1k_tokens_cache_creation_1h": "cache_creation_1h_per_1k_micro",
    "per_1k_tokens_image_output": "image_output_per_1k_micro",
    "per_1k_tokens_reasoning": "reasoning_per_1k_micro",
    "per_1k_tokens_input_priority": "input_priority_per_1k_micro",
    "per_1k_tokens_output_priority": "output_priority_per_1k_micro",
    "per_1k_tokens_cache_read_priority": "cache_read_priority_per_1k_micro",
    "long_context_threshold": "long_context_threshold_tokens",
    "long_context_input_multiplier": "long_context_input_multiplier_x10000",
    "long_context_output_multiplier": "long_context_output_multiplier_x10000",
}


def pricing_to_json(pricing: ModelPricing) -> str:
    return json.dumps(pricing.__dict__, sort_keys=True, separators=(",", ":"))


def pricing_from_json(raw: str | bytes | None) -> ModelPricing | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = {name: data.get(name) for name in ModelPricing.__dataclass_fields__}
    try:
        return ModelPricing(**fields).with_defaults()
    except TypeError:
        return None


def pricing_from_rules(
    rules: list[PricingRule],
    *,
    source: str = PRICING_SOURCE_DB,
) -> ModelPricing | None:
    values: dict[str, Any] = {}
    for rule in rules:
        field = UNIT_FIELD_MAP.get(rule.unit)
        if not field:
            continue
        values[field] = int(rule.price_micro or 0)
    if not values:
        return None
    values.setdefault("pricing_source", source)
    return ModelPricing(**values).with_defaults()


class PricingResolver:
    def __init__(self, *, redis: Any | None = None, process_ttl_sec: float = 10.0) -> None:
        self.redis = redis
        self.process_ttl_sec = max(0.0, process_ttl_sec)
        self._cache: dict[str, tuple[float, ModelPricing]] = {}

    def _cache_key(self, model: str, channel: str | None) -> str:
        return f"{channel or 'default'}:{model}"

    async def resolve(
        self,
        db: AsyncSession,
        model: str,
        *,
        channel: str | None = None,
    ) -> ModelPricing:
        model_key = (model or "").strip()
        cache_key = self._cache_key(model_key, channel)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        redis_key = f"lumen:pricing:v1:{cache_key}"
        if self.redis is not None:
            try:
                raw = await self.redis.get(redis_key)
                pricing = pricing_from_json(raw)
                if pricing is not None:
                    pricing = ModelPricing(
                        **{
                            **pricing.__dict__,
                            "pricing_source": PRICING_SOURCE_REDIS,
                        }
                    ).with_defaults()
                    self._cache[cache_key] = (
                        now + self.process_ttl_sec,
                        ModelPricing(
                            **{
                                **pricing.__dict__,
                                "pricing_source": PRICING_SOURCE_PROCESS,
                            }
                        ).with_defaults(),
                    )
                    return pricing
            except Exception:
                pass

        pricing = await self._resolve_from_db(db, model_key, channel=channel)
        if pricing is None:
            pricing = fallback_pricing_for(model_key)
        if pricing is None:
            pricing = ModelPricing(pricing_source=PRICING_SOURCE_MISSING).with_defaults()

        self._cache[cache_key] = (
            now + self.process_ttl_sec,
            ModelPricing(
                **{**pricing.__dict__, "pricing_source": PRICING_SOURCE_PROCESS}
            ).with_defaults(),
        )
        if self.redis is not None and pricing.pricing_source == PRICING_SOURCE_DB:
            try:
                await self.redis.set(redis_key, pricing_to_json(pricing), ex=60)
            except Exception:
                pass
        return pricing

    async def _resolve_from_db(
        self,
        db: AsyncSession,
        model: str,
        *,
        channel: str | None,
    ) -> ModelPricing | None:
        variants = [channel, "default"] if channel else ["default"]
        for variant in [v for v in variants if v]:
            rows = (
                await db.execute(
                    select(PricingRule).where(
                        PricingRule.scope == "chat_model",
                        PricingRule.variant == variant,
                        PricingRule.enabled.is_(True),
                    )
                )
            ).scalars().all()
            exact = [row for row in rows if row.key == model]
            pricing = pricing_from_rules(exact, source=PRICING_SOURCE_DB)
            if pricing is not None:
                return pricing
            wildcard = [
                row
                for row in rows
                if ("*" in row.key or "?" in row.key)
                and fnmatchcase(model.lower(), row.key.lower())
            ]
            pricing = pricing_from_rules(wildcard, source=PRICING_SOURCE_DB)
            if pricing is not None:
                return pricing
        return None
