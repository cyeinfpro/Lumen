"""Bounded execution history and replay-safe event batches."""

from __future__ import annotations

from datetime import timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import CanvasNodeExecution, CanvasRun, CanvasRunEvent

from .document_service import decode_cursor, get_owned_canvas
from .errors import canvas_http
from .run_serialization import (
    execution_dict,
    execution_tasks_by_execution,
    run_event_dict,
)


def _history_cursor(row: CanvasNodeExecution) -> str:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return f"{created.isoformat()}|{row.id}"


async def execution_history(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    node_id: str,
    cursor: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    if not 1 <= limit <= 100:
        raise canvas_http(
            "invalid_limit", "history limit must be between 1 and 100", 422
        )
    query = select(CanvasNodeExecution).where(
        CanvasNodeExecution.canvas_id == canvas_id,
        CanvasNodeExecution.user_id == user_id,
        CanvasNodeExecution.node_id == node_id,
    )
    decoded = decode_cursor(cursor)
    if decoded is not None:
        created_at, execution_id = decoded
        query = query.where(
            or_(
                CanvasNodeExecution.created_at < created_at,
                and_(
                    CanvasNodeExecution.created_at == created_at,
                    CanvasNodeExecution.id < execution_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                query.order_by(
                    CanvasNodeExecution.created_at.desc(), CanvasNodeExecution.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    page = rows[:limit]
    tasks = await execution_tasks_by_execution(db, page)
    items = []
    for row in page:
        item = execution_dict(row, tasks.get(row.id))
        item.update(
            {
                "config_snapshot": {
                    key: value
                    for key, value in (row.config_snapshot_jsonb or {}).items()
                    if key != "_canvas"
                },
                "input_snapshot": row.input_snapshot_jsonb or {},
                "definition_hash": row.definition_hash,
                "input_hash": row.input_hash,
                "processor_version": row.processor_version,
            }
        )
        items.append(item)
    return {
        "items": items,
        "next_cursor": _history_cursor(page[-1])
        if len(rows) > limit and page
        else None,
    }


def event_batch(
    events: list[dict[str, Any]],
    *,
    after_seq: int,
    last_event_seq: int,
) -> dict[str, Any]:
    expected = after_seq + 1
    gap = after_seq > last_event_seq
    for event in events:
        if event["seq"] != expected:
            gap = True
        expected = event["seq"] + 1
    if not events and after_seq < last_event_seq:
        gap = True
    cursor = events[-1]["seq"] if events else after_seq
    return {
        "items": events,
        "after_seq": after_seq,
        "next_after_seq": cursor,
        "last_event_seq": last_event_seq,
        "has_more": cursor < last_event_seq and not gap,
        "snapshot_required": gap,
    }


async def execution_event_batch(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    run_id: str,
    after_seq: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    if after_seq < 0 or not 1 <= limit <= 200:
        raise canvas_http("invalid_event_cursor", "invalid event cursor or limit", 422)
    run = (
        await db.execute(
            select(CanvasRun).where(
                CanvasRun.id == run_id,
                CanvasRun.canvas_id == canvas_id,
                CanvasRun.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise canvas_http("not_found", "canvas run not found", 404)
    high_water = int(run.last_event_seq)
    rows = list(
        (
            await db.execute(
                select(CanvasRunEvent)
                .where(
                    CanvasRunEvent.run_id == run_id,
                    CanvasRunEvent.seq > after_seq,
                    CanvasRunEvent.seq <= high_water,
                )
                .order_by(CanvasRunEvent.seq)
                .limit(limit)
            )
        ).scalars()
    )
    return event_batch(
        [run_event_dict(row) for row in rows],
        after_seq=after_seq,
        last_event_seq=high_water,
    )
