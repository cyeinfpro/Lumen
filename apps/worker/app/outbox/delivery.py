"""Outbox side-effect delivery and committed-row fast path."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from lumen_core.arq_jobs import arq_job_id

from ..generation_dispatch import enqueue_generation_dispatch
from .contracts import (
    OUTBOX_ENQUEUE_DEDUPE_PREFIX,
    OUTBOX_ENQUEUE_DEDUPE_TTL_S,
    OUTBOX_TASK_JOBS,
    OutboxPayloadError,
    PendingOutboxDelivery,
)
from .metrics import outbox_events_total
from .retry import clear_fail_count
from .staging import mark_staged_outbox_published

logger = logging.getLogger(__name__)
EventPublisher = Callable[..., Awaitable[Any]]


async def deliver_outbox_event(
    redis: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    event_publisher: EventPublisher,
    log: logging.Logger = logger,
) -> tuple[str, str, bool]:
    dedupe_key = f"{OUTBOX_ENQUEUE_DEDUPE_PREFIX}{event_id}"
    marker = str(payload.get("task_id") or payload.get("user_id") or event_id)
    existing_delivery = await redis.get(dedupe_key)
    if existing_delivery:
        log.info(
            "outbox delivery deduped event=%s marker=%s kind=%s",
            event_id,
            marker,
            kind,
        )
        outbox_events_total.labels(kind=kind, outcome="deduped").inc()
        return dedupe_key, marker, False

    if kind == "sse":
        user_id = payload.get("user_id")
        channel = payload.get("channel")
        event_name = payload.get("event_name")
        data = payload.get("data")
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(channel, str)
            or not channel
            or not isinstance(event_name, str)
            or not event_name
            or not isinstance(data, dict)
        ):
            raise OutboxPayloadError("invalid SSE payload")
        event_data = dict(data)
        event_data.setdefault("outbox_id", event_id)
        event_data.setdefault("event_id", event_id)
        await event_publisher(redis, user_id, channel, event_name, event_data)
        return dedupe_key, user_id, True

    if kind == "memory_extract":
        conversation_id = payload.get("conversation_id")
        source_message_id = payload.get("source_user_message_id")
        assistant_message_id = payload.get("assistant_message_id")
        task_id = payload.get("task_id")
        memory_event_id = payload.get("event_id")
        expected_event_id = f"memory-extract:{source_message_id}:{assistant_message_id}"
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(source_message_id, str)
            or not source_message_id
            or not isinstance(assistant_message_id, str)
            or not assistant_message_id
            or task_id != assistant_message_id
            or memory_event_id != expected_event_id
        ):
            raise OutboxPayloadError("invalid memory_extract payload")
        await redis.enqueue_job(
            OUTBOX_TASK_JOBS[kind],
            conversation_id,
            source_message_id,
            assistant_message_id,
            _job_id=arq_job_id(kind, assistant_message_id, event_id),
        )
        return dedupe_key, assistant_message_id, True

    job_name = OUTBOX_TASK_JOBS.get(kind)
    task_id = payload.get("task_id") or payload.get("id")
    if job_name is None or not task_id:
        raise OutboxPayloadError("invalid task payload")
    enqueue_kwargs: dict[str, Any] = {}
    defer_s = payload.get("defer_s")
    if isinstance(defer_s, (int, float)) and defer_s > 0:
        enqueue_kwargs["_defer_by"] = float(defer_s)
    if kind == "generation":
        result = await enqueue_generation_dispatch(
            redis,
            task_id=str(task_id),
            attempt=1,
            defer_by=enqueue_kwargs.get("_defer_by"),
            job_try=1,
        )
        if not result.accepted:
            raise RuntimeError(
                "generation dispatch lacks durable enqueue evidence "
                f"task={task_id} attempt={result.identity.attempt} "
                f"revision={result.identity.revision}"
            )
        return dedupe_key, str(task_id), True
    enqueue_kwargs["_job_id"] = arq_job_id(
        kind,
        str(task_id),
        str(payload.get("outbox_id") or event_id),
    )
    job_try = payload.get("job_try")
    if (
        kind == "completion"
        and isinstance(job_try, int)
        and not isinstance(job_try, bool)
        and job_try > 0
    ):
        enqueue_kwargs["_job_try"] = job_try
    await redis.enqueue_job(job_name, task_id, **enqueue_kwargs)
    return dedupe_key, str(task_id), True


async def deliver_staged_outbox_events(
    redis: Any,
    deliveries: list[PendingOutboxDelivery],
    *,
    session_factory: Any,
    event_publisher: EventPublisher,
    log: logging.Logger = logger,
) -> None:
    """Best-effort fast path; any failure leaves the durable row recoverable."""
    for event_id, kind, payload in deliveries:
        try:
            dedupe_key, marker, should_set_dedupe = await deliver_outbox_event(
                redis,
                event_id=event_id,
                kind=kind,
                payload=payload,
                event_publisher=event_publisher,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            outbox_events_total.labels(kind=kind, outcome="fast_path_failed").inc()
            log.warning(
                "post-commit outbox delivery failed event=%s kind=%s err=%s",
                event_id,
                kind,
                exc,
            )
            continue

        try:
            committed = await mark_staged_outbox_published(
                session_factory,
                event_id,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            outbox_events_total.labels(kind=kind, outcome="finalize_failed").inc()
            log.warning(
                "post-commit outbox finalize failed event=%s kind=%s err=%s",
                event_id,
                kind,
                exc,
            )
            continue
        if not committed:
            continue

        if should_set_dedupe:
            try:
                await redis.set(
                    dedupe_key,
                    marker,
                    ex=OUTBOX_ENQUEUE_DEDUPE_TTL_S,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "post-commit outbox dedupe write failed key=%s err=%s",
                    dedupe_key,
                    exc,
                )
        await clear_fail_count(redis, event_id, log=log)
        outbox_events_total.labels(kind=kind, outcome="fast_path_published").inc()
