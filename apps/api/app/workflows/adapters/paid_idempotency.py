"""SQLAlchemy adapter for durable paid workflow idempotency."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.media_workflows import WorkflowRun

from ...idempotency.advisory import lock_user_key
from ..application.paid_idempotency import (
    PaidOperationRequest,
    ensure_matching_operation,
    operation_record,
    paid_operation_task_metadata,
    record_paid_operation_metadata,
)


_PAID_OPERATION_SESSION_INFO_KEY = "paid_workflow_operation"


@dataclass(slots=True)
class SQLAlchemyPaidOperationPort:
    db: AsyncSession

    async def lock(self, request: PaidOperationRequest) -> None:
        await lock_user_key(
            self.db,
            "paid-workflow-operation",
            request.user_id,
            request.idempotency_key,
        )

    async def find(self, request: PaidOperationRequest) -> WorkflowRun | None:
        rows = list(
            (
                await self.db.execute(
                    select(WorkflowRun).where(WorkflowRun.user_id == request.user_id)
                )
            )
            .scalars()
            .all()
        )
        matched: WorkflowRun | None = None
        for run in rows:
            record = operation_record(
                run.metadata_jsonb,
                request.client_key_hash,
            )
            if record is None:
                continue
            ensure_matching_operation(record, request)
            matched = matched or run
        return matched

    def bind(self, request: PaidOperationRequest) -> None:
        existing = self.db.info.get(_PAID_OPERATION_SESSION_INFO_KEY)
        if existing is not None and existing != request:
            raise RuntimeError("database session already has a paid workflow operation")
        self.db.info[_PAID_OPERATION_SESSION_INFO_KEY] = request

    def clear(self, request: PaidOperationRequest) -> None:
        if self.db.info.get(_PAID_OPERATION_SESSION_INFO_KEY) is request:
            self.db.info.pop(_PAID_OPERATION_SESSION_INFO_KEY, None)

    async def rollback(self) -> None:
        await self.db.rollback()

    def is_integrity_error(self, exc: Exception) -> bool:
        return isinstance(exc, IntegrityError)


def _current_paid_operation(
    db: AsyncSession,
) -> PaidOperationRequest | None:
    session_info = getattr(db, "info", None)
    if not isinstance(session_info, dict):
        return None
    request = session_info.get(_PAID_OPERATION_SESSION_INFO_KEY)
    return request if isinstance(request, PaidOperationRequest) else None


def record_current_paid_operation(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    request = _current_paid_operation(db)
    if request is not None:
        run.metadata_jsonb = record_paid_operation_metadata(
            run.metadata_jsonb,
            request,
        )


def current_paid_operation_task_metadata(
    db: AsyncSession,
) -> dict[str, str]:
    request = _current_paid_operation(db)
    return {} if request is None else paid_operation_task_metadata(request)


__all__ = [
    "SQLAlchemyPaidOperationPort",
    "current_paid_operation_task_metadata",
    "record_current_paid_operation",
]
