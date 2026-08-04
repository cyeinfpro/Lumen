"""Shared cleanup and progress helpers for image race orchestration."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .transport import ImageProgressCallback


SECONDARY_DURABILITY_EVENTS = frozenset(
    {
        "dispatch_ready",
        "response_ready",
        "image_job_execution",
    }
)


def runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def drain_task_group_result(task_group: asyncio.Future[Any]) -> None:
    with suppress(BaseException):
        task_group.result()


async def cancel_and_wait_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = runtime_services(runtime)
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    for task in pending:
        if task.cancelling() == 0:
            task.cancel()
    grouped = asyncio.gather(*pending, return_exceptions=True)
    try:
        await asyncio.wait_for(
            asyncio.shield(grouped),
            timeout=services.core.RACE_CANCEL_WAIT_S,
        )
    except asyncio.TimeoutError:
        grouped.add_done_callback(services.race.drain_task_group_result)
        services.infrastructure.logger.warning(
            "%s cancel cleanup still pending after %.1fs for %d task(s)",
            label,
            services.core.RACE_CANCEL_WAIT_S,
            len(pending),
        )
    except asyncio.CancelledError:
        grouped.add_done_callback(services.race.drain_task_group_result)
        raise


def completed_race_batch(
    tasks: list[asyncio.Task[Any]],
    done: set[asyncio.Task[Any]],
) -> tuple[list[asyncio.Task[Any]], list[asyncio.Task[Any]]]:
    ordered = [task for task in tasks if task in done]
    successful = [
        task for task in ordered if not task.cancelled() and task.exception() is None
    ]
    return ordered, successful


def simultaneous_bonus_tasks(
    successful: list[asyncio.Task[Any]],
    winner: asyncio.Task[Any],
) -> set[asyncio.Task[Any]]:
    return {task for task in successful if task is not winner}


def has_successful_task(tasks: Iterable[asyncio.Task[Any]]) -> bool:
    return any(
        task.done() and not task.cancelled() and task.exception() is None
        for task in tasks
    )


async def await_irrevocable_task(task: asyncio.Task[Any]) -> Any:
    """Let a started durability boundary finish before propagating cancellation."""
    cancellation_seen = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_seen = True
            if not task.done():
                continue
            result = task.result()
        if cancellation_seen:
            raise asyncio.CancelledError
        return result


async def invoke_progress_callback(
    progress_callback: ImageProgressCallback | None,
    event: dict[str, Any],
) -> None:
    if progress_callback is None:
        return
    result = progress_callback(event)
    if inspect.isawaitable(result):
        await result


def metadata_only_progress(
    progress_callback: ImageProgressCallback | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> ImageProgressCallback:
    services = runtime_services(runtime)

    async def _forward(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type in SECONDARY_DURABILITY_EVENTS:
            await invoke_progress_callback(
                progress_callback,
                event,
            )
            return
        if event_type != "provider_used":
            return
        extra = {
            key: event.get(key)
            for key in (
                "attempt",
                "endpoint_attempt",
                "duration_ms",
                "status",
                "reason",
                "error_code",
                "status_code",
                "byok",
            )
            if event.get(key) is not None
        }
        await services.transport.emit_image_progress(
            progress_callback,
            "provider_used",
            provider=event.get("provider"),
            route=event.get("route"),
            source=event.get("source"),
            endpoint=event.get("endpoint"),
            **extra,
        )

    return _forward


async def cleanup_race_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    label: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = runtime_services(runtime)
    leftovers = [task for task in tasks if not task.done()]
    if not leftovers:
        return
    try:
        await services.race.cancel_and_wait_tasks(
            leftovers,
            label=label,
            runtime=runtime,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        services.infrastructure.logger.debug("%s failed", label, exc_info=True)


__all__ = [
    "await_irrevocable_task",
    "cancel_and_wait_tasks",
    "cleanup_race_tasks",
    "completed_race_batch",
    "drain_task_group_result",
    "has_successful_task",
    "invoke_progress_callback",
    "metadata_only_progress",
    "runtime_services",
    "simultaneous_bonus_tasks",
]
