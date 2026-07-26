"""Generation and completion reconciliation domain implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

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
from lumen_core.upstream_billing import has_upstream_response_receipt

from .contracts import LeaseState, ReconcileContext, ReconcileResult
from .lease import read_lease_state
from .metrics import reconciliation_rows_total

RECON_STUCK_AFTER = timedelta(minutes=5)
RECON_TIMEOUT_CODE = "timeout"
RECON_TIMEOUT_MESSAGE = "task stuck; reconciler timed out"
EV_GEN_REQUEUED = "generation.requeued"
EV_COMP_REQUEUED = "completion.requeued"


def _aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        task_payload = {
            "task_id": task_id,
            "user_id": task.user_id,
            "kind": self.spec.name,
            "source": "stuck_task_reconciler",
        }
        data = {
            f"{self.spec.id_field}_id": task_id,
            "message_id": task.message_id,
            "attempt": attempt,
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
    ) -> tuple[str, str, dict[str, Any]]:
        data = {
            f"{self.spec.id_field}_id": task.id,
            "message_id": task.message_id,
            "code": RECON_TIMEOUT_CODE,
            "message": RECON_TIMEOUT_MESSAGE,
            "retriable": False,
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

    async def _settle_timeout_billing(
        self,
        context: ReconcileContext,
        task: Any,
    ) -> None:
        # attempt/status only prove that a worker claimed the row. They do not
        # prove the HTTP request reached the provider. Charge only when the
        # runner persisted a positive upstream response receipt.
        if has_upstream_response_receipt(task):
            method = self.spec.settle_unknown_method
            kwargs = {"knowledge": "unknown"}
        else:
            method = self.spec.release_method
            kwargs = {}
        await getattr(context.billing, method)(
            context.session,
            task,
            reason=RECON_TIMEOUT_CODE,
            **kwargs,
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

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        cutoff = context.now - RECON_STUCK_AFTER
        query = (
            select(self.spec.model)
            .where(
                self.spec.model.status.in_(self.spec.active_statuses),
                self.spec.model.updated_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        rows = list((await context.session.execute(query)).scalars())
        result = ReconcileResult()
        for task in rows:
            if not self._eligible(task, cutoff):
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="ineligible",
                ).inc()
                continue
            lease_state = await read_lease_state(
                context.redis,
                task.id,
                kind=self.name,
                unknowns=context.lease_unknowns,
                heartbeat_at=task.updated_at,
                now=context.now,
            )
            if lease_state is not LeaseState.EXPIRED:
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action=f"skip_{lease_state.value}",
                ).inc()
                continue

            if int(task.attempt or 0) < self.spec.max_attempts:
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
