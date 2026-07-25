from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

from ..domain.resource_estimate import ImageResourceEstimate


class CapacityExceeded(RuntimeError):
    pass


class CapacityUnavailable(RuntimeError):
    pass


def configured_process_count() -> int:
    for name in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                continue
    return 1


@dataclass(frozen=True)
class CapacityLimits:
    max_concurrency: int
    max_peak_bytes: int
    lease_ttl_seconds: int = 120

    @classmethod
    def from_env(cls) -> "CapacityLimits":
        from ...config import settings

        return cls(
            max_concurrency=settings.image_upload_global_concurrency,
            max_peak_bytes=settings.image_upload_global_peak_bytes,
            lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
        )

    def scaled_for_process(self, process_count: int | None = None) -> "CapacityLimits":
        processes = max(1, process_count or configured_process_count())
        return CapacityLimits(
            max_concurrency=self.max_concurrency // processes,
            max_peak_bytes=max(1, self.max_peak_bytes // processes),
            lease_ttl_seconds=self.lease_ttl_seconds,
        )


class _LocalCapacityLease:
    def __init__(
        self,
        capacity: "ScaledLocalCapacity",
        lease_id: str,
    ) -> None:
        self._capacity = capacity
        self.lease_id = lease_id
        self._released = False

    async def renew(self) -> bool:
        return not self._released

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._capacity._release(self.lease_id)


class ScaledLocalCapacity:
    """Per-process safety layer scaled by the configured API process count."""

    def __init__(
        self,
        limits: CapacityLimits,
        *,
        process_count: int | None = None,
    ) -> None:
        self.limits = limits.scaled_for_process(process_count)
        self._lock = asyncio.Lock()
        self._leases: dict[str, int] = {}

    async def reserve(
        self,
        estimate: ImageResourceEstimate,
    ) -> _LocalCapacityLease:
        async with self._lock:
            used = sum(self._leases.values())
            if (
                self.limits.max_concurrency < 1
                or len(self._leases) >= self.limits.max_concurrency
                or used + estimate.peak_bytes > self.limits.max_peak_bytes
            ):
                raise CapacityExceeded("image upload capacity exhausted")
            lease_id = uuid.uuid4().hex
            self._leases[lease_id] = estimate.peak_bytes
        return _LocalCapacityLease(self, lease_id)

    async def _release(self, lease_id: str) -> None:
        async with self._lock:
            self._leases.pop(lease_id, None)

    async def snapshot(self) -> tuple[int, int]:
        async with self._lock:
            return len(self._leases), sum(self._leases.values())
