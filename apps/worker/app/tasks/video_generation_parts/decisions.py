from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ...task_runtime import (
    BillingAction,
    Effect,
    EffectBatch,
    EffectKind,
    IdempotencyToken,
    QueueAction,
    RetrySchedule,
    TransitionDecision,
)


class VideoDomainState(StrEnum):
    QUEUED = "queued"
    SUBMIT_UNKNOWN = "submit_unknown"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class VideoPollOutcome(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    RETRYABLE_ERROR = "retryable_error"


@dataclass(frozen=True, slots=True)
class VideoPollPolicy:
    interval_s: int
    max_count: int
    max_duration_s: int


def video_poll_window_exhausted(
    *,
    submitted_at: datetime | None,
    poll_count: int,
    now: datetime,
    policy: VideoPollPolicy,
) -> bool:
    if poll_count >= policy.max_count:
        return True
    return submitted_at is not None and (
        submitted_at + timedelta(seconds=policy.max_duration_s) <= now
    )


def decide_video_submission_failure(
    *,
    task_id: str,
    attempt: int,
    outcome_unknown: bool,
    retryable: bool,
    retry_delay_s: int,
    max_attempts: int,
) -> TransitionDecision:
    def token(operation: str) -> IdempotencyToken:
        return IdempotencyToken(task_id, operation, attempt)

    if outcome_unknown:
        return TransitionDecision(
            next_state=VideoDomainState.SUBMIT_UNKNOWN.value,
            queue_action=QueueAction.DEFER,
            effects=EffectBatch(
                database=(
                    Effect(
                        EffectKind.DATABASE,
                        "mark_submit_unknown",
                        token=token("submit_unknown"),
                    ),
                ),
            ),
            reason="provider_outcome_unknown",
        )
    if retryable and attempt < max_attempts:
        return TransitionDecision(
            next_state=VideoDomainState.QUEUED.value,
            queue_action=QueueAction.ENQUEUE,
            retry=RetrySchedule(
                delay_s=max(0, retry_delay_s),
                reason="submit_retryable",
                max_attempts=max_attempts,
            ),
            effects=EffectBatch(
                database=(
                    Effect(
                        EffectKind.DATABASE,
                        "mark_submit_retry",
                        token=token("submit_retry"),
                    ),
                ),
                queue=(
                    Effect(
                        EffectKind.QUEUE,
                        "enqueue_video_submit",
                        token=token("submit_enqueue"),
                    ),
                ),
            ),
        )
    return _terminal_video_decision(
        task_id=task_id,
        attempt=attempt,
        state=VideoDomainState.FAILED,
        billing=BillingAction.RELEASE,
    )


def decide_video_poll(
    *,
    task_id: str,
    attempt: int,
    outcome: VideoPollOutcome,
    retry_delay_s: int = 0,
) -> TransitionDecision:
    if outcome in {VideoPollOutcome.RUNNING, VideoPollOutcome.RETRYABLE_ERROR}:
        name = (
            "enqueue_video_poll"
            if outcome is VideoPollOutcome.RUNNING
            else "enqueue_video_poll_retry"
        )
        return TransitionDecision(
            next_state=VideoDomainState.RUNNING.value,
            queue_action=QueueAction.ENQUEUE,
            retry=(
                RetrySchedule(
                    delay_s=max(0, retry_delay_s),
                    reason="poll_retryable",
                    max_attempts=0,
                )
                if outcome is VideoPollOutcome.RETRYABLE_ERROR
                else None
            ),
            effects=EffectBatch(
                queue=(
                    Effect(
                        EffectKind.QUEUE,
                        name,
                        token=IdempotencyToken(task_id, name, attempt),
                    ),
                ),
            ),
        )
    state = {
        VideoPollOutcome.SUCCEEDED: VideoDomainState.SUCCEEDED,
        VideoPollOutcome.FAILED: VideoDomainState.FAILED,
        VideoPollOutcome.CANCELLED: VideoDomainState.CANCELLED,
        VideoPollOutcome.EXPIRED: VideoDomainState.EXPIRED,
    }[outcome]
    return _terminal_video_decision(
        task_id=task_id,
        attempt=attempt,
        state=state,
        billing=(
            BillingAction.SETTLE
            if state is VideoDomainState.SUCCEEDED
            else BillingAction.RELEASE
        ),
    )


def _terminal_video_decision(
    *,
    task_id: str,
    attempt: int,
    state: VideoDomainState,
    billing: BillingAction,
) -> TransitionDecision:
    def token(operation: str) -> IdempotencyToken:
        return IdempotencyToken(task_id, operation, attempt)

    event_name = f"video_{state.value}"
    return TransitionDecision(
        next_state=state.value,
        billing_action=billing,
        queue_action=QueueAction.RELEASE,
        effects=EffectBatch(
            database=(
                Effect(
                    EffectKind.DATABASE,
                    f"mark_{state.value}",
                    token=token("terminal_state"),
                ),
            ),
            billing=(
                Effect(
                    EffectKind.BILLING,
                    billing.value,
                    token=token("billing"),
                ),
            ),
            events=(
                Effect(
                    EffectKind.EVENT_STAGE,
                    event_name,
                    token=token("terminal_event"),
                ),
                Effect(
                    EffectKind.COMMIT,
                    "commit_terminal",
                    token=token("terminal_commit"),
                ),
                Effect(
                    EffectKind.EVENT_DELIVERY,
                    f"deliver_{event_name}",
                    token=token("terminal_delivery"),
                ),
            ),
            queue=(
                Effect(
                    EffectKind.QUEUE,
                    "release_provider_slot",
                    token=token("provider_slot_release"),
                ),
            ),
            lease=(
                Effect(
                    EffectKind.LEASE,
                    "release_task_lease",
                    token=token("lease_release"),
                ),
            ),
        ),
    )
