"""Completion execution receipts and terminal billing settlement."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from lumen_core.constants import (
    EV_COMP_FAILED,
    CompletionStage,
    CompletionStatus,
    MessageStatus,
)
from lumen_core.model_entities.tasks import Completion
from lumen_core.upstream_billing import (
    LocalBillingAction,
    NO_UPSTREAM_COST_RECEIPTS,
    decide_dispatch_evidence_billing,
    mark_upstream_dispatch_proven_no_cost,
    mark_upstream_dispatch_proven_undelivered,
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)

from .completion_checkpoint import (
    completion_execution_epoch,
    completion_has_trustworthy_persisted_usage,
)

COMPLETION_RESULT_UNKNOWN_CODE = "completion_result_unknown"
COMPLETION_RESULT_UNKNOWN_MESSAGE = (
    "upstream dispatch completed without a response receipt; result is unknown"
)


class CompletionDispatchResultUnknown(RuntimeError):
    error_code = COMPLETION_RESULT_UNKNOWN_CODE
    status_code = None


async def settle_completion_actual_or_unknown(
    billing: Any,
    session: Any,
    completion: Any,
    *,
    reason: str,
    knowledge: str,
) -> None:
    if completion_has_trustworthy_persisted_usage(completion):
        await billing.charge_completion(session, completion)
        return
    decision = decide_dispatch_evidence_billing(
        completion,
        actual_cost_known=False,
    )
    if decision.released:
        await billing.release_completion(
            session,
            completion,
            reason=reason,
        )
        return
    await billing.settle_completion_unknown_upstream(
        session,
        completion,
        reason=reason,
        knowledge=knowledge,
    )


async def stage_completion_preflight_failure(
    state: Any,
    session: Any,
    completion: Completion,
    *,
    err_code: str,
    err_msg: str,
) -> None:
    completion.status = CompletionStatus.FAILED.value
    completion.progress_stage = CompletionStage.FINALIZING
    completion.attempt = state.preparation.attempt
    completion.finished_at = datetime.now(timezone.utc)
    completion.error_code = err_code
    completion.error_message = err_msg
    message = await session.get(
        state.ports.persistence.Message,
        state.preparation.message_id,
    )
    if message is not None and message.status != MessageStatus.CANCELED:
        message.status = MessageStatus.FAILED
    failed = await session.get(
        state.ports.persistence.Completion,
        state.request.task_id,
    )
    if failed is not None:
        await state.ports.billing.worker_billing.release_completion(
            session,
            failed,
            reason=err_code,
        )
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before preflight failure commit")
    delivery = state.ports.events._stage_completion_event(
        session,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_FAILED,
        state.ports.events._completion_event_payload(
            state.request.task_id,
            state.preparation.message_id,
            state.preparation.attempt,
            state.preparation.attempt_epoch,
            execution_epoch=completion_execution_epoch(state),
            code=err_code,
            message=err_msg,
            retriable=False,
        ),
    )
    await session.commit()
    await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)


async def record_completion_upstream_marker(
    state: Any,
    *,
    response_received: bool,
    proven_undelivered: bool = False,
    proven_no_cost: bool = False,
) -> None:
    if sum((response_received, proven_undelivered, proven_no_cost)) > 1:
        raise ValueError("upstream marker outcomes are mutually exclusive")
    async with state.ports.persistence.SessionLocal() as session:
        ownership_conditions = [
            state.ports.persistence.Completion.id == state.request.task_id,
            state.ports.persistence.Completion.attempt
            == state.preparation.attempt,
            state.ports.persistence.Completion.execution_epoch
            == completion_execution_epoch(state),
            state.ports.persistence.Completion.status
            == CompletionStatus.STREAMING.value,
        ]
        if not response_received and not proven_undelivered and not proven_no_cost:
            ownership_conditions.append(
                state.ports.persistence.Completion.cancel_requested_at.is_(None)
            )
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(*ownership_conditions)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion marker stale task={state.request.task_id} "
                f"attempt={state.preparation.attempt}"
            )
        marker = (
            mark_upstream_response_received
            if response_received
            else mark_upstream_dispatch_proven_no_cost
            if proven_no_cost
            else mark_upstream_dispatch_proven_undelivered
            if proven_undelivered
            else mark_upstream_dispatch_started
        )
        completion.upstream_request = marker(
            completion,
            at=datetime.now(timezone.utc).isoformat(),
            attempt=state.preparation.attempt,
            execution_epoch=completion_execution_epoch(state),
        )
        await session.commit()


async def ensure_completion_execution_current(state: Any) -> None:
    async with state.ports.persistence.SessionLocal() as session:
        current = (
            await session.execute(
                state.ports.persistence.select(
                    state.ports.persistence.Completion.id
                ).where(
                    state.ports.persistence.Completion.id == state.request.task_id,
                    state.ports.persistence.Completion.attempt
                    == state.preparation.attempt_epoch,
                    state.ports.persistence.Completion.execution_epoch
                    == completion_execution_epoch(state),
                    state.ports.persistence.Completion.status.in_(
                        state.ports.retry._RUNNING_COMPLETION_STATUSES
                    ),
                )
            )
        ).scalar_one_or_none()
    if current is None:
        raise state.ports.retry._CompletionEpochSuperseded(
            f"completion execution superseded task={state.request.task_id} "
            f"execution_epoch={completion_execution_epoch(state)} "
            f"attempt={state.preparation.attempt_epoch}"
        )


async def raise_completion_dispatch_failure(
    state: Any,
    exc: Exception,
    *,
    record_marker: Any = record_completion_upstream_marker,
) -> None:
    terminal_exceptions = (
        CompletionDispatchResultUnknown,
        state.ports.retry._CompletionEpochSuperseded,
        state.ports.retry._TaskCancelled,
    )
    if isinstance(exc, terminal_exceptions) or state.usage.response_receipt_recorded:
        return
    if getattr(state.usage, "active_round_dispatch_started", True) is False:
        return
    if _completion_dispatch_failure_proves_undelivered(exc):
        state.usage.active_round_dispatch_proven_undelivered = True
        usage_totals = getattr(state.usage, "usage_totals", None)
        if usage_totals is not None:
            usage_totals.mark_round_proven_no_cost()
        await record_marker(
            state,
            response_received=False,
            proven_undelivered=True,
        )
        return
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code > 0:
        state.usage.active_round_response_received = True
        await record_marker(
            state,
            response_received=True,
        )
        state.usage.response_receipt_recorded = True
        return
    if (
        state.preparation.queue_metadata_payload.get("_stable_provider_idempotency")
        is True
    ):
        return
    raise CompletionDispatchResultUnknown(COMPLETION_RESULT_UNKNOWN_MESSAGE) from exc


def _completion_dispatch_failure_proves_undelivered(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return True
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return False
    receipt_reason = payload.get("receipt_reason") or payload.get(
        "upstream_receipt_reason"
    )
    return (
        isinstance(receipt_reason, str) and receipt_reason in NO_UPSTREAM_COST_RECEIPTS
    )


def _completion_has_live_usage(state: Any) -> bool:
    usage = state.usage
    streaming = getattr(state, "streaming", None)
    totals = getattr(usage, "usage_totals", None)
    values = totals.values() if totals is not None else ()
    return bool(
        getattr(streaming, "has_partial", False)
        or getattr(streaming, "tool_images", ())
        or any(int(value or 0) > 0 for value in values)
    )


async def completion_cancel_requires_unknown_settlement(state: Any) -> bool:
    if _completion_has_live_usage(state):
        return False
    usage = state.usage
    if getattr(usage, "active_round_dispatch_started", False):
        execution_epoch = completion_execution_epoch(state)
        attempt = max(0, int(getattr(state.preparation, "attempt", 0) or 0))
        evidence: dict[str, object] = mark_upstream_dispatch_started(
            {},
            at="live",
            attempt=attempt,
            execution_epoch=execution_epoch,
        )
        if getattr(
            usage,
            "active_round_dispatch_proven_undelivered",
            False,
        ):
            evidence = mark_upstream_dispatch_proven_undelivered(
                evidence,
                at="live",
                attempt=attempt,
                execution_epoch=execution_epoch,
            )
        elif getattr(usage, "active_round_dispatch_proven_no_cost", False):
            evidence = mark_upstream_dispatch_proven_no_cost(
                evidence,
                at="live",
                attempt=attempt,
                execution_epoch=execution_epoch,
            )
        elif getattr(usage, "active_round_response_received", False):
            evidence = mark_upstream_response_received(
                evidence,
                at="live",
                attempt=attempt,
                execution_epoch=execution_epoch,
            )
        return (
            decide_dispatch_evidence_billing(
                evidence,
                actual_cost_known=False,
                execution_epoch=execution_epoch,
            ).action
            is LocalBillingAction.SETTLE_DEFAULT
        )
    async with state.ports.persistence.SessionLocal() as session:
        completion = await session.get(
            state.ports.persistence.Completion,
            state.request.task_id,
        )
    if completion is None:
        return False
    execution_epoch = completion_execution_epoch(state)
    if int(getattr(completion, "execution_epoch", 0) or 0) != execution_epoch:
        return False
    decision = decide_dispatch_evidence_billing(
        completion,
        actual_cost_known=False,
        execution_epoch=execution_epoch,
    )
    return decision.action is LocalBillingAction.SETTLE_DEFAULT


async def settle_completion_result_unknown(state: Any) -> None:
    await _settle_completion_unknown(
        state,
        status=CompletionStatus.FAILED.value,
        code=COMPLETION_RESULT_UNKNOWN_CODE,
        message=COMPLETION_RESULT_UNKNOWN_MESSAGE,
        require_no_cancel=True,
    )


async def settle_completion_cancel_unknown(state: Any) -> None:
    await _settle_completion_unknown(
        state,
        status=CompletionStatus.CANCELED.value,
        code="cancelled",
        message="cancelled by user",
        require_no_cancel=False,
    )


async def _settle_completion_unknown(
    state: Any,
    *,
    status: str,
    code: str,
    message: str,
    require_no_cancel: bool,
) -> None:
    async with state.ports.persistence.SessionLocal() as session:
        conditions = [
            state.ports.persistence.Completion.id == state.request.task_id,
            state.ports.persistence.Completion.attempt
            == state.preparation.attempt_epoch,
            state.ports.persistence.Completion.execution_epoch
            == completion_execution_epoch(state),
            state.ports.persistence.Completion.status.in_(
                state.ports.retry._RUNNING_COMPLETION_STATUSES
            ),
        ]
        if require_no_cancel:
            conditions.append(
                state.ports.persistence.Completion.cancel_requested_at.is_(None)
            )
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(*conditions)
            .values(
                status=status,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=code,
                error_message=message,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion unknown settlement superseded "
                f"task={state.request.task_id} "
                f"execution_epoch={completion_execution_epoch(state)}"
            )
        row = await session.get(
            state.ports.persistence.Message,
            state.preparation.message_id,
        )
        if row is not None and row.status != MessageStatus.CANCELED:
            row.status = MessageStatus.FAILED
        completion = await session.get(
            state.ports.persistence.Completion,
            state.request.task_id,
        )
        if completion is None:
            raise LookupError(f"completion missing: {state.request.task_id}")
        await settle_completion_actual_or_unknown(
            state.ports.billing.worker_billing,
            session,
            completion,
            reason=code,
            knowledge="unknown",
        )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                execution_epoch=completion_execution_epoch(state),
                code=code,
                message=message,
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "failed"


__all__ = [
    "COMPLETION_RESULT_UNKNOWN_CODE",
    "COMPLETION_RESULT_UNKNOWN_MESSAGE",
    "CompletionDispatchResultUnknown",
    "completion_cancel_requires_unknown_settlement",
    "ensure_completion_execution_current",
    "raise_completion_dispatch_failure",
    "record_completion_upstream_marker",
    "settle_completion_actual_or_unknown",
    "settle_completion_cancel_unknown",
    "settle_completion_result_unknown",
    "stage_completion_preflight_failure",
]
