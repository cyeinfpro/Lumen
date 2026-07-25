from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ...task_runtime import (
    BillingAction,
    Effect,
    EffectBatch,
    EffectKind,
    IdempotencyToken,
    LeaseState,
    QueueAction,
    TransitionDecision,
    lease_allows_mutation,
)


class CompletionDomainState(StrEnum):
    QUEUED = "queued"
    STREAMING = "streaming"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompletionFrameKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_UPDATE = "tool_update"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class CompletionStreamSnapshot:
    text: str = ""
    reasoning: str = ""
    tool_updates: tuple[dict[str, Any], ...] = ()
    terminal: CompletionFrameKind | None = None


@dataclass(frozen=True, slots=True)
class CompletionFrame:
    kind: CompletionFrameKind
    text: str = ""
    tool_update: dict[str, Any] | None = None


def reduce_completion_frame(
    snapshot: CompletionStreamSnapshot,
    frame: CompletionFrame,
) -> CompletionStreamSnapshot:
    if snapshot.terminal is not None:
        return snapshot
    if frame.kind is CompletionFrameKind.TEXT_DELTA:
        return replace(snapshot, text=snapshot.text + frame.text)
    if frame.kind is CompletionFrameKind.REASONING_DELTA:
        return replace(snapshot, reasoning=snapshot.reasoning + frame.text)
    if frame.kind is CompletionFrameKind.TOOL_UPDATE:
        if frame.tool_update is None:
            return snapshot
        return replace(
            snapshot,
            tool_updates=(*snapshot.tool_updates, dict(frame.tool_update)),
        )
    return replace(snapshot, terminal=frame.kind)


def decide_completion_claim(
    *,
    task_id: str,
    attempt: int,
    current: CompletionDomainState,
    lease: LeaseState,
) -> TransitionDecision:
    if current is not CompletionDomainState.QUEUED:
        raise ValueError(f"illegal completion claim from {current.value}")
    if not lease_allows_mutation(lease):
        return TransitionDecision(
            next_state=current.value,
            queue_action=QueueAction.DEFER,
            reason=f"lease_{lease.value}",
        )
    return TransitionDecision(
        next_state=CompletionDomainState.STREAMING.value,
        effects=EffectBatch(
            database=(
                Effect(
                    EffectKind.DATABASE,
                    "claim_completion",
                    token=IdempotencyToken(task_id, "claim", attempt),
                ),
            ),
        ),
    )


def decide_completion_finalize(
    *,
    task_id: str,
    attempt: int,
    current: CompletionDomainState,
    terminal: CompletionFrameKind,
) -> TransitionDecision:
    if current not in {
        CompletionDomainState.STREAMING,
        CompletionDomainState.FINALIZING,
    }:
        raise ValueError(f"illegal completion finalize from {current.value}")
    if terminal is CompletionFrameKind.COMPLETED:
        state = CompletionDomainState.SUCCEEDED
        billing = BillingAction.CHARGE
    elif terminal is CompletionFrameKind.CANCELLED:
        state = CompletionDomainState.CANCELLED
        billing = BillingAction.RELEASE
    elif terminal in {
        CompletionFrameKind.FAILED,
        CompletionFrameKind.INCOMPLETE,
    }:
        state = CompletionDomainState.FAILED
        billing = BillingAction.RELEASE
    else:
        raise ValueError(f"frame is not terminal: {terminal.value}")

    def token(operation: str) -> IdempotencyToken:
        return IdempotencyToken(task_id, operation, attempt)

    event_name = f"completion_{state.value}"
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
            lease=(
                Effect(
                    EffectKind.LEASE,
                    "release_task_lease",
                    token=token("lease_release"),
                ),
            ),
        ),
    )
