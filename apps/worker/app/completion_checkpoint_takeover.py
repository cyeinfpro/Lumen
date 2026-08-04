"""Pre-claim completion checkpoint takeover and terminal settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    EV_COMP_FAILED,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    MessageStatus,
)

from .completion_checkpoint import (
    COMPLETION_CHECKPOINT_QUARANTINED,
    COMPLETION_CHECKPOINT_STATE_KEY,
    apply_completed_checkpoint,
    completion_checkpoint_has_no_usable_output,
    completion_checkpoint_requires_recovery,
    completion_checkpoint_validation_error,
    completion_has_completed_checkpoint,
    recover_completion_checkpoint_images,
)
from .completion_execution_settlement import settle_completion_actual_or_unknown
from .completion_text import completion_text_or_empty

COMPLETION_CHECKPOINT_CORRUPT_CODE = "completion_checkpoint_corrupt"
COMPLETION_CHECKPOINT_CORRUPT_MESSAGE = (
    "completion checkpoint payload is corrupt and was quarantined"
)


@dataclass(frozen=True, slots=True)
class CompletionCheckpointClaimAction:
    required: bool
    validation_error: str | None = None


@dataclass(frozen=True, slots=True)
class _CheckpointApplyContext:
    session: Any
    billing: Any
    now: datetime
    stage_event: Any


def completion_checkpoint_claim_action(
    completion: Any,
) -> CompletionCheckpointClaimAction:
    validation_error = completion_checkpoint_validation_error(completion)
    if validation_error is not None:
        return CompletionCheckpointClaimAction(True, validation_error)
    return CompletionCheckpointClaimAction(
        bool(
            completion_checkpoint_requires_recovery(completion)
            or completion_has_completed_checkpoint(completion)
            or completion_checkpoint_has_no_usable_output(completion)
        )
    )


def bind_completion_claim_identity(state: Any, completion: Any) -> None:
    state.preparation.was_restarted = bool(
        (completion.attempt or 0) > 0
        and completion_text_or_empty(completion.text)
    )
    state.preparation.user_id = completion.user_id
    state.preparation.message_id = completion.message_id
    state.preparation.system_prompt = completion.system_prompt
    state.preparation.user_api_credential_id = getattr(
        completion,
        "user_api_credential_id",
        None,
    )
    state.preparation.chat_model = (
        completion.model or state.ports.context.DEFAULT_CHAT_MODEL
    )


def _checkpoint_failure_delivery(
    state: Any,
    session: Any,
    completion: Any,
    *,
    code: str,
    message: str,
) -> tuple[str, str, dict[str, Any]]:
    return state.ports.events._stage_completion_event(
        session,
        completion.user_id,
        state.request.channel,
        EV_COMP_FAILED,
        state.ports.events._completion_event_payload(
            str(completion.id),
            completion.message_id,
            int(completion.attempt or 0),
            int(completion.attempt or 0),
            execution_epoch=int(completion.execution_epoch or 0),
            code=code,
            message=message,
            retriable=False,
        ),
    )


async def _apply_checkpoint_failure(
    state: Any,
    session: Any,
    completion: Any,
    *,
    code: str,
    message: str,
    validation_error: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    await settle_completion_actual_or_unknown(
        state.ports.billing.worker_billing,
        session,
        completion,
        reason=code,
        knowledge="unknown",
    )
    if validation_error is not None:
        request = dict(completion.upstream_request or {})
        request[COMPLETION_CHECKPOINT_STATE_KEY] = COMPLETION_CHECKPOINT_QUARANTINED
        request["completion_checkpoint_quarantine_reason"] = validation_error[:500]
        completion.upstream_request = request
    completion.status = CompletionStatus.FAILED.value
    completion.progress_stage = CompletionStage.FINALIZING
    completion.finished_at = datetime.now(timezone.utc)
    completion.error_code = code
    completion.error_message = message
    if code == EC.NO_TEXT_RETURNED.value:
        completion.text = ""
    row = await session.get(
        state.ports.persistence.Message,
        completion.message_id,
    )
    if row is not None and row.status != MessageStatus.CANCELED.value:
        row.status = MessageStatus.FAILED.value
    return _checkpoint_failure_delivery(
        state,
        session,
        completion,
        code=code,
        message=message,
    )


def _checkpoint_fence_matches(state: Any, completion: Any) -> bool:
    return bool(
        int(completion.attempt or 0) == state.preparation.attempt_epoch
        and int(completion.execution_epoch or 0)
        == int(
            state.preparation.queue_metadata_payload.get("execution_epoch", 0)
            or 0
        )
    )


async def take_over_completion_checkpoint(state: Any, checkpoint_row: Any) -> bool:
    if completion_checkpoint_requires_recovery(checkpoint_row):
        await recover_completion_checkpoint_images(
            checkpoint_row,
            redis=state.request.redis,
            channel=state.request.channel,
            tool_image_service=state.ports.tools.tool_image_service,
        )

    delivery: tuple[str, str, dict[str, Any]] | None = None
    outcome = "failed"
    async with state.ports.persistence.SessionLocal() as session:
        await state.ports.persistence._acquire_completion_xact_lock(
            session,
            state.request.task_id,
        )
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(
                    state.ports.persistence.Completion.id == state.request.task_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None or not _checkpoint_fence_matches(state, completion):
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion checkpoint takeover superseded task={state.request.task_id}"
            )
        if state.ports.persistence.is_completion_terminal(completion.status):
            state.settlement.task_outcome = (
                "succeeded"
                if completion.status == CompletionStatus.SUCCEEDED.value
                else "terminal"
            )
            return True
        validation_error = completion_checkpoint_validation_error(completion)
        if validation_error is not None:
            delivery = await _apply_checkpoint_failure(
                state,
                session,
                completion,
                code=COMPLETION_CHECKPOINT_CORRUPT_CODE,
                message=COMPLETION_CHECKPOINT_CORRUPT_MESSAGE,
                validation_error=validation_error,
            )
        elif completion_checkpoint_has_no_usable_output(completion):
            delivery = await _apply_checkpoint_failure(
                state,
                session,
                completion,
                code=EC.NO_TEXT_RETURNED.value,
                message="upstream returned empty completion",
            )
        elif completion_has_completed_checkpoint(completion):
            delivery = await apply_completed_checkpoint(
                _CheckpointApplyContext(
                    session=session,
                    billing=state.ports.billing.worker_billing,
                    now=datetime.now(timezone.utc),
                    stage_event=(
                        state.ports.events._COMPLETION_EVENT_HOOKS.stage_outbox_event
                    ),
                ),
                completion,
            )
            outcome = "succeeded"
        else:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion checkpoint takeover incomplete task={state.request.task_id}"
            )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)

    if delivery is not None:
        await state.ports.events._deliver_completion_event(
            state.request.redis,
            delivery,
        )
    state.settlement.task_outcome = outcome
    return True


__all__ = [
    "CompletionCheckpointClaimAction",
    "bind_completion_claim_identity",
    "completion_checkpoint_claim_action",
    "take_over_completion_checkpoint",
]
