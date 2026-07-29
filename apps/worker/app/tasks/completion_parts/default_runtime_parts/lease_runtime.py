"""Lease, cancellation, and stream-abort control for completion tasks."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from .. import stream as completion_stream


RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


@dataclass(frozen=True, slots=True)
class CleanupDependencies:
    release_lease: Callable[..., Any]
    task_duration_seconds: Any
    safe_outcome: Callable[[str], str]
    logger: Any


async def is_cancelled(
    redis: Any,
    task_id: str,
    *,
    cancel_check_errors_total: Any,
    logger: Any,
) -> bool:
    return await completion_stream._is_cancelled(
        redis,
        task_id,
        hooks=completion_stream.CancellationCheckHooks(
            cancel_check_errors_total=cancel_check_errors_total,
            logger=logger,
        ),
    )


async def raise_if_cancelled(
    redis: Any,
    task_id: str,
    reason: str,
    *,
    is_cancelled: Callable[..., Any],
) -> None:
    await completion_stream._raise_if_completion_cancelled(
        redis,
        task_id,
        reason,
        is_cancelled=is_cancelled,
    )


async def watch_cancel(
    redis: Any,
    task_id: str,
    *,
    cancel_requested: asyncio.Event,
    stop_requested: asyncio.Event,
    poll_interval_s: float,
    is_cancelled: Callable[..., Any],
) -> None:
    await completion_stream._watch_completion_cancel(
        redis,
        task_id,
        cancel_requested=cancel_requested,
        stop_requested=stop_requested,
        poll_interval_s=poll_interval_s,
        is_cancelled=is_cancelled,
    )


async def iter_stream_with_abort(
    stream: Any,
    *,
    cancel_requested: asyncio.Event,
    lease_lost: asyncio.Event,
    tool_tracker: Any,
    tool_idle_timeout_s: float,
    next_event: Callable[..., Any],
) -> AsyncIterator[Any]:
    async for event in completion_stream._iter_completion_stream_with_abort(
        stream,
        cancel_requested=cancel_requested,
        lease_lost=lease_lost,
        tool_tracker=tool_tracker,
        tool_idle_timeout_s=tool_idle_timeout_s,
        next_event=next_event,
    ):
        yield event


async def acquire_lease(
    redis: Any,
    task_id: str,
    worker_token: str,
    *,
    lease_ttl_s: int,
    lease_lost_error: type[BaseException],
) -> None:
    ok = await redis.set(
        f"task:{task_id}:lease",
        worker_token,
        ex=lease_ttl_s,
        nx=True,
    )
    if not ok:
        raise lease_lost_error(f"lease already held task={task_id}")


async def release_lease(
    redis: Any,
    task_id: str,
    worker_token: str,
    *,
    logger: Any,
) -> None:
    try:
        await redis.eval(
            RELEASE_LEASE_LUA,
            1,
            f"task:{task_id}:lease",
            worker_token,
        )
    except Exception:  # noqa: BLE001
        logger.debug("completion lease release failed task=%s", task_id, exc_info=True)


async def lease_renewer(
    redis: Any,
    task_id: str,
    worker_token: str,
    lease_lost: asyncio.Event | None,
    *,
    lease_ttl_s: int,
    lease_renew_s: int,
    logger: Any,
) -> None:
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(lease_renew_s)
            try:
                ok = await redis.eval(
                    RENEW_LEASE_LUA,
                    1,
                    f"task:{task_id}:lease",
                    worker_token,
                    str(lease_ttl_s),
                )
                if int(ok or 0) != 1:
                    if lease_lost is not None:
                        lease_lost.set()
                    logger.warning(
                        "completion lease ownership lost task=%s worker=%s",
                        task_id,
                        worker_token,
                    )
                    return
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.warning(
                    "lease renew failed task=%s err=%s streak=%d",
                    task_id,
                    exc,
                    consecutive_failures,
                )
                if consecutive_failures >= 3:
                    if lease_lost is not None:
                        lease_lost.set()
                    logger.error(
                        "lease renewer giving up task=%s failures=%d",
                        task_id,
                        consecutive_failures,
                    )
                    return
    except asyncio.CancelledError:
        raise


async def cleanup_runtime(
    *,
    redis: Any,
    task_id: str,
    lease_token: str,
    lease_acquired: bool,
    renewer: asyncio.Task[None] | None,
    cancel_stop_requested: asyncio.Event | None,
    cancel_watcher: asyncio.Task[None] | None,
    stream_span_cm: Any | None,
    task_start: float,
    task_outcome: str,
    dependencies: CleanupDependencies,
) -> None:
    if cancel_stop_requested is not None:
        cancel_stop_requested.set()

    async def critical_cleanup() -> None:
        for label, task in (
            ("cancel watcher", cancel_watcher),
            ("lease renewer", renewer),
        ):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException:  # noqa: BLE001
                dependencies.logger.debug(
                    "completion %s cleanup failed task=%s",
                    label,
                    task_id,
                    exc_info=True,
                )
        if lease_acquired:
            await dependencies.release_lease(redis, task_id, lease_token)

    cleanup_future = asyncio.ensure_future(critical_cleanup())
    cancel_during_cleanup = False
    try:
        await asyncio.shield(cleanup_future)
    except asyncio.CancelledError:
        cancel_during_cleanup = True

        def consume_late_cleanup(task: asyncio.Task[None]) -> None:
            with suppress(BaseException):
                task.result()
            dependencies.logger.debug(
                "completion late cleanup finished task=%s",
                task_id,
            )

        cleanup_future.add_done_callback(consume_late_cleanup)
    finally:
        if stream_span_cm is not None:
            with suppress(BaseException):
                stream_span_cm.__exit__(None, None, None)
        try:
            duration = asyncio.get_event_loop().time() - task_start
            dependencies.task_duration_seconds.labels(
                kind="completion",
                outcome=dependencies.safe_outcome(task_outcome),
            ).observe(duration)
        except Exception:  # noqa: BLE001
            pass

    if cancel_during_cleanup:
        raise asyncio.CancelledError()
