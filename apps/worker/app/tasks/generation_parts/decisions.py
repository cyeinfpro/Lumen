from __future__ import annotations

from enum import StrEnum

from ...task_runtime import (
    BillingAction,
    Effect,
    EffectBatch,
    EffectKind,
    IdempotencyToken,
    LeaseState,
    QueueAction,
    RetrySchedule,
    TransitionDecision,
    lease_allows_mutation,
)
from ...task_runtime.transitions import require_transition


class GenerationDomainState(StrEnum):
    QUEUED = "queued"
    CLAIMING = "claiming"
    RUNNING = "running"
    PERSISTING = "persisting"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


def _token(task_id: str, attempt: int, operation: str) -> IdempotencyToken:
    return IdempotencyToken(task_id, operation, attempt)


def decide_claim(
    *,
    task_id: str,
    attempt: int,
    current: GenerationDomainState,
    lease: LeaseState,
) -> TransitionDecision:
    if not lease_allows_mutation(lease):
        return TransitionDecision(
            next_state=current.value,
            queue_action=QueueAction.DEFER,
            reason=f"lease_{lease.value}",
        )
    target = require_transition(
        current.value,
        frozenset({GenerationDomainState.QUEUED.value}),
        GenerationDomainState.CLAIMING.value,
    )
    return TransitionDecision(
        next_state=target,
        effects=EffectBatch(
            database=(
                Effect(
                    EffectKind.DATABASE,
                    "claim_generation",
                    token=_token(task_id, attempt, "claim"),
                ),
            ),
        ),
    )


def decide_upstream_outcome(
    *,
    task_id: str,
    attempt: int,
    current: GenerationDomainState,
    outcome: GenerationOutcome,
    retry_delay_s: float = 0,
    max_attempts: int = 0,
) -> TransitionDecision:
    require_transition(
        current.value,
        frozenset(
            {
                GenerationDomainState.CLAIMING.value,
                GenerationDomainState.RUNNING.value,
                GenerationDomainState.PERSISTING.value,
            }
        ),
        outcome.value,
    )
    if outcome is GenerationOutcome.SUCCEEDED:
        return TransitionDecision(
            next_state=GenerationDomainState.PERSISTING.value,
            effects=EffectBatch(
                database=(
                    Effect(
                        EffectKind.DATABASE,
                        "persist_generated_artifacts",
                        token=_token(task_id, attempt, "persist"),
                    ),
                ),
            ),
        )
    if outcome is GenerationOutcome.RETRYABLE_FAILURE:
        return TransitionDecision(
            next_state=GenerationDomainState.RETRY_WAIT.value,
            queue_action=QueueAction.ENQUEUE,
            retry=RetrySchedule(
                delay_s=max(0, retry_delay_s),
                reason="upstream_retryable",
                max_attempts=max_attempts,
            ),
            effects=EffectBatch(
                database=(
                    Effect(
                        EffectKind.DATABASE,
                        "mark_generation_retry",
                        token=_token(task_id, attempt, "retry"),
                    ),
                ),
                queue=(
                    Effect(
                        EffectKind.QUEUE,
                        "enqueue_generation_retry",
                        token=_token(task_id, attempt, "retry_enqueue"),
                    ),
                ),
            ),
        )
    if outcome is GenerationOutcome.CANCELLED:
        return decide_cancel(task_id=task_id, attempt=attempt, current=current)
    if outcome is GenerationOutcome.STALE:
        return TransitionDecision(
            next_state=current.value,
            queue_action=QueueAction.DEFER,
            reason="stale_attempt",
        )
    return decide_finalize(
        task_id=task_id,
        attempt=attempt,
        current=current,
        outcome=GenerationOutcome.FAILED,
    )


def decide_retry(
    *,
    task_id: str,
    attempt: int,
    current: GenerationDomainState,
    delay_s: float,
    max_attempts: int,
) -> TransitionDecision:
    return decide_upstream_outcome(
        task_id=task_id,
        attempt=attempt,
        current=current,
        outcome=GenerationOutcome.RETRYABLE_FAILURE,
        retry_delay_s=delay_s,
        max_attempts=max_attempts,
    )


def decide_cancel(
    *,
    task_id: str,
    attempt: int,
    current: GenerationDomainState,
) -> TransitionDecision:
    require_transition(
        current.value,
        frozenset(
            state.value
            for state in GenerationDomainState
            if state
            not in {
                GenerationDomainState.SUCCEEDED,
                GenerationDomainState.FAILED,
                GenerationDomainState.CANCELLED,
            }
        ),
        GenerationDomainState.CANCELLED.value,
    )
    return _terminal_decision(
        task_id=task_id,
        attempt=attempt,
        state=GenerationDomainState.CANCELLED,
        billing=BillingAction.RELEASE,
        event_name="generation_cancelled",
    )


def decide_finalize(
    *,
    task_id: str,
    attempt: int,
    current: GenerationDomainState,
    outcome: GenerationOutcome,
) -> TransitionDecision:
    require_transition(
        current.value,
        frozenset(
            {
                GenerationDomainState.CLAIMING.value,
                GenerationDomainState.RUNNING.value,
                GenerationDomainState.PERSISTING.value,
            }
        ),
        outcome.value,
    )
    if outcome is GenerationOutcome.SUCCEEDED:
        return _terminal_decision(
            task_id=task_id,
            attempt=attempt,
            state=GenerationDomainState.SUCCEEDED,
            billing=BillingAction.SETTLE,
            event_name="generation_succeeded",
        )
    if outcome is GenerationOutcome.CANCELLED:
        return decide_cancel(task_id=task_id, attempt=attempt, current=current)
    if outcome is not GenerationOutcome.FAILED:
        raise ValueError(f"outcome is not terminal: {outcome.value}")
    return _terminal_decision(
        task_id=task_id,
        attempt=attempt,
        state=GenerationDomainState.FAILED,
        billing=BillingAction.RELEASE,
        event_name="generation_failed",
    )


def _terminal_decision(
    *,
    task_id: str,
    attempt: int,
    state: GenerationDomainState,
    billing: BillingAction,
    event_name: str,
) -> TransitionDecision:
    return TransitionDecision(
        next_state=state.value,
        billing_action=billing,
        queue_action=QueueAction.RELEASE,
        effects=EffectBatch(
            database=(
                Effect(
                    EffectKind.DATABASE,
                    f"mark_{state.value}",
                    token=_token(task_id, attempt, "terminal_state"),
                ),
            ),
            billing=(
                Effect(
                    EffectKind.BILLING,
                    billing.value,
                    token=_token(task_id, attempt, "billing"),
                ),
            ),
            events=(
                Effect(
                    EffectKind.EVENT_STAGE,
                    event_name,
                    token=_token(task_id, attempt, "terminal_event"),
                ),
                Effect(
                    EffectKind.COMMIT,
                    "commit_terminal",
                    token=_token(task_id, attempt, "terminal_commit"),
                ),
                Effect(
                    EffectKind.EVENT_DELIVERY,
                    f"deliver_{event_name}",
                    token=_token(task_id, attempt, "terminal_delivery"),
                ),
            ),
            queue=(
                Effect(
                    EffectKind.QUEUE,
                    "release_queue_slot",
                    token=_token(task_id, attempt, "queue_release"),
                ),
            ),
            lease=(
                Effect(
                    EffectKind.LEASE,
                    "release_task_lease",
                    token=_token(task_id, attempt, "lease_release"),
                ),
            ),
        ),
    )
