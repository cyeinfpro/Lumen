from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class CapacityLeaseLost(RuntimeError):
    pass


@asynccontextmanager
async def maintained_capacity_lease(
    lease: Any,
    *,
    ttl_seconds: int,
) -> AsyncIterator[None]:
    stopped = asyncio.Event()
    lost = asyncio.Event()

    async def renew_loop() -> None:
        interval = max(5.0, ttl_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                if not await lease.renew():
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    task = asyncio.create_task(renew_loop(), name="image-upload-capacity-renew")
    try:
        yield
        if lost.is_set():
            raise CapacityLeaseLost("image upload capacity lease was lost")
    finally:
        stopped.set()
        await task
        await lease.release()
