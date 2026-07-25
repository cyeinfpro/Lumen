from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .effects import EffectBatch


class BillingAction(StrEnum):
    NONE = "none"
    SETTLE = "settle"
    RELEASE = "release"
    CHARGE = "charge"


class QueueAction(StrEnum):
    NONE = "none"
    ENQUEUE = "enqueue"
    DEFER = "defer"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    delay_s: float
    reason: str
    max_attempts: int


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    next_state: str
    effects: EffectBatch = EffectBatch()
    billing_action: BillingAction = BillingAction.NONE
    queue_action: QueueAction = QueueAction.NONE
    retry: RetrySchedule | None = None
    reason: str = ""


def require_transition(
    current: str,
    allowed: frozenset[str],
    target: str,
) -> str:
    if current not in allowed:
        raise ValueError(f"illegal transition {current!r} -> {target!r}")
    return target
