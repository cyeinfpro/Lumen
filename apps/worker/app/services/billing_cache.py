"""Worker-facing billing cache wrapper."""

from __future__ import annotations

from typing import Any

from lumen_core.billing_cache import BillingCacheService, WindowUsage

_service: BillingCacheService | None = None


async def configure(redis: Any | None) -> BillingCacheService:
    global _service
    if _service is not None:
        await _service.stop_workers()
    _service = BillingCacheService(redis=redis)
    await _service.start_workers()
    return _service


async def shutdown() -> None:
    global _service
    if _service is not None:
        await _service.stop_workers()
        _service = None


def get_billing_cache() -> BillingCacheService | None:
    return _service


__all__ = [
    "BillingCacheService",
    "WindowUsage",
    "configure",
    "get_billing_cache",
    "shutdown",
]
