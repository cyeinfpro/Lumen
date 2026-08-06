"""Frozen outbox invariants and failure policy.

The matrix is executable documentation for Workstream D. New publisher paths
must classify failures here before changing persistence behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeAlias
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    failure: str
    published: bool
    persist_dlq: bool
    retry: bool
    invariant: str


FAILURE_MATRIX = (
    FailurePolicy(
        failure="delivery_succeeded",
        published=True,
        persist_dlq=False,
        retry=False,
        invariant="published_at changes only after the side effect is accepted",
    ),
    FailurePolicy(
        failure="delivery_already_deduped",
        published=True,
        persist_dlq=False,
        retry=False,
        invariant="a durable dedupe marker proves the prior side effect",
    ),
    FailurePolicy(
        failure="transient_delivery_error",
        published=False,
        persist_dlq=True,
        retry=True,
        invariant=(
            "PostgreSQL delivery_attempts drives retry and the durable DLQ threshold"
        ),
    ),
    FailurePolicy(
        failure="invalid_payload",
        published=True,
        persist_dlq=True,
        retry=False,
        invariant="poison events are quarantined in the parent locking transaction",
    ),
    FailurePolicy(
        failure="database_commit_error_after_delivery",
        published=False,
        persist_dlq=False,
        retry=True,
        invariant="stable job ids absorb replay; Redis dedupe is written post-commit",
    ),
    FailurePolicy(
        failure="post_commit_diagnostic_error",
        published=True,
        persist_dlq=False,
        retry=False,
        invariant="best-effort diagnostics never roll back durable publication",
    ),
)

OutboxKind: TypeAlias = Literal[
    "completion",
    "generation",
    "memory_extract",
    "sse",
    "storyboard_assembly",
    "video_generation",
]
PendingOutboxDelivery: TypeAlias = tuple[str, str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    id: str
    kind: str
    payload: object
    delivery_attempts: int
    claim_until: datetime


OUTBOX_LOCK_KEY = "lock:outbox:publisher"
OUTBOX_LOCK_TTL_S = 10
OUTBOX_BATCH = 100
OUTBOX_CLAIM_TTL_S = 300
OUTBOX_DELIVERY_CONCURRENCY = 8
OUTBOX_DELIVERY_TIMEOUT_S = 15.0
OUTBOX_DLQ_KEY = "outbox:dead-letter"
OUTBOX_DLQ_MAXLEN = 1000
OUTBOX_MAX_FAIL_COUNT = 5
OUTBOX_FAIL_COUNT_HASH = "outbox:fail_count"
OUTBOX_FAIL_COUNT_TTL_S = 24 * 3600
OUTBOX_RETRY_BASE_DELAY_S = 5
OUTBOX_RETRY_MAX_DELAY_S = 15 * 60
OUTBOX_DLQ_RETRY_DELAY_S = 5 * 60
OUTBOX_ENQUEUE_DEDUPE_PREFIX = "outbox:enqueued:"
OUTBOX_ENQUEUE_DEDUPE_TTL_S = 24 * 3600
OUTBOX_TASK_JOBS = MappingProxyType(
    {
        "generation": "run_generation",
        "completion": "run_completion",
        "memory_extract": "memory_extract",
        "video_generation": "run_video_generation",
        "storyboard_assembly": "run_storyboard_assembly",
    }
)


class OutboxPayloadError(ValueError):
    """The event is poison data and cannot succeed without data repair."""
