"""Worker-facing billing cache wrapper."""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from lumen_core.billing_cache import BillingCacheService, WindowUsage


@dataclass(slots=True)
class BillingCacheRuntime:
    service: BillingCacheService | None = None


_RUNTIME = BillingCacheRuntime()


async def configure(redis: Any | None) -> BillingCacheService:
    if _RUNTIME.service is not None:
        await _RUNTIME.service.stop_workers()
    service = BillingCacheService(redis=redis)
    await service.start_workers()
    _RUNTIME.service = service
    return service


async def shutdown() -> None:
    service = _RUNTIME.service
    _RUNTIME.service = None
    if service is not None:
        await service.stop_workers()


def get_billing_cache() -> BillingCacheService | None:
    return _RUNTIME.service


__all__ = [
    "BillingCacheService",
    "WindowUsage",
    "configure",
    "get_billing_cache",
    "shutdown",
]
