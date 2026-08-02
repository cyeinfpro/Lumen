from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from sqlalchemy import select, update

from lumen_core.constants import (
    EV_GEN_FAILED,
    EV_GEN_RETRYING,
    RETRY_BACKOFF_SECONDS,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    task_channel,
)
from lumen_core.models import Generation, Message
from lumen_core.upstream_billing import (
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
)

from ...provider_runtime.errors import UpstreamError
from ...upstream_parts import GeneratedImageResult
from ...generation_dispatch import DispatchIdentity
from ...retry import RetryDecision, is_moderation_block, is_retriable
from ...storage import StorageDiskFullError
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .event_delivery import stage_generation_event
from .execution_boundary import release_or_settle_generation
from .lease import is_cancelled
from .queue import (
    IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
    enqueue_generation_once,
    image_queue_not_before_key,
)
from .services import RunGenerationDeps


MAX_ATTEMPTS = 5
STALE_ATTEMPT_REQUEUE_DELAY_S = 5
MODERATION_RETRY_CAP = 6
RETRY_JITTER_RATIO = 0.20
RETRY_BACKOFF_MAX_SECONDS = 15 * 60
RUNNING_GENERATION_STATUSES = (GenerationStatus.RUNNING.value,)
logger = logging.getLogger(__name__)
T = TypeVar("T")


class _GenerationExecutionTaskId(str):
    execution_epoch: int

    def __new__(
        cls,
        task_id: str,
        execution_epoch: int,
    ) -> _GenerationExecutionTaskId:
        value = super().__new__(cls, task_id)
        value.execution_epoch = max(0, int(execution_epoch))
        return value


def generation_execution_epoch(task_or_state: object) -> int:
    task = getattr(task_or_state, "generation", task_or_state)
    try:
        return max(0, int(getattr(task, "execution_epoch", 0) or 0))
    except (TypeError, ValueError):
        return 0


def generation_execution_identity(execution_epoch: int, attempt: int) -> int:
    epoch = max(0, int(execution_epoch))
    retry_attempt = max(0, int(attempt))
    return (epoch << 32) | retry_attempt


def generation_execution_trace_id(trace_id: str, execution_epoch: int) -> str:
    """Keep provider idempotency stable inside one epoch and rotate on manual retry."""

    value = str(trace_id).strip()
    prefix, separator, suffix = value.rpartition(":execution:")
    if separator and suffix.isdigit():
        value = prefix
    return f"{value}:execution:{max(0, int(execution_epoch))}"


def generation_execution_task_id(
    task_id: str,
    execution_epoch: int,
) -> str:
    return _GenerationExecutionTaskId(str(task_id), execution_epoch)


def current_generation_execution_epoch(task_id: str) -> int | None:
    value = getattr(task_id, "execution_epoch", None)
    if value is None:
        return None
    return max(0, int(value))


def generation_dispatch_requires_unknown_settlement(state: Any) -> bool:
    request = state.gen_upstream_request_snapshot or {}
    execution_epoch = generation_execution_epoch(state)
    return bool(
        has_upstream_dispatch_receipt(
            request,
            execution_epoch=execution_epoch,
        )
        and not has_proven_undelivered_dispatch(
            request,
            execution_epoch=execution_epoch,
        )
    )


async def finalize_generation_cancel_unknown(state: Any) -> None:
    await _finalize_generation_unknown(
        state,
        status=GenerationStatus.CANCELED.value,
        code=EC.CANCELLED.value,
        error_message="cancelled by user",
        allow_cancel_requested=True,
    )


async def finalize_generation_result_unknown(
    state: Any,
    exc: BaseException,
) -> None:
    await _finalize_generation_unknown(
        state,
        status=GenerationStatus.FAILED.value,
        code=EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        error_message=str(exc)[:2000] or "upstream result is unknown",
        allow_cancel_requested=False,
    )


async def _finalize_generation_unknown(
    state: Any,
    *,
    status: str,
    code: str,
    error_message: str,
    allow_cancel_requested: bool,
) -> None:
    delivery = None
    try:
        async with state.services.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    state.task_id,
                    state.attempt,
                    statuses=(GenerationStatus.RUNNING.value,),
                    allow_cancel_requested=allow_cancel_requested,
                    execution_epoch=generation_execution_epoch(state),
                ).values(
                    status=status,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=code,
                    error_message=error_message,
                )
            )
            ensure_generation_updated(result, state.task_id, state.attempt)
            message_row = await session.get(Message, state.message_id)
            if message_row is not None and message_row.status not in (
                MessageStatus.SUCCEEDED,
                MessageStatus.FAILED,
                MessageStatus.CANCELED,
            ):
                message_row.status = MessageStatus.FAILED
            generation = await session.get(Generation, state.task_id)
            if generation is None:
                raise LookupError(f"generation missing: {state.task_id}")
            await state.services.billing.settle_unknown_upstream(
                session,
                generation,
                reason=code,
                knowledge="unknown",
            )
            delivery = stage_generation_event(
                session,
                state.user_id,
                state.channel,
                EV_GEN_FAILED,
                {
                    "generation_id": state.task_id,
                    "message_id": state.message_id,
                    "execution_epoch": generation_execution_epoch(state),
                    "attempt": state.attempt,
                    "code": code,
                    "message": error_message,
                    "retriable": False,
                },
            )
            await session.commit()
            await state.services.billing.flush_after_commit(session)
    except StaleGenerationAttempt as exc:
        logger.info(
            "generation unknown settlement superseded task=%s epoch=%s "
            "attempt=%s err=%s",
            state.task_id,
            generation_execution_epoch(state),
            state.attempt,
            exc,
        )
        state.task_outcome = "stale_attempt"
        return
    if delivery is None:
        raise RuntimeError("generation unknown settlement event was not staged")
    await state.services.events.deliver(state.redis, delivery)
    state.task_outcome = "failed"


def _expected_execution_epoch(
    task_id: str,
    execution_epoch: int | None,
) -> int | None:
    if execution_epoch is not None:
        return max(0, int(execution_epoch))
    return current_generation_execution_epoch(task_id)


def bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    try:
        current = int(current_attempt or 0)
    except (TypeError, ValueError):
        current = 0
    current = max(0, current)
    if current >= MAX_ATTEMPTS:
        return current, False
    return current + 1, True


def base_retry_backoff_seconds(attempt: int) -> float:
    idx = max(0, int(attempt) - 1)
    if idx < len(RETRY_BACKOFF_SECONDS):
        return float(RETRY_BACKOFF_SECONDS[idx])
    last = float(RETRY_BACKOFF_SECONDS[-1]) if RETRY_BACKOFF_SECONDS else 1.0
    overflow = idx - len(RETRY_BACKOFF_SECONDS) + 1
    return min(
        last * (2**overflow),
        float(RETRY_BACKOFF_MAX_SECONDS),
    )


def retry_delay_seconds(
    attempt: int,
    *,
    jitter_ratio: float = RETRY_JITTER_RATIO,
) -> float:
    base = base_retry_backoff_seconds(attempt)
    if base <= 0 or jitter_ratio <= 0:
        return base
    return base + random.uniform(0, base * jitter_ratio)


def retry_not_before_ttl(delay: float) -> int:
    return max(
        1,
        math.ceil(delay + IMAGE_QUEUE_NOT_BEFORE_GRACE_S),
    )


def generation_attempt_update(
    task_id: str,
    attempt_epoch: int,
    *,
    statuses: tuple[str, ...] | None = None,
    allow_cancel_requested: bool = False,
    execution_epoch: int | None = None,
) -> Any:
    statement = update(Generation).where(
        Generation.id == task_id,
        Generation.attempt == attempt_epoch,
    )
    expected_execution_epoch = _expected_execution_epoch(task_id, execution_epoch)
    if expected_execution_epoch is not None:
        statement = statement.where(
            Generation.execution_epoch == expected_execution_epoch
        )
    if statuses:
        statement = statement.where(Generation.status.in_(statuses))
    if not allow_cancel_requested:
        statement = statement.where(Generation.cancel_requested_at.is_(None))
    return statement


def ensure_generation_updated(
    result: Any,
    task_id: str,
    attempt_epoch: int | None,
) -> None:
    rowcount = getattr(result, "rowcount", None)
    if rowcount == 0:
        raise StaleGenerationAttempt(
            f"generation {task_id} attempt {attempt_epoch} no longer owns row"
        )


async def ensure_generation_attempt_current(
    session: Any,
    task_id: str,
    attempt_epoch: int,
    *,
    execution_epoch: int | None = None,
) -> None:
    current = (
        await session.execute(
            select(Generation.attempt, Generation.execution_epoch)
            .where(Generation.id == task_id)
            .with_for_update()
        )
    ).one_or_none()
    expected_execution_epoch = _expected_execution_epoch(task_id, execution_epoch)
    current_attempt = current[0] if current is not None else None
    current_execution_epoch = current[1] if current is not None else None
    if current_attempt != attempt_epoch or (
        expected_execution_epoch is not None
        and current_execution_epoch != expected_execution_epoch
    ):
        raise StaleGenerationAttempt(
            f"generation {task_id} execution moved from "
            f"epoch={expected_execution_epoch} attempt={attempt_epoch} to "
            f"epoch={current_execution_epoch} attempt={current_attempt}"
        )


async def ensure_generation_execution_current(
    session: Any,
    task_id: str,
    execution_epoch: int,
) -> None:
    current_epoch = (
        await session.execute(
            select(Generation.execution_epoch)
            .where(Generation.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current_epoch != max(0, int(execution_epoch)):
        raise StaleGenerationAttempt(
            f"generation {task_id} execution epoch moved from "
            f"{execution_epoch} to {current_epoch}"
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
    services: RunGenerationDeps,
) -> bool:
    failure_delivery = None
    try:
        async with services.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=statuses,
                ).values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            ensure_generation_updated(result, task_id, attempt)
            message = await session.get(Message, message_id)
            if message is not None and message.status != MessageStatus.CANCELED:
                message.status = MessageStatus.FAILED
            if not retriable:
                generation = await session.get(Generation, task_id)
                if generation is not None:
                    await release_or_settle_generation(
                        services.billing,
                        session,
                        generation,
                        reason=error_code,
                    )
            failure_delivery = stage_generation_event(
                session,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "execution_epoch": current_generation_execution_epoch(task_id),
                    "code": error_code,
                    "message": error_message,
                    "retriable": retriable,
                },
            )
            await session.commit()
            await services.billing.flush_after_commit(session)
    except StaleGenerationAttempt as stale_exc:
        logger.info(
            "generation failed update skipped by stale attempt "
            "task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    if failure_delivery is None:
        raise RuntimeError("generation failure outbox event was not staged")
    await services.events.deliver(redis, failure_delivery)
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
    replace_dispatch: DispatchIdentity | None = None,
    services: RunGenerationDeps,
) -> bool:
    try:
        async with services.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=RUNNING_GENERATION_STATUSES,
                ).values(
                    status=GenerationStatus.QUEUED.value,
                    progress_stage=GenerationStage.QUEUED,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            ensure_generation_updated(result, task_id, attempt)
            await session.commit()
    except StaleGenerationAttempt as stale_exc:
        logger.info(
            "generation retry update skipped by stale attempt "
            "task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    try:
        await redis.set(
            image_queue_not_before_key(task_id),
            str(time.time() + delay),
            ex=retry_not_before_ttl(delay),
        )
        enqueued = await enqueue_generation_once(
            redis,
            task_id,
            attempt=attempt + 1,
            defer_by=delay,
            job_try=attempt + 1,
            replace_dispatch=replace_dispatch,
            services=services,
        )
        if not enqueued:
            return False
    except Exception as enqueue_exc:  # noqa: BLE001
        logger.error("re-enqueue failed task=%s err=%s", task_id, enqueue_exc)
        enqueue_error = "retry_enqueue_failed"
        enqueue_message = f"failed to enqueue retry: {enqueue_exc}"
        await mark_generation_attempt_failed(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            error_code=enqueue_error,
            error_message=enqueue_message[:2000],
            retriable=False,
            statuses=(
                GenerationStatus.QUEUED.value,
                GenerationStatus.RUNNING.value,
            ),
            services=services,
        )
        return False

    await services.events.publish(
        redis,
        user_id,
        task_channel(task_id),
        EV_GEN_RETRYING,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "execution_epoch": current_generation_execution_epoch(task_id),
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
    replace_dispatch: DispatchIdentity | None = None,
    services: RunGenerationDeps,
) -> bool:
    if attempt <= 0:
        return False
    try:
        async with services.store.session() as session:
            row = (
                await session.execute(
                    select(
                        Generation.status,
                        Generation.message_id,
                        Generation.user_id,
                    )
                    .where(
                        Generation.id == task_id,
                        Generation.attempt == attempt,
                        Generation.status == GenerationStatus.QUEUED.value,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            expected_execution_epoch = current_generation_execution_epoch(task_id)
            if (
                row is not None
                and expected_execution_epoch is not None
                and (
                    await session.execute(
                        select(Generation.execution_epoch).where(
                            Generation.id == task_id
                        )
                    )
                ).scalar_one_or_none()
                != expected_execution_epoch
            ):
                return False
            if row is None:
                return False
            _status, message_id, user_id = row
            await session.rollback()
    except StaleGenerationAttempt as stale_exc:
        logger.info(
            "stale attempt requeue skipped task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stale attempt requeue check failed task=%s attempt=%s err=%s",
            task_id,
            attempt,
            exc,
        )
        return False

    try:
        await redis.set(
            image_queue_not_before_key(task_id),
            str(time.time() + delay),
            ex=retry_not_before_ttl(delay),
        )
        enqueued = await enqueue_generation_once(
            redis,
            task_id,
            attempt=attempt + 1,
            defer_by=delay,
            job_try=attempt + 1,
            replace_dispatch=replace_dispatch,
            services=services,
        )
        if not enqueued:
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stale attempt re-enqueue failed task=%s attempt=%s err=%s",
            task_id,
            attempt,
            exc,
        )
        return False

    await services.events.publish(
        redis,
        str(user_id),
        task_channel(task_id),
        EV_GEN_RETRYING,
        {
            "generation_id": task_id,
            "message_id": str(message_id),
            "attempt": attempt,
            "max_attempts": MAX_ATTEMPTS,
            "retry_delay_seconds": delay,
            "error_code": "stale_attempt_requeued",
            "error_message": f"stale attempt requeued: {reason}"[:2000],
            "reason": reason,
        },
    )
    return True


async def await_with_lease_guard(
    awaitable: Awaitable[T],
    lease_lost: asyncio.Event,
    *,
    redis: Any | None = None,
    task_id: str | None = None,
    cancel_poll_interval_s: float = 1.0,
) -> T:
    if lease_lost.is_set():
        raise LeaseLost("generation lease renewer failed")

    async def wait_cancelled() -> None:
        assert redis is not None
        assert task_id is not None
        interval_s = max(0.05, float(cancel_poll_interval_s))
        while True:
            if await is_cancelled(redis, task_id):
                return
            await asyncio.sleep(interval_s)

    work_task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
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
            raise LeaseLost("generation lease renewer failed")
        if cancel_task is not None and cancel_task in done:
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise TaskCancelled("cancelled during upstream call")
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
    image_iter: AsyncIterator[GeneratedImageResult] | None,
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
        logger.debug(
            "generation image iterator aclose failed task=%s",
            task_id,
            exc_info=True,
        )


async def anext_image_with_guards(
    image_iter: AsyncIterator[GeneratedImageResult],
    lease_lost: asyncio.Event,
    *,
    redis: Any,
    task_id: str,
) -> GeneratedImageResult | None:
    try:
        return await await_with_lease_guard(
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
    if isinstance(exc, StorageDiskFullError):
        return is_retriable(
            EC.DISK_FULL.value,
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, TimeoutError):
        return is_retriable(
            "timeout",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, UpstreamError):
        return is_retriable(
            exc.error_code,
            exc.status_code,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return is_retriable(
            "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, httpx.HTTPError):
        return is_retriable(
            "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    return RetryDecision(False, f"unhandled {type(exc).__name__}")


def safe_generation_error_details(exc: BaseException) -> dict[str, Any]:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return {}
    details: dict[str, Any] = {}
    transparent_qc = payload.get("transparent_qc")
    if isinstance(transparent_qc, dict):
        sanitized_qc = sanitize_transparent_qc_payload(transparent_qc)
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
    if not is_moderation_block(err_code, err_msg):
        return None
    if is_dual_race or not reserved_provider_name:
        return None
    if enabled_provider_count <= 1:
        return None
    if enabled_provider_count - already_avoided_count <= 1:
        return None
    if already_avoided_count + 1 >= min(cap, enabled_provider_count):
        return None
    return RetryDecision(
        retriable=True,
        reason="moderation_blocked try_next_provider",
    )
