"""Administrator outbox dead-letter management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Completion,
    Generation,
    OutboxDeadLetter,
    OutboxEvent,
    User,
    VideoGeneration,
    WorkflowRun,
)


class DlqItemOut(BaseModel):
    id: str
    outbox_id: str | None
    event_type: str
    payload: dict[str, Any]
    error_class: str | None
    error_message: str | None
    retry_count: int
    failed_at: datetime
    resolved_at: datetime | None


DlqTaskKind = Literal[
    "generation",
    "completion",
    "video_generation",
    "storyboard_assembly",
]
DlqKind = DlqTaskKind | Literal["sse"]

DLQ_KIND_BY_EVENT_TYPE = MappingProxyType(
    {
        "outbox.generation": "generation",
        "outbox.completion": "completion",
        "outbox.video_generation": "video_generation",
        "outbox.storyboard_assembly": "storyboard_assembly",
        "outbox.sse": "sse",
    }
)


@dataclass(frozen=True)
class DlqRouteDependencies:
    http_error: Callable[..., Exception]
    write_admin_audit: Callable[..., Awaitable[Any]]
    logger: Any


async def _dlq_task_exists(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_id: str,
) -> bool:
    if kind == "generation":
        statement = select(Generation.id).join(User, User.id == Generation.user_id)
    elif kind == "completion":
        statement = select(Completion.id).join(User, User.id == Completion.user_id)
    elif kind == "video_generation":
        statement = select(VideoGeneration.id).join(
            User,
            User.id == VideoGeneration.user_id,
        )
    else:
        statement = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(WorkflowRun.type == "storyboard")
        )
    exists = (
        await db.execute(
            statement.where(
                statement.selected_columns[0] == task_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return exists is not None


async def _soft_deleted_dlq_task_ids(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_ids: set[str],
) -> set[str]:
    if not task_ids:
        return set()
    if kind == "generation":
        statement = (
            select(Generation.id)
            .join(User, User.id == Generation.user_id)
            .where(
                Generation.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "completion":
        statement = (
            select(Completion.id)
            .join(User, User.id == Completion.user_id)
            .where(
                Completion.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "video_generation":
        statement = (
            select(VideoGeneration.id)
            .join(User, User.id == VideoGeneration.user_id)
            .where(
                VideoGeneration.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    else:
        statement = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(
                WorkflowRun.id.in_(task_ids),
                WorkflowRun.type == "storyboard",
                User.deleted_at.is_not(None),
            )
        )
    return set((await db.execute(statement)).scalars())


async def _soft_deleted_dlq_row_ids(
    db: AsyncSession,
    rows: list[OutboxDeadLetter],
) -> set[str]:
    task_rows_by_kind: dict[DlqTaskKind, dict[str, set[str]]] = {}
    sse_rows_by_user: dict[str, set[str]] = {}
    for row in rows:
        kind = DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
        if kind is None:
            continue
        payload = dict(row.payload or {})
        if kind == "sse":
            user_id = payload.get("user_id")
            if isinstance(user_id, str) and user_id:
                sse_rows_by_user.setdefault(user_id, set()).add(row.id)
            continue
        task_id = payload.get("task_id") or payload.get("id")
        if isinstance(task_id, str) and task_id:
            task_rows_by_kind.setdefault(kind, {}).setdefault(task_id, set()).add(
                row.id
            )

    row_ids: set[str] = set()
    for kind, rows_by_task in task_rows_by_kind.items():
        deleted_task_ids = await _soft_deleted_dlq_task_ids(
            db,
            kind=kind,
            task_ids=set(rows_by_task),
        )
        for task_id in deleted_task_ids:
            row_ids.update(rows_by_task[task_id])
    if sse_rows_by_user:
        deleted_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(sse_rows_by_user),
                        User.deleted_at.is_not(None),
                    )
                )
            ).scalars()
        )
        for user_id in deleted_user_ids:
            row_ids.update(sse_rows_by_user[user_id])
    return row_ids


async def list_dlq(
    *,
    db: AsyncSession,
    limit: int,
    include_resolved: bool,
) -> dict[str, Any]:
    statement = select(OutboxDeadLetter)
    if not include_resolved:
        statement = statement.where(OutboxDeadLetter.resolved_at.is_(None))
    statement = statement.order_by(desc(OutboxDeadLetter.failed_at)).limit(limit)
    rows = list((await db.execute(statement)).scalars())
    items = [
        DlqItemOut(
            id=row.id,
            outbox_id=row.outbox_id,
            event_type=row.event_type,
            payload=dict(row.payload or {}),
            error_class=row.error_class,
            error_message=row.error_message,
            retry_count=row.retry_count,
            failed_at=row.failed_at,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]
    return {"items": items, "total": len(items)}


async def _load_dlq_retry_row(
    db: AsyncSession,
    dlq_id: str,
    *,
    deps: DlqRouteDependencies,
) -> OutboxDeadLetter:
    row = (
        await db.execute(
            select(OutboxDeadLetter)
            .where(OutboxDeadLetter.id == dlq_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not row:
        raise deps.http_error("not_found", "dlq item not found", 404)
    if row.resolved_at is not None:
        raise deps.http_error("already_resolved", "dlq item already resolved", 409)
    return row


def _validate_dlq_retry_row(
    row: OutboxDeadLetter,
    *,
    deps: DlqRouteDependencies,
) -> tuple[DlqKind, dict[str, Any]]:
    kind = DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
    if kind is None:
        raise deps.http_error(
            "unsupported_event_type",
            f"DLQ retry does not support {row.event_type}",
            422,
        )
    if row.error_class not in {"OutboxEnqueueFailed", "OutboxPublishFailed"}:
        raise deps.http_error(
            "unrepairable_dlq_payload",
            "malformed or invalid outbox payload must be repaired before retry",
            422,
        )
    return kind, dict(row.payload or {})


async def _validate_dlq_retry_owner(
    db: AsyncSession,
    *,
    row: OutboxDeadLetter,
    kind: DlqKind,
    payload: dict[str, Any],
    dlq_id: str,
    deps: DlqRouteDependencies,
) -> str | None:
    task_id = payload.get("task_id") or payload.get("id")
    if kind == "sse":
        user_id = payload.get("user_id")
        valid = (
            isinstance(user_id, str)
            and bool(user_id)
            and isinstance(payload.get("channel"), str)
            and bool(payload.get("channel"))
            and isinstance(payload.get("event_name"), str)
            and bool(payload.get("event_name"))
            and isinstance(payload.get("data"), dict)
        )
        if not valid:
            raise deps.http_error(
                "invalid_payload",
                "DLQ SSE payload is invalid",
                400,
            )
        exists = (
            await db.execute(
                select(User.id).where(
                    User.id == user_id,
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        task_id = user_id
    else:
        if not isinstance(task_id, str) or not task_id:
            raise deps.http_error(
                "invalid_task_id",
                "dlq payload task_id is invalid",
                400,
            )
        exists = await _dlq_task_exists(db, kind=kind, task_id=task_id)
    if exists:
        return str(task_id)
    deps.logger.info(
        "dlq retry skipped: task_or_user_missing dlq_id=%s task_id=%s event_type=%s",
        dlq_id,
        task_id,
        row.event_type,
    )
    raise deps.http_error(
        "task_not_found",
        "dlq payload references an unknown task or deleted user",
        404,
    )


async def _prepare_dlq_outbox(
    db: AsyncSession,
    *,
    row: OutboxDeadLetter,
    kind: DlqKind,
    payload: dict[str, Any],
    deps: DlqRouteDependencies,
) -> OutboxEvent:
    outbox = None
    if row.outbox_id:
        outbox = (
            await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == row.outbox_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if outbox is None:
        outbox = OutboxEvent(kind=kind, payload={}, published_at=None)
        db.add(outbox)
        await db.flush()
        row.outbox_id = outbox.id
    elif outbox.kind != kind:
        raise deps.http_error(
            "outbox_kind_mismatch",
            "DLQ event type does not match its outbox row",
            409,
        )
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = payload
    outbox.published_at = None
    row.retry_count = (row.retry_count or 0) + 1
    row.error_message = "retry scheduled via durable outbox"
    return outbox


async def retry_dlq(
    *,
    dlq_id: str,
    request: Request,
    admin: Any,
    db: AsyncSession,
    deps: DlqRouteDependencies,
) -> dict[str, Any]:
    row = await _load_dlq_retry_row(db, dlq_id, deps=deps)
    kind, payload = _validate_dlq_retry_row(row, deps=deps)
    task_id = await _validate_dlq_retry_owner(
        db,
        row=row,
        kind=kind,
        payload=payload,
        dlq_id=dlq_id,
        deps=deps,
    )
    outbox = await _prepare_dlq_outbox(
        db,
        row=row,
        kind=kind,
        payload=payload,
        deps=deps,
    )
    await deps.write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.retry",
        details={
            "dlq_id": dlq_id,
            "event_type": row.event_type,
            "requeued": True,
            "task_id": task_id,
            "outbox_id": outbox.id,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "dlq_id": dlq_id,
        "requeued": True,
        "resolved": False,
        "outbox_id": outbox.id,
    }


async def sweep_dlq_for_deleted_users(
    *,
    request: Request,
    admin: Any,
    db: AsyncSession,
    limit: int,
    deps: DlqRouteDependencies,
) -> dict[str, Any]:
    swept_ids: list[str] = []
    scanned = 0
    now = datetime.now(timezone.utc)
    cursor: tuple[datetime, str] | None = None
    while True:
        statement = select(OutboxDeadLetter).where(
            OutboxDeadLetter.resolved_at.is_(None),
            OutboxDeadLetter.event_type.in_(tuple(DLQ_KIND_BY_EVENT_TYPE)),
        )
        if cursor is not None:
            failed_at, dlq_id = cursor
            statement = statement.where(
                or_(
                    OutboxDeadLetter.failed_at > failed_at,
                    and_(
                        OutboxDeadLetter.failed_at == failed_at,
                        OutboxDeadLetter.id > dlq_id,
                    ),
                )
            )
        rows = list(
            (
                await db.execute(
                    statement.order_by(
                        OutboxDeadLetter.failed_at.asc(),
                        OutboxDeadLetter.id.asc(),
                    ).limit(limit)
                )
            ).scalars()
        )
        if not rows:
            break
        scanned += len(rows)
        deleted_owner_row_ids = await _soft_deleted_dlq_row_ids(db, rows)
        for row in rows:
            if row.id not in deleted_owner_row_ids:
                continue
            row.resolved_at = now
            row.error_message = (
                (row.error_message or "") + " | swept: owner soft-deleted"
            ).strip(" |")
            swept_ids.append(row.id)
        cursor = (rows[-1].failed_at, rows[-1].id)
        if len(rows) < limit:
            break

    await deps.write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.sweep_deleted_users",
        details={"swept": len(swept_ids), "scanned": scanned},
    )
    await db.commit()
    deps.logger.info(
        "dlq sweep deleted-users admin=%s swept=%d scanned=%d",
        admin.id,
        len(swept_ids),
        scanned,
    )
    return {"ok": True, "swept": len(swept_ids), "scanned": scanned}
