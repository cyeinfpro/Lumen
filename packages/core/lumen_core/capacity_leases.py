from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Sequence


logger = logging.getLogger(__name__)


class CapacityLeaseLost(RuntimeError):
    pass


@dataclass
class CapacityLeaseGuard:
    lease: Any
    ttl_seconds: float
    safety_seconds: float
    lost: asyncio.Event
    _monotonic: Any
    _last_confirmed_at: float
    _renew_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def create(
        cls,
        lease: Any,
        *,
        ttl_seconds: float,
        monotonic: Any = time.monotonic,
    ) -> CapacityLeaseGuard:
        if ttl_seconds <= 0:
            raise ValueError("capacity lease TTL must be positive")
        safety_seconds = min(
            max(1.0, ttl_seconds / 4.0),
            max(ttl_seconds / 2.0, 0.001),
        )
        return cls(
            lease=lease,
            ttl_seconds=ttl_seconds,
            safety_seconds=safety_seconds,
            lost=asyncio.Event(),
            _monotonic=monotonic,
            _last_confirmed_at=monotonic(),
        )

    def mark_lost(self) -> None:
        self.lost.set()

    def _remaining_seconds(self) -> float:
        deadline = self._last_confirmed_at + self.ttl_seconds - self.safety_seconds
        return deadline - self._monotonic()

    async def assert_owned(self) -> None:
        if self.lost.is_set():
            raise CapacityLeaseLost("capacity lease was lost")
        async with self._renew_lock:
            if self.lost.is_set():
                raise CapacityLeaseLost("capacity lease was lost")
            remaining = self._remaining_seconds()
            if remaining <= 0:
                self.mark_lost()
                raise CapacityLeaseLost("capacity lease expired")
            try:
                owned = await asyncio.wait_for(
                    self.lease.renew(),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.mark_lost()
                raise CapacityLeaseLost(
                    "capacity lease could not be confirmed"
                ) from exc
            if not owned:
                self.mark_lost()
                raise CapacityLeaseLost("capacity lease ownership changed")
            if self.lost.is_set():
                raise CapacityLeaseLost("capacity lease was lost")
            self._last_confirmed_at = self._monotonic()

    async def wait_lost(self) -> None:
        await self.lost.wait()


async def race_with_capacity_lease(
    work: Awaitable[Any],
    guard: CapacityLeaseGuard,
) -> Any:
    return await race_with_capacity_leases(work, (guard,))


async def assert_capacity_leases_owned(
    guards: Sequence[CapacityLeaseGuard],
) -> None:
    for guard in guards:
        await guard.assert_owned()


async def race_with_capacity_leases(
    work: Awaitable[Any],
    guards: Sequence[CapacityLeaseGuard],
) -> Any:
    if not guards:
        return await work
    work_task = asyncio.ensure_future(work)
    lost_tasks = [
        asyncio.create_task(
            guard.wait_lost(),
            name="image-capacity-lease-lost",
        )
        for guard in guards
    ]
    try:
        done, _pending = await asyncio.wait(
            (work_task, *lost_tasks),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if any(task in done for task in lost_tasks):
            work_task.cancel()
            await asyncio.gather(work_task, return_exceptions=True)
            raise CapacityLeaseLost("capacity lease was lost")
        for task in lost_tasks:
            task.cancel()
        await asyncio.gather(*lost_tasks, return_exceptions=True)
        result = await work_task
        await assert_capacity_leases_owned(guards)
        return result
    except BaseException:
        work_task.cancel()
        for task in lost_tasks:
            task.cancel()
        await asyncio.gather(work_task, *lost_tasks, return_exceptions=True)
        raise


@asynccontextmanager
async def maintained_capacity_lease(
    lease: Any,
    *,
    ttl_seconds: float,
) -> AsyncIterator[CapacityLeaseGuard]:
    guard = CapacityLeaseGuard.create(
        lease,
        ttl_seconds=ttl_seconds,
    )
    stopped = asyncio.Event()
    interval = min(max(ttl_seconds / 3.0, 0.05), 10.0)

    async def renew_loop() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await guard.assert_owned()
            except CapacityLeaseLost:
                return

    task = asyncio.create_task(renew_loop(), name="capacity-lease-renew")
    try:
        yield guard
    finally:
        stopped.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release_timeout = min(max(ttl_seconds / 4.0, 1.0), 5.0)
        try:
            await asyncio.wait_for(
                lease.release(),
                timeout=release_timeout,
            )
        except Exception:
            logger.warning("capacity lease release failed", exc_info=True)
