"""Generation and completion reconciliation domain implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select

from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_FAILED,
    EV_GEN_FAILED,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    task_channel,
    user_channel,
)
from lumen_core.models import Completion, Generation, Message
from lumen_core.upstream_billing import (
    upstream_dispatch_result_unknown,
)

from ..completion_checkpoint import (
    COMPLETION_CHECKPOINT_QUARANTINED,
    COMPLETION_CHECKPOINT_STATE_KEY,
    apply_completed_checkpoint,
    completion_checkpoint_has_no_usable_output,
    completion_checkpoint_requires_recovery,
    completion_checkpoint_validation_error,
    completion_execution_epoch as completion_execution_epoch,
    completion_has_completed_checkpoint,
    completion_has_trustworthy_persisted_usage,
    recover_completion_checkpoint_images,
)
from ..completion_checkpoint_payloads import CompletionCheckpointCorrupt
from ..completion_execution_settlement import (
    COMPLETION_RESULT_UNKNOWN_CODE as COMPLETION_RESULT_UNKNOWN_CODE,
    COMPLETION_RESULT_UNKNOWN_MESSAGE as COMPLETION_RESULT_UNKNOWN_MESSAGE,
    CompletionDispatchResultUnknown as CompletionDispatchResultUnknown,
    completion_cancel_requires_unknown_settlement as completion_cancel_requires_unknown_settlement,
    ensure_completion_execution_current as ensure_completion_execution_current,
    raise_completion_dispatch_failure as _raise_completion_dispatch_failure,
    record_completion_upstream_marker as _record_completion_upstream_marker,
    settle_completion_actual_or_unknown,
    settle_completion_cancel_unknown as settle_completion_cancel_unknown,
    settle_completion_result_unknown as settle_completion_result_unknown,
    stage_completion_preflight_failure as stage_completion_preflight_failure,
)
from ..completion_tool_image_runtime import build_completion_tool_image_service
from .completion_execution_fence import (
    bind_completion_execution_fence as bind_completion_execution_fence,
)
from .contracts import LeaseState, ReconcileContext, ReconcileResult
from .lease import read_lease_states
from .metrics import reconciliation_rows_total
from .upstream_evidence import (
    dispatch_cost_requires_settlement as _dispatch_cost_requires_settlement,
    generation_has_takeover_checkpoint as _generation_has_takeover_checkpoint,
    generation_sidecar_cost_requires_settlement as _generation_sidecar_cost_requires_settlement,
)

RECON_STUCK_AFTER = timedelta(minutes=5)
RECON_TIMEOUT_CODE = "timeout"
RECON_TIMEOUT_MESSAGE = "task stuck; reconciler timed out"
RECON_RESULT_UNKNOWN_CODE = "result_unknown"
RECON_RESULT_UNKNOWN_MESSAGE = "upstream dispatch has no response receipt; result is unknown"
RECON_CANCEL_CODE = "cancelled"
RECON_CANCEL_MESSAGE = "cancelled by user"
COMPLETION_CHECKPOINT_CORRUPT_CODE = "completion_checkpoint_corrupt"
COMPLETION_CHECKPOINT_CORRUPT_MESSAGE = (
    "completion checkpoint image payload is corrupt and was quarantined"
)
EV_GEN_REQUEUED = "generation.requeued"
EV_COMP_REQUEUED = "completion.requeued"
RECON_BATCH_LIMIT = 100


async def _recover_completion_checkpoint(context: ReconcileContext, completion: Any) -> bool:
    return await recover_completion_checkpoint_images(
        completion,
        redis=context.redis,
        channel=task_channel(str(completion.id)),
        tool_image_service=build_completion_tool_image_service(),
    )


def _aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def record_completion_upstream_marker(
    state: Any,
    *,
    response_received: bool,
    proven_undelivered: bool = False,
    proven_no_cost: bool = False,
) -> None:
    await _record_completion_upstream_marker(
        state,
        response_received=response_received,
        proven_undelivered=proven_undelivered,
        proven_no_cost=proven_no_cost,
    )


async def raise_completion_dispatch_failure(state: Any, exc: Exception) -> None:
    await _raise_completion_dispatch_failure(
        state,
        exc,
        record_marker=record_completion_upstream_marker,
    )


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

    def _candidate_eligible(self, task: Any, cutoff: datetime) -> bool:
        return str(task.status) in self.spec.active_statuses and (
            getattr(task, "cancel_requested_at", None) is not None
            or self._eligible(task, cutoff)
        )

    def _candidate_query(
        self,
        cutoff: datetime,
        cursor: tuple[datetime, str] | None,
    ) -> Any:
        model = self.spec.model
        query = select(model).where(
            model.status.in_(self.spec.active_statuses),
            or_(
                model.cancel_requested_at.is_not(None),
                model.updated_at < cutoff,
            ),
        )
        if cursor is not None:
            cursor_updated_at, cursor_id = cursor
            query = query.where(
                or_(
                    model.updated_at > cursor_updated_at,
                    and_(
                        model.updated_at == cursor_updated_at,
                        model.id > cursor_id,
                    ),
                )
            )
        return query.order_by(model.updated_at.asc(), model.id.asc()).limit(
            RECON_BATCH_LIMIT
        )

    async def _lock_candidate(
        self,
        context: ReconcileContext,
        candidate: Any,
        cutoff: datetime,
        *,
        allow_fresh: bool = False,
    ) -> Any | None:
        model = self.spec.model
        conditions = [
            model.id == str(candidate.id),
            model.status.in_(self.spec.active_statuses),
        ]
        if not allow_fresh:
            conditions.append(
                or_(
                    model.cancel_requested_at.is_not(None),
                    model.updated_at < cutoff,
                )
            )
        query = (
            select(model)
            .where(*conditions)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        rows = list((await context.session.execute(query)).scalars())
        task = next(
            (row for row in rows if str(row.id) == str(candidate.id)),
            None,
        )
        if task is None or (
            not allow_fresh and not self._candidate_eligible(task, cutoff)
        ):
            return None
        lease_state = (
            await read_lease_states(
                context.redis,
                [task],
                kind=self.name,
                unknowns=context.lease_unknowns,
                now=context.now,
            )
        )[str(task.id)]
        if lease_state is not LeaseState.EXPIRED:
            cancel_requested = getattr(task, "cancel_requested_at", None) is not None
            action = "skip_cancel" if cancel_requested else "skip"
            reconciliation_rows_total.labels(
                domain=self.name,
                action=f"{action}_{lease_state.value}",
            ).inc()
            return None
        return task

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
        if _dispatch_cost_requires_settlement(task) or (
            self.spec.name == "generation"
            and _generation_sidecar_cost_requires_settlement(task)
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
        await self._settle_timeout_billing(
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

    async def _apply_checkpoint_corruption(
        self,
        context: ReconcileContext,
        task: Any,
        *,
        validation_error: str,
    ) -> tuple[str, str, dict[str, Any]]:
        await self._settle_unknown_billing(
            context,
            task,
            reason=COMPLETION_CHECKPOINT_CORRUPT_CODE,
        )
        request = (
            dict(task.upstream_request)
            if isinstance(task.upstream_request, dict)
            else {}
        )
        request[COMPLETION_CHECKPOINT_STATE_KEY] = COMPLETION_CHECKPOINT_QUARANTINED
        request["completion_checkpoint_quarantine_reason"] = validation_error[:500]
        task.upstream_request = request
        task.status = self.spec.failed_status
        task.progress_stage = self.spec.finalizing_stage
        task.error_code = COMPLETION_CHECKPOINT_CORRUPT_CODE
        task.error_message = COMPLETION_CHECKPOINT_CORRUPT_MESSAGE
        task.finished_at = context.now
        task.updated_at = context.now
        message = await context.session.get(Message, task.message_id)
        if message is not None:
            message.status = MessageStatus.FAILED.value
        return self._stage_failure_event(
            context,
            task,
            code=COMPLETION_CHECKPOINT_CORRUPT_CODE,
            message=COMPLETION_CHECKPOINT_CORRUPT_MESSAGE,
        )

    async def _apply_checkpoint_no_output(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        code = EC.NO_TEXT_RETURNED.value
        message = "upstream returned empty completion"
        await self._settle_unknown_billing(
            context,
            task,
            reason=code,
        )
        task.status = self.spec.failed_status
        task.progress_stage = self.spec.finalizing_stage
        task.error_code = code
        task.error_message = message
        task.text = ""
        task.finished_at = context.now
        task.updated_at = context.now
        row = await context.session.get(Message, task.message_id)
        if row is not None:
            row.status = MessageStatus.FAILED.value
        return self._stage_failure_event(
            context,
            task,
            code=code,
            message=message,
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
            or _dispatch_cost_requires_settlement(task)
            or (
                self.spec.name == "generation"
                and _generation_sidecar_cost_requires_settlement(task)
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

    async def _recover_or_quarantine_completion_checkpoint(
        self,
        context: ReconcileContext,
        candidate: Any,
        cutoff: datetime,
    ) -> tuple[bool, tuple[str, str, dict[str, Any]] | None]:
        validation_error = completion_checkpoint_validation_error(candidate)
        if validation_error is not None:
            task = await self._lock_candidate(context, candidate, cutoff)
            if task is None:
                return False, None
            return False, await self._apply_checkpoint_corruption(
                context,
                task,
                validation_error=validation_error,
            )
        if not completion_checkpoint_requires_recovery(candidate):
            return False, None
        try:
            return await _recover_completion_checkpoint(context, candidate), None
        except CompletionCheckpointCorrupt as exc:
            task = await self._lock_candidate(
                context,
                candidate,
                cutoff,
                allow_fresh=True,
            )
            if task is None:
                return False, None
            return False, await self._apply_checkpoint_corruption(
                context,
                task,
                validation_error=str(exc),
            )

    async def _reconcile_expired_candidate(
        self,
        context: ReconcileContext,
        candidate: Any,
        cutoff: datetime,
    ) -> tuple[str, list[tuple[str, str, dict[str, Any]]]] | None:
        cancel_requested = getattr(candidate, "cancel_requested_at", None) is not None
        recovered_checkpoint = False
        checkpoint_failure = None
        if self.spec.name == "completion" and not cancel_requested:
            (
                recovered_checkpoint,
                checkpoint_failure,
            ) = await self._recover_or_quarantine_completion_checkpoint(
                context,
                candidate,
                cutoff,
            )
        if checkpoint_failure is not None:
            return "checkpoint_corrupt", [checkpoint_failure]
        task = await self._lock_candidate(
            context,
            candidate,
            cutoff,
            allow_fresh=recovered_checkpoint,
        )
        if task is None:
            return None
        if getattr(task, "cancel_requested_at", None) is not None:
            return "canceled", [await self._apply_cancel(context, task)]
        if not self._eligible(task, cutoff):
            reconciliation_rows_total.labels(
                domain=self.name,
                action="ineligible",
            ).inc()
            return None

        has_takeover_checkpoint = (
            self.spec.name == "generation"
            and _generation_has_takeover_checkpoint(task)
        )
        if (
            self.spec.name == "completion"
            and completion_checkpoint_has_no_usable_output(task)
        ):
            return "checkpoint_no_output", [
                await self._apply_checkpoint_no_output(context, task)
            ]
        if (
            self.spec.name == "completion"
            and completion_has_completed_checkpoint(task)
        ):
            return "completed_checkpoint", [
                await apply_completed_checkpoint(context, task)
            ]
        if (
            upstream_dispatch_result_unknown(task)
            and not has_takeover_checkpoint
        ):
            return "result_unknown", [
                await self._apply_result_unknown(context, task)
            ]
        if has_takeover_checkpoint or int(task.attempt or 0) < self.spec.max_attempts:
            task.status = self.spec.queued_status
            task.progress_stage = self.spec.queued_stage
            task.updated_at = context.now
            return "requeued", self._stage_requeue_events(context, task)
        return "timed_out", [await self._apply_timeout(context, task)]

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        cutoff = context.now - RECON_STUCK_AFTER
        result = ReconcileResult()
        cursor: tuple[datetime, str] | None = None
        # Redis owns lease state, so scan unlocked keyset pages until the
        # processing budget is full and lock only candidates that look expired.
        while result.touched < RECON_BATCH_LIMIT:
            rows = list(
                (
                    await context.session.execute(self._candidate_query(cutoff, cursor))
                ).scalars()
            )
            if not rows:
                break
            cursor = (_aware_utc(rows[-1].updated_at) or cutoff, str(rows[-1].id))
            candidates = [
                task for task in rows if self._candidate_eligible(task, cutoff)
            ]
            lease_states = await read_lease_states(
                context.redis,
                candidates,
                kind=self.name,
                unknowns=context.lease_unknowns,
                now=context.now,
            )
            for candidate in candidates:
                cancel_requested = (
                    getattr(candidate, "cancel_requested_at", None) is not None
                )
                lease_state = lease_states[str(candidate.id)]
                if lease_state is not LeaseState.EXPIRED:
                    action = "skip_cancel" if cancel_requested else "skip"
                    reconciliation_rows_total.labels(
                        domain=self.name,
                        action=f"{action}_{lease_state.value}",
                    ).inc()
                    continue
                outcome = await self._reconcile_expired_candidate(
                    context,
                    candidate,
                    cutoff,
                )
                if outcome is None:
                    continue
                action, pending_outbox = outcome
                result.pending_outbox.extend(pending_outbox)
                result.touched += 1
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action=action,
                ).inc()
                if result.touched >= RECON_BATCH_LIMIT:
                    break
            if len(rows) < RECON_BATCH_LIMIT:
                break
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
