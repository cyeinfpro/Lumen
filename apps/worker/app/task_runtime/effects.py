from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

from .contracts import IdempotencyToken


class EffectKind(IntEnum):
    DATABASE = 10
    BILLING = 20
    EVENT_STAGE = 30
    COMMIT = 40
    EVENT_DELIVERY = 50
    QUEUE = 60
    LEASE = 70


@dataclass(frozen=True, slots=True)
class Effect:
    kind: EffectKind
    name: str
    payload: Any = None
    token: IdempotencyToken | None = None


@dataclass(frozen=True, slots=True)
class EffectBatch:
    database: tuple[Effect, ...] = ()
    billing: tuple[Effect, ...] = ()
    events: tuple[Effect, ...] = ()
    queue: tuple[Effect, ...] = ()
    lease: tuple[Effect, ...] = ()

    def ordered(self) -> tuple[Effect, ...]:
        effects = (
            *self.database,
            *self.billing,
            *self.events,
            *self.queue,
            *self.lease,
        )
        if any(left.kind > right.kind for left, right in zip(effects, effects[1:])):
            raise ValueError("effect batch violates execution ordering")
        return effects


class EffectExecutor(Protocol):
    async def was_applied(self, token: IdempotencyToken) -> bool: ...

    async def apply(self, effect: Effect) -> None: ...

    async def mark_applied(self, token: IdempotencyToken) -> None: ...


async def execute_effect_batch(
    batch: EffectBatch,
    executor: EffectExecutor,
) -> tuple[Effect, ...]:
    applied: list[Effect] = []
    for effect in batch.ordered():
        if effect.token is not None and await executor.was_applied(effect.token):
            continue
        await executor.apply(effect)
        if effect.token is not None:
            await executor.mark_applied(effect.token)
        applied.append(effect)
    return tuple(applied)
