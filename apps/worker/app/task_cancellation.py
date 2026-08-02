"""DB-authoritative cancellation probes for generation and completion workers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable

from sqlalchemy import select

from lumen_core.constants import MessageStatus
from lumen_core.model_entities.accounts import User
from lumen_core.model_entities.conversations import Conversation, Message

from .task_runtime import RuntimeSlot


@dataclass(frozen=True, slots=True)
class DurableCancellationState:
    requested: bool
    reason: str | None = None


class DurableCancellationProbeUnavailable(RuntimeError):
    """The durable cancellation authority could not be read."""


@dataclass(slots=True)
class _CancellationScope:
    kind: str
    task_id: str
    model: Any
    session_factory: Callable[[], Any]
    logger: logging.Logger
    poll_interval_s: float
    last_checked_at: float = 0.0
    last_state: DurableCancellationState = DurableCancellationState(False)


_CURRENT_SCOPE: RuntimeSlot[_CancellationScope] = RuntimeSlot(
    "lumen_task_cancellation_scope"
)


def bind_task_cancellation(
    *,
    kind: str,
    task_id: str,
    model: Any,
    session_factory: Callable[[], Any],
    logger: logging.Logger,
    poll_interval_s: float = 0.5,
) -> Any:
    """Bind one task's durable cancellation reader to its execution context."""
    return _CURRENT_SCOPE.use(
        _CancellationScope(
            kind=kind,
            task_id=task_id,
            model=model,
            session_factory=session_factory,
            logger=logger,
            poll_interval_s=max(0.05, float(poll_interval_s)),
        )
    )


def force_next_cancellation_check(task_id: str) -> None:
    """Invalidate the bound negative cache before a critical state transition."""
    scope = _CURRENT_SCOPE.current_or_none()
    if scope is not None and scope.task_id == task_id:
        scope.last_checked_at = 0.0


async def load_durable_cancellation_state(
    session: Any,
    *,
    model: Any,
    task_id: str,
) -> DurableCancellationState:
    """Read task intent plus durable parent-context cancellation state."""
    row = (
        await session.execute(
            select(
                model.cancel_requested_at,
                model.status,
                Message.status,
                Message.deleted_at,
                Conversation.deleted_at,
                User.deleted_at,
            )
            .select_from(model)
            .join(Message, Message.id == model.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(User, User.id == model.user_id)
            .where(model.id == task_id)
        )
    ).one_or_none()
    if row is None:
        return DurableCancellationState(True, "task_missing")

    (
        cancel_requested_at,
        task_status,
        message_status,
        message_deleted_at,
        conversation_deleted_at,
        user_deleted_at,
    ) = row
    if cancel_requested_at is not None:
        return DurableCancellationState(True, "task_cancel_requested")
    if str(task_status) == "canceled":
        return DurableCancellationState(True, "task_canceled")
    if str(message_status) == MessageStatus.CANCELED.value:
        return DurableCancellationState(True, "message_canceled")
    if message_deleted_at is not None:
        return DurableCancellationState(True, "message_deleted")
    if conversation_deleted_at is not None:
        return DurableCancellationState(True, "conversation_deleted")
    if user_deleted_at is not None:
        return DurableCancellationState(True, "user_deleted")
    return DurableCancellationState(False)


async def scoped_cancellation_requested(
    task_id: str,
    *,
    redis_signal: bool | None,
    force_db: bool = False,
) -> bool:
    """Resolve cancellation with DB authority inside a bound worker scope.

    ``redis_signal`` is only a wake-up hint. A stale key cannot cancel a task
    whose durable intent was cleared by retry, while a missing/expired key is
    repaired by periodic DB polling.
    """
    scope = _CURRENT_SCOPE.current_or_none()
    if scope is None or scope.task_id != task_id:
        # Preserve the low-level helper contract used by isolated unit tests.
        # Production runners always bind a durable scope.
        return True if redis_signal is None else bool(redis_signal)

    now = time.monotonic()
    should_read_db = (
        force_db
        or scope.last_state.requested
        or scope.last_checked_at <= 0
        or now - scope.last_checked_at >= scope.poll_interval_s
    )
    if not should_read_db:
        return scope.last_state.requested

    try:
        async with scope.session_factory() as session:
            state = await load_durable_cancellation_state(
                session,
                model=scope.model,
                task_id=task_id,
            )
    except Exception as exc:  # noqa: BLE001
        scope.logger.warning(
            "%s durable cancel check unavailable task=%s err=%s",
            scope.kind,
            task_id,
            exc,
        )
        scope.last_checked_at = now
        raise DurableCancellationProbeUnavailable(
            f"{scope.kind} durable cancellation state unavailable task={task_id}"
        ) from exc

    scope.last_checked_at = now
    scope.last_state = state
    return state.requested


__all__ = [
    "DurableCancellationProbeUnavailable",
    "DurableCancellationState",
    "bind_task_cancellation",
    "force_next_cancellation_check",
    "load_durable_cancellation_state",
    "scoped_cancellation_requested",
]
