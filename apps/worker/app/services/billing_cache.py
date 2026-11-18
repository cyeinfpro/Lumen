"""Worker-facing billing cache wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.billing_cache import BillingCacheService, WindowUsage

from ..task_runtime import RuntimeSlot


@dataclass(slots=True)
class BillingCacheRuntime:
    service: BillingCacheService | None = None


_RUNTIME_SLOT: RuntimeSlot[BillingCacheRuntime] = RuntimeSlot(
    "worker-billing-cache",
    default_factory=BillingCacheRuntime,
)


def current_runtime() -> BillingCacheRuntime:
    return _RUNTIME_SLOT.current()


async def configure(
    redis: Any | None,
    *,
    runtime: BillingCacheRuntime | None = None,
) -> BillingCacheService:
    existing = _RUNTIME_SLOT.current_or_none()
    active = runtime or existing or BillingCacheRuntime()
    if existing is not None and existing is not active:
        if existing.service is not None:
            await existing.service.stop_workers()
            existing.service = None
        _RUNTIME_SLOT.clear_default(existing)
    if active.service is not None:
        await active.service.stop_workers()
    service = BillingCacheService(redis=redis)
    await service.start_workers()
    active.service = service
    _RUNTIME_SLOT.install_default(active)
    return service


async def shutdown(runtime: BillingCacheRuntime | None = None) -> None:
    active = runtime or _RUNTIME_SLOT.current_or_none()
    if active is None:
        return
    service = active.service
    active.service = None
    _RUNTIME_SLOT.clear_default(active)
    if service is not None:
        await service.stop_workers()


def get_billing_cache() -> BillingCacheService | None:
    active = _RUNTIME_SLOT.current_or_none()
    return active.service if active is not None else None


__all__ = [
    "BillingCacheRuntime",
    "BillingCacheService",
    "WindowUsage",
    "configure",
    "current_runtime",
    "get_billing_cache",
    "shutdown",
]
