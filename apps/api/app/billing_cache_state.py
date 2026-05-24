"""Process-local billing cache wiring for API routes."""

from __future__ import annotations

from .services.billing_cache import BillingCacheService

_billing_cache_service: BillingCacheService | None = None


def configure_billing_cache(service: BillingCacheService | None) -> None:
    global _billing_cache_service
    _billing_cache_service = service


def billing_cache() -> BillingCacheService | None:
    return _billing_cache_service


async def invalidate_balance_cache(user_id: str) -> None:
    service = billing_cache()
    if service is None:
        return
    await service.invalidate(user_id)
