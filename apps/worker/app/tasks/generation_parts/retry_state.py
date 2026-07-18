from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

MAX_ATTEMPTS = 5
STALE_ATTEMPT_REQUEUE_DELAY_S = 5
MODERATION_RETRY_CAP = 6
RETRY_JITTER_RATIO = 0.20
RETRY_BACKOFF_MAX_SECONDS = 15 * 60


def bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    try:
        current = int(current_attempt or 0)
    except (TypeError, ValueError):
        current = 0
    current = max(0, current)
    if current >= _g._MAX_ATTEMPTS:
        return current, False
    return current + 1, True


def base_retry_backoff_seconds(attempt: int) -> float:
    idx = max(0, int(attempt) - 1)
    if idx < len(_g.RETRY_BACKOFF_SECONDS):
        return float(_g.RETRY_BACKOFF_SECONDS[idx])
    last = float(_g.RETRY_BACKOFF_SECONDS[-1]) if _g.RETRY_BACKOFF_SECONDS else 1.0
    overflow = idx - len(_g.RETRY_BACKOFF_SECONDS) + 1
    return min(
        last * (2**overflow),
        float(_g._RETRY_BACKOFF_MAX_SECONDS),
    )


def retry_delay_seconds(
    attempt: int,
    *,
    jitter_ratio: float = RETRY_JITTER_RATIO,
) -> float:
    base = _g._base_retry_backoff_seconds(attempt)
    if base <= 0 or jitter_ratio <= 0:
        return base
    return base + random.uniform(0, base * jitter_ratio)


def retry_not_before_ttl(delay: float) -> int:
    return max(
        1,
        math.ceil(delay + _g._IMAGE_QUEUE_NOT_BEFORE_GRACE_S),
    )


def generation_attempt_update(
    task_id: str,
    attempt_epoch: int,
    *,
    statuses: tuple[str, ...] | None = None,
) -> Any:
    statement = _g.update(_g.Generation).where(
        _g.Generation.id == task_id,
        _g.Generation.attempt == attempt_epoch,
    )
    if statuses:
        statement = statement.where(_g.Generation.status.in_(statuses))
    return statement


def ensure_generation_updated(
    result: Any,
    task_id: str,
    attempt_epoch: int | None,
) -> None:
    rowcount = getattr(result, "rowcount", None)
    if rowcount == 0:
        raise _g._StaleGenerationAttempt(
            f"generation {task_id} attempt {attempt_epoch} no longer owns row"
        )


async def ensure_generation_attempt_current(
    session: Any,
    task_id: str,
    attempt_epoch: int,
) -> None:
    current_attempt = (
        await session.execute(
            _g.select(_g.Generation.attempt)
            .where(_g.Generation.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current_attempt != attempt_epoch:
        raise _g._StaleGenerationAttempt(
            f"generation {task_id} attempt moved from "
            f"{attempt_epoch} to {current_attempt}"
        )


async def mark_generation_attempt_failed(
    redis: Any,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    attempt: int,
    error_code: str,
    error_message: str,
    retriable: bool,
    statuses: tuple[str, ...] = ("running",),
) -> bool:
    failure_delivery = None
    try:
        async with _g.SessionLocal() as session:
            result = await session.execute(
                _g._generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=statuses,
                ).values(
                    status=_g.GenerationStatus.FAILED.value,
                    progress_stage=_g.GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            _g._ensure_generation_updated(result, task_id, attempt)
            message = await session.get(_g.Message, message_id)
            if message is not None and message.status != _g.MessageStatus.CANCELED:
                message.status = _g.MessageStatus.FAILED
            if not retriable:
                generation = await session.get(_g.Generation, task_id)
                if generation is not None:
                    await _g.worker_billing.release_generation(
                        session,
                        generation,
                        reason=error_code,
                    )
            failure_delivery = _g._stage_generation_event(
                session,
                user_id,
                _g.task_channel(task_id),
                _g.EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": error_code,
                    "message": error_message,
                    "retriable": retriable,
                },
            )
            await session.commit()
            await _g.worker_billing.flush_balance_cache_refreshes(session)
    except _g._StaleGenerationAttempt as stale_exc:
        _g.logger.info(
            "generation failed update skipped by stale attempt "
            "task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    if failure_delivery is None:
        raise RuntimeError("generation failure outbox event was not staged")
    await _g._deliver_generation_event(redis, failure_delivery)
    return True


async def mark_generation_attempt_retrying(
    redis: Any,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    attempt: int,
    error_code: str,
    error_message: str,
    delay: float,
    reason: str,
    max_attempts: int,
) -> bool:
    try:
        async with _g.SessionLocal() as session:
            result = await session.execute(
                _g._generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=_g._RUNNING_GENERATION_STATUSES,
                ).values(
                    status=_g.GenerationStatus.QUEUED.value,
                    progress_stage=_g.GenerationStage.QUEUED,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            _g._ensure_generation_updated(result, task_id, attempt)
            await session.commit()
    except _g._StaleGenerationAttempt as stale_exc:
        _g.logger.info(
            "generation retry update skipped by stale attempt "
            "task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    try:
        await redis.set(
            _g._image_queue_not_before_key(task_id),
            str(time.time() + delay),
            ex=_g._retry_not_before_ttl(delay),
        )
        enqueued = await _g._enqueue_generation_once(
            redis,
            task_id,
            defer_by=delay,
            job_try=attempt + 1,
        )
        if not enqueued:
            return False
    except Exception as enqueue_exc:  # noqa: BLE001
        _g.logger.error("re-enqueue failed task=%s err=%s", task_id, enqueue_exc)
        enqueue_error = "retry_enqueue_failed"
        enqueue_message = f"failed to enqueue retry: {enqueue_exc}"
        await _g._mark_generation_attempt_failed(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            error_code=enqueue_error,
            error_message=enqueue_message[:2000],
            retriable=False,
            statuses=(
                _g.GenerationStatus.QUEUED.value,
                _g.GenerationStatus.RUNNING.value,
            ),
        )
        return False

    await _g.publish_event(
        redis,
        user_id,
        _g.task_channel(task_id),
        _g.EV_GEN_RETRYING,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "retry_delay_seconds": delay,
            "error_code": error_code,
            "error_message": error_message,
            "reason": reason,
        },
    )
    return True


async def maybe_requeue_stale_generation_attempt(
    redis: Any,
    *,
    task_id: str,
    attempt: int,
    reason: str,
    delay: float = STALE_ATTEMPT_REQUEUE_DELAY_S,
) -> bool:
    if attempt <= 0:
        return False
    try:
        async with _g.SessionLocal() as session:
            row = (
                await session.execute(
                    _g.select(
                        _g.Generation.status,
                        _g.Generation.message_id,
                        _g.Generation.user_id,
                    )
                    .where(
                        _g.Generation.id == task_id,
                        _g.Generation.attempt == attempt,
                        _g.Generation.status == _g.GenerationStatus.QUEUED.value,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            if row is None:
                return False
            _status, message_id, user_id = row
            await session.rollback()
    except _g._StaleGenerationAttempt as stale_exc:
        _g.logger.info(
            "stale attempt requeue skipped task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "stale attempt requeue check failed task=%s attempt=%s err=%s",
            task_id,
            attempt,
            exc,
        )
        return False

    try:
        await redis.set(
            _g._image_queue_not_before_key(task_id),
            str(time.time() + delay),
            ex=_g._retry_not_before_ttl(delay),
        )
        await redis.enqueue_job(
            "run_generation",
            task_id,
            _defer_by=delay,
            _job_try=attempt + 1,
        )
    except Exception as exc:  # noqa: BLE001
        _g.logger.warning(
            "stale attempt re-enqueue failed task=%s attempt=%s err=%s",
            task_id,
            attempt,
            exc,
        )
        return False

    await _g.publish_event(
        redis,
        str(user_id),
        _g.task_channel(task_id),
        _g.EV_GEN_RETRYING,
        {
            "generation_id": task_id,
            "message_id": str(message_id),
            "attempt": attempt,
            "max_attempts": _g._MAX_ATTEMPTS,
            "retry_delay_seconds": delay,
            "error_code": "stale_attempt_requeued",
            "error_message": f"stale attempt requeued: {reason}"[:2000],
            "reason": reason,
        },
    )
    return True


async def await_with_lease_guard(
    awaitable: Awaitable[Any],
    lease_lost: asyncio.Event,
    *,
    redis: Any | None = None,
    task_id: str | None = None,
    cancel_poll_interval_s: float = 1.0,
) -> Any:
    if lease_lost.is_set():
        raise _g._LeaseLost("generation lease renewer failed")

    async def wait_cancelled() -> None:
        assert redis is not None
        assert task_id is not None
        interval_s = max(0.05, float(cancel_poll_interval_s))
        while True:
            if await _g._is_cancelled(redis, task_id):
                return
            await asyncio.sleep(interval_s)

    work_task: asyncio.Future[Any] = asyncio.ensure_future(awaitable)
    lease_task = asyncio.create_task(lease_lost.wait())
    cancel_task: asyncio.Task[None] | None = (
        asyncio.create_task(wait_cancelled())
        if redis is not None and task_id is not None
        else None
    )
    try:
        watch_tasks: set[asyncio.Future[Any]] = {work_task, lease_task}
        if cancel_task is not None:
            watch_tasks.add(cancel_task)
        done, _pending = await asyncio.wait(
            watch_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_task in done and lease_lost.is_set():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise _g._LeaseLost("generation lease renewer failed")
        if cancel_task is not None and cancel_task in done:
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise _g._TaskCancelled("cancelled during upstream call")
        return await work_task
    finally:
        if not work_task.done():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task
        if cancel_task is not None:
            cancel_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_task


async def consume_image_iter_close_result(
    image_iter: AsyncIterator[tuple[str, str | None]] | None,
    *,
    task_id: str,
) -> None:
    if image_iter is None:
        return
    try:
        close = getattr(image_iter, "aclose", None)
        if close is not None:
            await close()
    except (asyncio.CancelledError, GeneratorExit):
        pass
    except Exception:  # noqa: BLE001
        _g.logger.debug(
            "generation image iterator aclose failed task=%s",
            task_id,
            exc_info=True,
        )


async def anext_image_with_guards(
    image_iter: AsyncIterator[tuple[str, str | None]],
    lease_lost: asyncio.Event,
    *,
    redis: Any,
    task_id: str,
) -> tuple[str, str | None] | None:
    try:
        return await _g._await_with_lease_guard(
            image_iter.__anext__(),
            lease_lost,
            redis=redis,
            task_id=task_id,
        )
    except StopAsyncIteration:
        return None


def classify_exception(
    exc: BaseException,
    has_partial: bool,
) -> Any:
    if isinstance(exc, _g.StorageDiskFullError):
        return _g.is_retriable(
            _g.EC.DISK_FULL.value,
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, TimeoutError):
        return _g.is_retriable(
            "timeout",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, _g.UpstreamError):
        return _g.is_retriable(
            exc.error_code,
            exc.status_code,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(
        exc,
        (
            _g.httpx.ConnectError,
            _g.httpx.ReadTimeout,
            _g.httpx.RemoteProtocolError,
        ),
    ):
        return _g.is_retriable(
            "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, _g.httpx.HTTPError):
        return _g.is_retriable(
            "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    return _g.RetryDecision(False, f"unhandled {type(exc).__name__}")


def safe_generation_error_details(exc: BaseException) -> dict[str, Any]:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return {}
    details: dict[str, Any] = {}
    transparent_qc = payload.get("transparent_qc")
    if isinstance(transparent_qc, dict):
        sanitized_qc = _g._sanitize_transparent_qc_payload(transparent_qc)
        if sanitized_qc:
            details["transparent_qc"] = sanitized_qc
    transparent_provider = payload.get("transparent_provider")
    if isinstance(transparent_provider, str) and transparent_provider:
        details["transparent_provider"] = transparent_provider[:128]
    return details


def sanitize_transparent_qc_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}

    passed = payload.get("passed")
    if isinstance(passed, bool):
        output["passed"] = passed

    for key in ("score", "alpha_coverage", "largest_component_ratio"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            output[key] = round(float(value), 4)

    border_alpha_max = payload.get("border_alpha_max")
    if isinstance(border_alpha_max, (int, float)) and math.isfinite(
        float(border_alpha_max)
    ):
        output["border_alpha_max"] = max(
            0,
            min(255, int(border_alpha_max)),
        )

    bbox = payload.get("foreground_bbox")
    if (
        isinstance(bbox, (list, tuple))
        and len(bbox) == 4
        and all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in bbox
        )
    ):
        output["foreground_bbox"] = [max(0, int(value)) for value in bbox]
    elif bbox is None and "foreground_bbox" in payload:
        output["foreground_bbox"] = None

    for key in ("failure_reasons", "warnings"):
        raw_items = payload.get(key)
        if isinstance(raw_items, list):
            output[key] = [str(item)[:160] for item in raw_items[:20]]

    return output


def decide_moderation_retry_upgrade(
    *,
    base_decision: Any,
    err_code: str | None,
    err_msg: str,
    is_dual_race: bool,
    reserved_provider_name: str | None,
    enabled_provider_count: int,
    already_avoided_count: int,
    cap: int = MODERATION_RETRY_CAP,
) -> Any | None:
    if base_decision.retriable:
        return None
    if not _g.is_moderation_block(err_code, err_msg):
        return None
    if is_dual_race or not reserved_provider_name:
        return None
    if enabled_provider_count <= 1:
        return None
    if enabled_provider_count - already_avoided_count <= 1:
        return None
    if already_avoided_count + 1 >= min(cap, enabled_provider_count):
        return None
    return _g.RetryDecision(
        retriable=True,
        reason="moderation_blocked try_next_provider",
    )
