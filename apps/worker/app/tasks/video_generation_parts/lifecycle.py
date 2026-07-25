"""Lease, scheduling, and polling-window lifecycle helpers."""

from __future__ import annotations

from .runtime import video_ports
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any


def now() -> datetime:
    return datetime.now(timezone.utc)


def poll_window_exhausted(generation: Any, current: datetime) -> bool:
    """Return whether the local poll budget is spent.

    Compatibility audit expressions:
    ``generation.poll_count >= _MAX_POLL_COUNT`` and
    ``submitted_at + timedelta(seconds=_MAX_POLL_DURATION_S)``.
    """

    if generation.poll_count >= video_ports()._MAX_POLL_COUNT:
        return True
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return False
    return (
        submitted_at + timedelta(seconds=video_ports()._MAX_POLL_DURATION_S) <= current
    )


def provider_tracking_window_exhausted(
    generation: Any,
    current: datetime,
) -> bool:
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return False
    return (
        submitted_at + timedelta(seconds=video_ports()._MAX_PROVIDER_POLL_DURATION_S)
        <= current
    )


def poll_elapsed_s(generation: Any, current: datetime) -> int | None:
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return None
    return int((current - submitted_at).total_seconds())


async def acquire_lease(redis: Any, task_id: str, token: str) -> bool:
    return bool(
        await redis.set(
            f"video:{task_id}:lease",
            token,
            ex=video_ports()._LEASE_TTL_S,
            nx=True,
        )
    )


async def renew_lease(redis: Any, task_id: str, token: str) -> bool | None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """
    try:
        renewed = await redis.eval(
            lua,
            1,
            f"video:{task_id}:lease",
            token,
            str(video_ports()._LEASE_TTL_S),
        )
        return int(renewed or 0) == 1
    except Exception:
        video_ports().logger.warning(
            "video lease renew failed task=%s",
            task_id,
            exc_info=True,
        )
        return None


async def lease_renewer(
    redis: Any,
    task_id: str,
    token: str,
    *,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    transient_failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=video_ports()._LEASE_RENEW_S)
            return
        except TimeoutError:
            pass
        renewed = await video_ports()._renew_lease(redis, task_id, token)
        if renewed is True:
            transient_failures = 0
            continue
        if renewed is False:
            lost.set()
            return
        transient_failures += 1
        if transient_failures >= video_ports()._LEASE_RENEW_MAX_TRANSIENT_FAILURES:
            lost.set()
            return


async def lease_active(redis: Any, task_id: str) -> bool:
    try:
        return await redis.get(f"video:{task_id}:lease") is not None
    except Exception:
        video_ports().logger.warning(
            "video lease status unavailable task=%s; keeping task fenced",
            task_id,
            exc_info=True,
        )
        return True


def raise_if_video_lease_lost(
    lease_lost: asyncio.Event | None,
    message: str,
) -> None:
    if lease_lost is not None and lease_lost.is_set():
        raise video_ports()._VideoLeaseLost(message)


async def release_lease(redis: Any, task_id: str, token: str) -> None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, f"video:{task_id}:lease", token)
    except Exception:
        video_ports().logger.debug(
            "video lease release failed task=%s",
            task_id,
            exc_info=True,
        )


def enqueue_job_id(kind: str, task_id: str, defer_s: int) -> str:
    delay = max(0, int(defer_s or 0))
    due_at = video_ports().time.time() + delay
    bucket_s = max(1, delay)
    bucket = int(due_at // bucket_s)
    return f"lumen:{kind}:{task_id}:{bucket_s}:{bucket}"


async def enqueue_poll(
    redis: Any,
    task_id: str,
    *,
    defer_s: int | None = None,
) -> None:
    delay = video_ports()._POLL_INTERVAL_S if defer_s is None else defer_s
    await redis.enqueue_job(
        "run_video_poll",
        task_id,
        _defer_by=delay,
        _job_id=video_ports()._enqueue_job_id("video_poll", task_id, delay),
    )


async def enqueue_submit(
    redis: Any,
    task_id: str,
    *,
    defer_s: int | None = None,
) -> None:
    delay = video_ports()._POLL_INTERVAL_S if defer_s is None else defer_s
    await redis.enqueue_job(
        "run_video_generation",
        task_id,
        _defer_by=delay,
        _job_id=video_ports()._enqueue_job_id("video_generation", task_id, delay),
    )


async def enqueue_cached_submit_recovery(
    redis: Any,
    task_id: str,
    *,
    defer_s: int,
) -> bool:
    try:
        cached_submit = await video_ports()._load_submit_result(redis, task_id)
    except Exception:
        video_ports().logger.warning(
            "video cached submit lookup failed task=%s",
            task_id,
            exc_info=True,
        )
        return False
    if cached_submit is None:
        return False
    try:
        await video_ports()._enqueue_submit(redis, task_id, defer_s=defer_s)
    except Exception:
        video_ports().logger.warning(
            "video cached submit recovery enqueue failed task=%s",
            task_id,
            exc_info=True,
        )
        return False
    return True


__all__ = [
    "acquire_lease",
    "enqueue_cached_submit_recovery",
    "enqueue_job_id",
    "enqueue_poll",
    "enqueue_submit",
    "lease_active",
    "lease_renewer",
    "now",
    "poll_elapsed_s",
    "poll_window_exhausted",
    "provider_tracking_window_exhausted",
    "raise_if_video_lease_lost",
    "release_lease",
    "renew_lease",
]
