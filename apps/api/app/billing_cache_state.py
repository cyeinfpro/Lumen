"""Process-local billing cache wiring for API routes."""

from __future__ import annotations

from dataclasses import dataclass

from .services.billing_cache import BillingCacheService


@dataclass
class _BillingCacheState:
    service: BillingCacheService | None = None


_billing_cache_state = _BillingCacheState()


def configure_billing_cache(service: BillingCacheService | None) -> None:
    _billing_cache_state.service = service


def billing_cache() -> BillingCacheService | None:
    return _billing_cache_state.service


async def invalidate_balance_cache(user_id: str) -> None:
    service = billing_cache()
    if service is None:
        return
    await service.invalidate(user_id)
