"""Transactional outbox publishing primitives and compatibility exports."""

from .contracts import (
    FAILURE_MATRIX,
    OUTBOX_BATCH,
    OUTBOX_CLAIM_TTL_S,
    OUTBOX_DELIVERY_CONCURRENCY,
    OUTBOX_DELIVERY_TIMEOUT_S,
    OUTBOX_DLQ_KEY,
    OUTBOX_DLQ_MAXLEN,
    OUTBOX_ENQUEUE_DEDUPE_PREFIX,
    OUTBOX_ENQUEUE_DEDUPE_TTL_S,
    OUTBOX_FAIL_COUNT_HASH,
    OUTBOX_FAIL_COUNT_TTL_S,
    OUTBOX_LOCK_KEY,
    OUTBOX_LOCK_TTL_S,
    OUTBOX_MAX_FAIL_COUNT,
    OUTBOX_TASK_JOBS,
    OutboxPayloadError,
    PendingOutboxDelivery,
)
from .claims import claim_outbox_events, finalize_outbox_results
from .delivery import deliver_outbox_event, deliver_staged_outbox_events
from .dlq import mirror, persist, persist_once, resolve
from .publisher import process_outbox_batch
from .retry import INCR_FAIL_COUNT_LUA, increment_fail_count
from .staging import mark_staged_outbox_published, stage_outbox_event

__all__ = (
    "FAILURE_MATRIX",
    "INCR_FAIL_COUNT_LUA",
    "OUTBOX_BATCH",
    "OUTBOX_CLAIM_TTL_S",
    "OUTBOX_DELIVERY_CONCURRENCY",
    "OUTBOX_DELIVERY_TIMEOUT_S",
    "OUTBOX_DLQ_KEY",
    "OUTBOX_DLQ_MAXLEN",
    "OUTBOX_ENQUEUE_DEDUPE_PREFIX",
    "OUTBOX_ENQUEUE_DEDUPE_TTL_S",
    "OUTBOX_FAIL_COUNT_HASH",
    "OUTBOX_FAIL_COUNT_TTL_S",
    "OUTBOX_LOCK_KEY",
    "OUTBOX_LOCK_TTL_S",
    "OUTBOX_MAX_FAIL_COUNT",
    "OUTBOX_TASK_JOBS",
    "OutboxPayloadError",
    "PendingOutboxDelivery",
    "claim_outbox_events",
    "deliver_outbox_event",
    "deliver_staged_outbox_events",
    "increment_fail_count",
    "finalize_outbox_results",
    "mark_staged_outbox_published",
    "mirror",
    "persist",
    "persist_once",
    "process_outbox_batch",
    "resolve",
    "stage_outbox_event",
)
