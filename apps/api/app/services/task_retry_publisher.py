"""Best-effort queue handoff for retried tasks."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import EV_COMP_QUEUED, EV_GEN_QUEUED, task_channel


async def publish_queued_retry(
    payload: dict[str, Any],
    message_id: str,
    *,
    get_redis: Callable[[], Any],
    get_arq_pool: Callable[[], Awaitable[Any]],
    publish_sse_event: Callable[..., Awaitable[None]],
    publish_error_counter: Any,
    logger: logging.Logger,
) -> None:
    """Publish the non-authoritative retry wakeup after the outbox commit."""
    try:
        redis = get_redis()
        kind = payload["kind"]
        fn_name = "run_completion" if kind == "completion" else "run_generation"
        ev_name = EV_COMP_QUEUED if kind == "completion" else EV_GEN_QUEUED
        id_field = "completion_id" if kind == "completion" else "generation_id"
        pool = await get_arq_pool()
        await pool.enqueue_job(
            fn_name,
            payload["task_id"],
            _job_id=arq_job_id(kind, payload["task_id"], payload.get("outbox_id")),
        )
        await publish_sse_event(
            redis,
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=ev_name,
            data={
                id_field: payload["task_id"],
                "message_id": message_id,
                "kind": kind,
                "execution_epoch": payload.get("execution_epoch", 0),
                "stage": "queued",
                "substage": "waiting_queue",
                "retrying": False,
                "waiting_provider": False,
                "cancelled": False,
            },
        )
    except Exception:
        kind = str(payload.get("kind") or "unknown")
        publish_error_counter.labels(kind=kind).inc()
        logger.warning(
            "best-effort queued task publish failed kind=%s task_id=%s",
            kind,
            payload.get("task_id"),
            exc_info=True,
        )


__all__ = ["publish_queued_retry"]
