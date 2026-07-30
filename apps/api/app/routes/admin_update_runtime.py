"""Router-owned marker cleanup runtime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request


MARKER_CLEANUP_RUNTIME_STATE_KEY = "_admin_update_marker_cleanup_runtime"


@dataclass(slots=True)
class MarkerCleanupRuntime:
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def schedule(
        self,
        proc,
        cleanup: Callable[[object], Awaitable[None]],
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(cleanup(proc))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def shutdown(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)


def marker_cleanup_runtime(request: Request) -> MarkerCleanupRuntime:
    runtime = getattr(request.app.state, MARKER_CLEANUP_RUNTIME_STATE_KEY, None)
    if not isinstance(runtime, MarkerCleanupRuntime):
        raise RuntimeError("admin update marker cleanup runtime is unavailable")
    return runtime


@asynccontextmanager
async def marker_cleanup_lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = MarkerCleanupRuntime()
    setattr(app.state, MARKER_CLEANUP_RUNTIME_STATE_KEY, runtime)
    try:
        yield
    finally:
        await runtime.shutdown()
        if getattr(app.state, MARKER_CLEANUP_RUNTIME_STATE_KEY, None) is runtime:
            delattr(app.state, MARKER_CLEANUP_RUNTIME_STATE_KEY)
