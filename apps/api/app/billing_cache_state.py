"""Process-local billing cache wiring for API routes."""

from __future__ import annotations

from dataclasses import dataclass

from lumen_core.billing_cache import BillingCacheService

from .runtime.slot import RuntimeSlot


@dataclass
class BillingCacheState:
    service: BillingCacheService | None = None


_BILLING_CACHE_SLOT: RuntimeSlot[BillingCacheState] = RuntimeSlot(
    "api-billing-cache-state",
    default_factory=BillingCacheState,
)


def configure_billing_cache_state(
    state: BillingCacheState | None = None,
) -> BillingCacheState:
    active = state or BillingCacheState()
    _BILLING_CACHE_SLOT.install_default(active)
    return active


def reset_billing_cache_state(state: BillingCacheState | None = None) -> None:
    active = state or _BILLING_CACHE_SLOT.current_or_none()
    if active is None:
        return
    active.service = None
    _BILLING_CACHE_SLOT.clear_default(active)


def configure_billing_cache(service: BillingCacheService | None) -> None:
    _BILLING_CACHE_SLOT.current().service = service


def billing_cache() -> BillingCacheService | None:
    active = _BILLING_CACHE_SLOT.current_or_none()
    return active.service if active is not None else None


async def invalidate_balance_cache(user_id: str) -> None:
    service = billing_cache()
    if service is None:
        return
    await service.invalidate(user_id)
