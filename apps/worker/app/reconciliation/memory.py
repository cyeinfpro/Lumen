"""Memory extraction reconciliation domain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select

from lumen_core.constants import MessageStatus, Role
from lumen_core.models import (
    Conversation,
    MemoryExtractionRun,
    Message,
    User,
)

from .contracts import ReconcileContext, ReconcileResult
from .metrics import reconciliation_rows_total

MEMORY_RECON_BATCH = 100
MEMORY_RETRY_BACKOFF_BASE_S = 30
MEMORY_RETRY_BACKOFF_MAX_S = 15 * 60


def memory_retry_backoff_seconds(attempt: int | None) -> int:
    normalized_attempt = max(1, int(attempt or 1))
    exponent = min(normalized_attempt - 1, 8)
    return min(
        MEMORY_RETRY_BACKOFF_MAX_S,
        MEMORY_RETRY_BACKOFF_BASE_S * (2**exponent),
    )


def aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def memory_run_due(run: MemoryExtractionRun, now: datetime) -> bool:
    status = str(run.status or "")
    if status not in {"retryable", "running"}:
        return False
    lease_expires_at = aware_utc(run.lease_expires_at)
    if lease_expires_at is not None and lease_expires_at > now:
        return False
    if status == "running":
        return True
    updated_at = aware_utc(run.updated_at) or aware_utc(run.created_at)
    if updated_at is None:
        return True
    return (
        updated_at + timedelta(seconds=memory_retry_backoff_seconds(run.attempt)) <= now
    )


def memory_run_due_predicate(now: datetime) -> Any:
    model = MemoryExtractionRun
    updated_at = func.coalesce(model.updated_at, model.created_at)
    retry_due = or_(
        and_(model.attempt <= 1, updated_at <= now - timedelta(seconds=30)),
        and_(model.attempt == 2, updated_at <= now - timedelta(seconds=60)),
        and_(model.attempt == 3, updated_at <= now - timedelta(seconds=120)),
        and_(model.attempt == 4, updated_at <= now - timedelta(seconds=240)),
        and_(model.attempt == 5, updated_at <= now - timedelta(seconds=480)),
        and_(
            model.attempt >= 6,
            updated_at <= now - timedelta(seconds=MEMORY_RETRY_BACKOFF_MAX_S),
        ),
    )
    return or_(
        model.status == "running",
        and_(model.status == "retryable", retry_due),
    )


async def memory_run_invalid_reason(
    session: Any,
    run: MemoryExtractionRun,
) -> str | None:
    user = await session.get(User, run.user_id)
    conversation = await session.get(Conversation, run.conversation_id)
    source_message = await session.get(Message, run.source_message_id)
    assistant_message = await session.get(Message, run.assistant_message_id)
    expected_event_id = (
        f"memory-extract:{run.source_message_id}:{run.assistant_message_id}"
    )
    if run.event_id != expected_event_id:
        return "event_id_mismatch"
    if user is None or getattr(user, "deleted_at", None) is not None:
        return "user_deleted"
    if bool(user.memory_disabled) or bool(user.memory_paused):
        return "memory_disabled"
    if (
        conversation is None
        or conversation.user_id != run.user_id
        or getattr(conversation, "deleted_at", None) is not None
    ):
        return "conversation_deleted"
    if bool(conversation.memory_disabled):
        return "memory_disabled"
    if (
        source_message is None
        or source_message.conversation_id != run.conversation_id
        or source_message.role != Role.USER.value
        or getattr(source_message, "deleted_at", None) is not None
    ):
        return "source_message_deleted"
    if (
        assistant_message is None
        or assistant_message.conversation_id != run.conversation_id
        or assistant_message.role != Role.ASSISTANT.value
        or assistant_message.parent_message_id != run.source_message_id
        or getattr(assistant_message, "deleted_at", None) is not None
    ):
        return "assistant_message_deleted"
    if assistant_message.status in {MessageStatus.CANCELED.value, "cancelled"}:
        return "assistant_message_canceled"
    return None


def cancel_memory_run(
    run: MemoryExtractionRun,
    *,
    reason: str,
    now: datetime,
) -> bool:
    if str(run.status or "") not in {"retryable", "running"}:
        return False
    run.status = "canceled"
    run.owner = None
    run.job_id = None
    run.fence = max(0, int(run.fence or 0)) + 1
    run.claimed_at = None
    run.lease_expires_at = None
    run.canceled_at = now
    run.cancel_reason = reason[:160]
    run.retry_reason = None
    run.updated_at = now
    return True


def requeue_memory_run(run: MemoryExtractionRun, *, now: datetime) -> bool:
    if not memory_run_due(run, now):
        return False
    run.status = "pending"
    run.owner = None
    run.job_id = None
    run.fence = max(0, int(run.fence or 0)) + 1
    run.recovery_count = max(0, int(run.recovery_count or 0)) + 1
    run.claimed_at = None
    run.lease_expires_at = None
    run.canceled_at = None
    run.cancel_reason = None
    run.retry_reason = None
    run.updated_at = now
    return True


class MemoryExtractionReconciler:
    name = "memory_extraction"

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        rows = list(
            (
                await context.session.execute(
                    select(MemoryExtractionRun)
                    .where(
                        MemoryExtractionRun.status.in_(("retryable", "running")),
                        memory_run_due_predicate(context.now),
                        or_(
                            MemoryExtractionRun.lease_expires_at.is_(None),
                            MemoryExtractionRun.lease_expires_at <= context.now,
                        ),
                    )
                    .order_by(
                        MemoryExtractionRun.updated_at,
                        MemoryExtractionRun.id,
                    )
                    .limit(MEMORY_RECON_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        result = ReconcileResult()
        for run in rows:
            if not memory_run_due(run, context.now):
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="not_due",
                ).inc()
                continue
            invalid_reason = await memory_run_invalid_reason(context.session, run)
            if invalid_reason is not None:
                if cancel_memory_run(run, reason=invalid_reason, now=context.now):
                    result.touched += 1
                    reconciliation_rows_total.labels(
                        domain=self.name,
                        action="canceled",
                    ).inc()
                continue

            previous_status = str(run.status)
            if not requeue_memory_run(run, now=context.now):
                continue
            result.pending_outbox.append(
                context.stage_event(
                    context.session,
                    kind="memory_extract",
                    payload={
                        "task_id": run.assistant_message_id,
                        "event_id": run.event_id,
                        "user_id": run.user_id,
                        "conversation_id": run.conversation_id,
                        "source_user_message_id": run.source_message_id,
                        "assistant_message_id": run.assistant_message_id,
                        "kind": "memory_extract",
                        "source": "memory_extraction_reconciler",
                        "recovered_from": previous_status,
                        "attempt": int(run.attempt or 0),
                        "fence": int(run.fence or 0),
                    },
                )
            )
            result.touched += 1
            reconciliation_rows_total.labels(
                domain=self.name,
                action="requeued",
            ).inc()
        return result


MEMORY_RECONCILER = MemoryExtractionReconciler()
