"""Generation and completion reconciliation domain implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select

from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_FAILED,
    EV_GEN_FAILED,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    user_channel,
)
from lumen_core.models import Completion, Generation, Message
from lumen_core.upstream_billing import (
    NO_UPSTREAM_COST_RECEIPTS,
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
    has_upstream_response_receipt,
    mark_upstream_dispatch_proven_undelivered,
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
    upstream_dispatch_result_unknown,
)

from .contracts import LeaseState, ReconcileContext, ReconcileResult
from .lease import read_lease_states
from .metrics import reconciliation_rows_total

RECON_STUCK_AFTER = timedelta(minutes=5)
RECON_TIMEOUT_CODE = "timeout"
RECON_TIMEOUT_MESSAGE = "task stuck; reconciler timed out"
RECON_RESULT_UNKNOWN_CODE = "result_unknown"
RECON_RESULT_UNKNOWN_MESSAGE = "upstream dispatch has no response receipt; result is unknown"
RECON_CANCEL_CODE = "cancelled"
RECON_CANCEL_MESSAGE = "cancelled by user"
EV_GEN_REQUEUED = "generation.requeued"
EV_COMP_REQUEUED = "completion.requeued"
COMPLETION_EXECUTION_EPOCH_KEY = "execution_epoch"
COMPLETION_RESULT_UNKNOWN_CODE = "completion_result_unknown"
COMPLETION_RESULT_UNKNOWN_MESSAGE = "upstream dispatch completed without a response receipt; result is unknown"
COMPLETION_USAGE_EXECUTION_EPOCH_KEY = "completion_usage_execution_epoch"
_COMPLETION_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "reasoning_tokens",
    "image_output_tokens",
)
RECON_BATCH_LIMIT = 100


class CompletionDispatchResultUnknown(RuntimeError):
    error_code = COMPLETION_RESULT_UNKNOWN_CODE
    status_code = None


def _aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def completion_execution_epoch(state: Any) -> int:
    try:
        return max(
            0,
            int(
                state.preparation.queue_metadata_payload.get(
                    COMPLETION_EXECUTION_EPOCH_KEY,
                    0,
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def completion_has_trustworthy_persisted_usage(completion: Any) -> bool:
    request = (
        completion.upstream_request
        if isinstance(getattr(completion, "upstream_request", None), dict)
        else {}
    )
    try:
        usage_epoch = max(
            0,
            int(request.get(COMPLETION_USAGE_EXECUTION_EPOCH_KEY)),
        )
        execution_epoch = max(
            0,
            int(getattr(completion, "execution_epoch", 0) or 0),
        )
    except (TypeError, ValueError):
        return False
    if usage_epoch != execution_epoch:
        return False
    for field in _COMPLETION_USAGE_FIELDS:
        try:
            if int(getattr(completion, field, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
    await billing.settle_completion_unknown_upstream(
        session,
        completion,
        reason=reason,
        knowledge=knowledge,
    )


def bind_completion_execution_fence(
    state: Any,
    execution_epoch: int,
) -> None:
    epoch = max(0, int(execution_epoch))
    ports = state.ports
    persistence = ports.persistence
    completion_model = persistence.Completion
    original_update = persistence.update
    original_context_record = ports.context._record_completion_context_metadata
    original_event_payload = ports.events._completion_event_payload
    original_upstream = ports.upstream

    def fenced_update(model: Any) -> Any:
        statement = original_update(model)
        if model is completion_model:
            statement = statement.where(completion_model.execution_epoch == epoch)
        return statement

    async def fenced_flush(
        task_id: str,
        text: str,
        *,
        attempt_epoch: int,
        retries: int = 3,
    ) -> None:
        last_exc: BaseException | None = None
        for index in range(max(1, int(retries))):
            try:
                async with persistence.SessionLocal() as session:
                    result = await session.execute(
                        fenced_update(completion_model)
                        .where(
                            completion_model.id == task_id,
                            completion_model.attempt == attempt_epoch,
                            completion_model.status == CompletionStatus.STREAMING.value,
                        )
                        .values(text=text)
                    )
                    if persistence.affected_rows(result) == 0:
                        raise ports.retry._CompletionEpochSuperseded(
                            f"completion execution superseded task={task_id} "
                            f"execution_epoch={epoch} attempt={attempt_epoch}"
                        )
                    await session.commit()
                    return
            except ports.retry._CompletionEpochSuperseded:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                ports.events.logger.warning(
                    "completion text flush failed task=%s epoch=%s "
                    "attempt=%s try=%d/%d err=%s",
                    task_id,
                    epoch,
                    attempt_epoch,
                    index + 1,
                    retries,
                    exc,
                )
                if index + 1 < retries:
                    await asyncio.sleep(0.2 * (2**index))
        raise original_upstream.UpstreamError(
            "completion text flush failed after retries",
            error_code="upstream_error",
            status_code=None,
        ) from last_exc

    async def fenced_context_record(
        session: Any,
        *,
        task_id: str,
        attempt_epoch: int,
        packed: Any,
    ) -> None:
        current_epoch = (
            await session.execute(
                persistence.select(completion_model.execution_epoch)
                .where(
                    completion_model.id == task_id,
                    completion_model.attempt == attempt_epoch,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current_epoch != epoch:
            raise ports.retry._CompletionEpochSuperseded(
                f"completion context superseded task={task_id} "
                f"execution_epoch={epoch} current={current_epoch}"
            )
        await original_context_record(
            session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            packed=packed,
        )

    async def fenced_upstream_metadata(
        *,
        task_id: str,
        attempt_epoch: int,
        provider_event: dict[str, str],
        fast_mode: bool,
    ) -> None:
        if not provider_event:
            return
        async with persistence.SessionLocal() as session:
            completion = (
                await session.execute(
                    persistence.select(completion_model)
                    .where(
                        completion_model.id == task_id,
                        completion_model.attempt == attempt_epoch,
                        completion_model.execution_epoch == epoch,
                        completion_model.status.in_(
                            ports.retry._RUNNING_COMPLETION_STATUSES
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if completion is None:
                raise ports.retry._CompletionEpochSuperseded(
                    f"completion metadata superseded task={task_id} "
                    f"execution_epoch={epoch} attempt={attempt_epoch}"
                )
            completion.upstream_request = (
                original_upstream._merge_completion_upstream_metadata(
                    dict(completion.upstream_request or {}),
                    provider_event=provider_event,
                    fast_mode=fast_mode,
                )
                or None
            )
            await session.commit()

    def fenced_event_payload(*args: Any, **extra: Any) -> dict[str, Any]:
        extra.setdefault("execution_epoch", epoch)
        return original_event_payload(*args, **extra)

    state.ports = replace(
        ports,
        persistence=replace(
            persistence,
            update=fenced_update,
            _flush_completion_text=fenced_flush,
        ),
        context=replace(
            ports.context,
            _record_completion_context_metadata=fenced_context_record,
        ),
        upstream=replace(
            original_upstream,
            _record_completion_upstream_metadata=fenced_upstream_metadata,
        ),
        events=replace(
            ports.events,
            _completion_event_payload=fenced_event_payload,
        ),
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
) -> None:
    async with state.ports.persistence.SessionLocal() as session:
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(
                    state.ports.persistence.Completion.id == state.request.task_id,
                    state.ports.persistence.Completion.attempt
                    == state.preparation.attempt,
                    state.ports.persistence.Completion.execution_epoch
                    == completion_execution_epoch(state),
                    state.ports.persistence.Completion.status
                    == CompletionStatus.STREAMING.value,
                    state.ports.persistence.Completion.cancel_requested_at.is_(None),
                )
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


async def raise_completion_dispatch_failure(state: Any, exc: Exception) -> None:
    terminal_exceptions = (
        CompletionDispatchResultUnknown,
        state.ports.retry._CompletionEpochSuperseded,
        state.ports.retry._TaskCancelled,
    )
    if isinstance(exc, terminal_exceptions) or state.usage.response_receipt_recorded:
        return
    if _completion_dispatch_failure_proves_undelivered(exc):
        state.usage.active_round_dispatch_proven_undelivered = True
        await record_completion_upstream_marker(
            state,
            response_received=False,
            proven_undelivered=True,
        )
        return
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code > 0:
        state.usage.active_round_response_received = True
        await record_completion_upstream_marker(state, response_received=True)
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


async def completion_cancel_requires_unknown_settlement(state: Any) -> bool:
    usage = state.usage
    if getattr(usage, "active_round_dispatch_started", False):
        return not (
            getattr(usage, "active_round_response_received", False)
            or getattr(usage, "active_round_dispatch_proven_undelivered", False)
        )
    if usage.response_receipt_recorded:
        return False
    async with state.ports.persistence.SessionLocal() as session:
        completion = await session.get(
            state.ports.persistence.Completion,
            state.request.task_id,
        )
    if completion is None:
        return False
    execution_epoch = completion_execution_epoch(state)
    return bool(
        int(getattr(completion, "execution_epoch", 0) or 0) == execution_epoch
        and has_upstream_dispatch_receipt(
            completion,
            execution_epoch=execution_epoch,
        )
        and not has_proven_undelivered_dispatch(
            completion,
            execution_epoch=execution_epoch,
        )
    )


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


@dataclass(frozen=True, slots=True)
class TaskDomainSpec:
    name: str
    model: type[Any]
    active_statuses: tuple[str, ...]
    max_attempts: int
    queued_status: str
    queued_stage: str
    failed_status: str
    finalizing_stage: str
    requeued_event: str
    failed_event: str
    release_method: str
    settle_unknown_method: str
    id_field: str


class TaskDomainReconciler:
    def __init__(self, spec: TaskDomainSpec) -> None:
        self.spec = spec
        self.name = spec.name

    def _eligible(self, task: Any, cutoff: datetime) -> bool:
        if str(task.status) not in self.spec.active_statuses:
            return False
        updated_at = _aware_utc(getattr(task, "updated_at", None))
        return updated_at is None or updated_at < cutoff

    def _stage_requeue_events(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        task_id = str(task.id)
        attempt = int(task.attempt or 0)
        execution_epoch = int(getattr(task, "execution_epoch", 0) or 0)
        task_payload = {
            "task_id": task_id,
            "user_id": task.user_id,
            "kind": self.spec.name,
            "source": "stuck_task_reconciler",
            "execution_epoch": execution_epoch,
        }
        data = {
            f"{self.spec.id_field}_id": task_id,
            "message_id": task.message_id,
            "attempt": attempt,
            "execution_epoch": execution_epoch,
            "max_attempts": self.spec.max_attempts,
            "kind": self.spec.name,
        }
        if self.spec.name == "completion":
            data["attempt_epoch"] = attempt
        return [
            context.stage_event(
                context.session,
                kind=self.spec.name,
                payload=task_payload,
            ),
            context.stage_event(
                context.session,
                kind="sse",
                payload={
                    "user_id": task.user_id,
                    "channel": user_channel(task.user_id),
                    "event_name": self.spec.requeued_event,
                    "data": data,
                },
            ),
        ]

    def _stage_failure_event(
        self,
        context: ReconcileContext,
        task: Any,
        *,
        code: str = RECON_TIMEOUT_CODE,
        message: str = RECON_TIMEOUT_MESSAGE,
    ) -> tuple[str, str, dict[str, Any]]:
        data = {
            f"{self.spec.id_field}_id": task.id,
            "message_id": task.message_id,
            "code": code,
            "message": message,
            "retriable": False,
            "execution_epoch": int(getattr(task, "execution_epoch", 0) or 0),
        }
        if self.spec.name == "completion":
            attempt = int(task.attempt or 0)
            data["attempt"] = attempt
            data["attempt_epoch"] = attempt
        return context.stage_event(
            context.session,
            kind="sse",
            payload={
                "user_id": task.user_id,
                "channel": user_channel(task.user_id),
                "event_name": self.spec.failed_event,
                "data": data,
            },
        )

    async def _settle_unknown_billing(
        self,
        context: ReconcileContext,
        task: Any,
        *,
        reason: str,
        knowledge: str = "unknown",
    ) -> None:
        if self.spec.name == "completion":
            await settle_completion_actual_or_unknown(
                context.billing,
                context.session,
                task,
                reason=reason,
                knowledge=knowledge,
            )
            return
        await getattr(context.billing, self.spec.settle_unknown_method)(
            context.session,
            task,
            reason=reason,
            knowledge=knowledge,
        )

    async def _settle_timeout_billing(
        self,
        context: ReconcileContext,
        task: Any,
        *,
        reason: str = RECON_TIMEOUT_CODE,
    ) -> None:
        if (
            self.spec.name == "completion"
            and completion_has_trustworthy_persisted_usage(task)
        ):
            await self._settle_unknown_billing(
                context,
                task,
                reason=reason,
            )
            return
        # A current-epoch dispatch without a response is still potentially
        # billable. Release only when there is no dispatch or the runner
        # persisted proof that the request never reached the provider.
        if has_upstream_response_receipt(task) or (
            has_upstream_dispatch_receipt(task)
            and not has_proven_undelivered_dispatch(task)
        ):
            await self._settle_unknown_billing(
                context,
                task,
                reason=reason,
            )
            return
        await getattr(context.billing, self.spec.release_method)(
            context.session,
            task,
            reason=reason,
        )

    async def _apply_timeout(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        # Billing is part of the terminal transition. If it fails, leave the
        # task untouched so the transaction can roll back and retry later.
        await self._settle_timeout_billing(context, task)
        task.status = self.spec.failed_status
        task.progress_stage = self.spec.finalizing_stage
        task.error_code = RECON_TIMEOUT_CODE
        task.error_message = RECON_TIMEOUT_MESSAGE
        task.finished_at = context.now
        task.updated_at = context.now
        message = await context.session.get(Message, task.message_id)
        if message is not None:
            message.status = MessageStatus.FAILED.value
        return self._stage_failure_event(context, task)

    async def _apply_result_unknown(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        await self._settle_unknown_billing(
            context,
            task,
            reason=RECON_RESULT_UNKNOWN_CODE,
        )
        task.status = self.spec.failed_status
        task.progress_stage = self.spec.finalizing_stage
        task.error_code = RECON_RESULT_UNKNOWN_CODE
        task.error_message = RECON_RESULT_UNKNOWN_MESSAGE
        task.finished_at = context.now
        task.updated_at = context.now
        message = await context.session.get(Message, task.message_id)
        if message is not None:
            message.status = MessageStatus.FAILED.value
        return self._stage_failure_event(
            context,
            task,
            code=RECON_RESULT_UNKNOWN_CODE,
            message=RECON_RESULT_UNKNOWN_MESSAGE,
        )

    async def _apply_cancel(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        if (
            (
                self.spec.name == "completion"
                and completion_has_trustworthy_persisted_usage(task)
            )
            or has_upstream_response_receipt(task)
            or (
                has_upstream_dispatch_receipt(task)
                and not has_proven_undelivered_dispatch(task)
            )
        ):
            await self._settle_unknown_billing(
                context,
                task,
                reason=RECON_CANCEL_CODE,
            )
        else:
            await getattr(context.billing, self.spec.release_method)(
                context.session,
                task,
                reason=RECON_CANCEL_CODE,
            )
        task.cancel_requested_at = task.cancel_requested_at or context.now
        task.status = (
            GenerationStatus.CANCELED.value
            if self.spec.name == "generation"
            else CompletionStatus.CANCELED.value
        )
        task.progress_stage = self.spec.finalizing_stage
        task.error_code = RECON_CANCEL_CODE
        task.error_message = RECON_CANCEL_MESSAGE
        task.finished_at = context.now
        task.updated_at = context.now
        message = await context.session.get(Message, task.message_id)
        if message is not None and message.status not in (
            MessageStatus.SUCCEEDED.value,
            MessageStatus.FAILED.value,
            MessageStatus.CANCELED.value,
        ):
            message.status = MessageStatus.FAILED.value
        data = {
            f"{self.spec.id_field}_id": task.id,
            "message_id": task.message_id,
            "code": RECON_CANCEL_CODE,
            "message": RECON_CANCEL_MESSAGE,
            "retriable": False,
            "execution_epoch": int(getattr(task, "execution_epoch", 0) or 0),
        }
        if self.spec.name == "completion":
            attempt = int(task.attempt or 0)
            data["attempt"] = attempt
            data["attempt_epoch"] = attempt
        return context.stage_event(
            context.session,
            kind="sse",
            payload={
                "user_id": task.user_id,
                "channel": user_channel(task.user_id),
                "event_name": self.spec.failed_event,
                "data": data,
            },
        )

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        cutoff = context.now - RECON_STUCK_AFTER
        query = (
            select(self.spec.model)
            .where(
                self.spec.model.status.in_(self.spec.active_statuses),
                or_(
                    self.spec.model.cancel_requested_at.is_not(None),
                    self.spec.model.updated_at < cutoff,
                ),
            )
            .limit(RECON_BATCH_LIMIT)
            .with_for_update(skip_locked=True)
        )
        rows = list((await context.session.execute(query)).scalars())
        lease_states = await read_lease_states(
            context.redis,
            rows,
            kind=self.name,
            unknowns=context.lease_unknowns,
            now=context.now,
        )
        result = ReconcileResult()
        for task in rows:
            cancel_requested = (
                str(task.status) in self.spec.active_statuses
                and getattr(task, "cancel_requested_at", None) is not None
            )
            if cancel_requested:
                lease_state = lease_states[str(task.id)]
                if lease_state is not LeaseState.EXPIRED:
                    reconciliation_rows_total.labels(
                        domain=self.name,
                        action=f"skip_cancel_{lease_state.value}",
                    ).inc()
                    continue
                result.pending_outbox.append(await self._apply_cancel(context, task))
                result.touched += 1
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="canceled",
                ).inc()
                continue
            if not self._eligible(task, cutoff):
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="ineligible",
                ).inc()
                continue
            lease_state = lease_states[str(task.id)]
            if lease_state is not LeaseState.EXPIRED:
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action=f"skip_{lease_state.value}",
                ).inc()
                continue

            if upstream_dispatch_result_unknown(task):
                result.pending_outbox.append(
                    await self._apply_result_unknown(context, task)
                )
                action = "result_unknown"
            elif int(task.attempt or 0) < self.spec.max_attempts:
                task.status = self.spec.queued_status
                task.progress_stage = self.spec.queued_stage
                task.updated_at = context.now
                result.pending_outbox.extend(self._stage_requeue_events(context, task))
                action = "requeued"
            else:
                result.pending_outbox.append(await self._apply_timeout(context, task))
                action = "timed_out"
            result.touched += 1
            reconciliation_rows_total.labels(
                domain=self.name,
                action=action,
            ).inc()
        return result


GENERATION_RECONCILER = TaskDomainReconciler(
    TaskDomainSpec(
        name="generation",
        model=Generation,
        active_statuses=(
            GenerationStatus.QUEUED.value,
            GenerationStatus.RUNNING.value,
        ),
        max_attempts=5,
        queued_status=GenerationStatus.QUEUED.value,
        queued_stage=GenerationStage.QUEUED.value,
        failed_status=GenerationStatus.FAILED.value,
        finalizing_stage=GenerationStage.FINALIZING.value,
        requeued_event=EV_GEN_REQUEUED,
        failed_event=EV_GEN_FAILED,
        release_method="release_generation",
        settle_unknown_method="settle_generation_unknown_upstream",
        id_field="generation",
    )
)

COMPLETION_RECONCILER = TaskDomainReconciler(
    TaskDomainSpec(
        name="completion",
        model=Completion,
        active_statuses=(
            CompletionStatus.QUEUED.value,
            CompletionStatus.STREAMING.value,
        ),
        max_attempts=3,
        queued_status=CompletionStatus.QUEUED.value,
        queued_stage=CompletionStage.QUEUED.value,
        failed_status=CompletionStatus.FAILED.value,
        finalizing_stage=CompletionStage.FINALIZING.value,
        requeued_event=EV_COMP_REQUEUED,
        failed_event=EV_COMP_FAILED,
        release_method="release_completion",
        settle_unknown_method="settle_completion_unknown_upstream",
        id_field="completion",
    )
)
