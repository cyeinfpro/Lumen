"""Persist ordered Canvas Run events."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import CanvasNodeExecution, CanvasRun, CanvasRunEvent


async def append_run_event(
    db: AsyncSession,
    *,
    run: CanvasRun,
    execution: CanvasNodeExecution | None,
    event_type: str,
    event_key: str,
    payload: dict[str, Any],
) -> CanvasRunEvent:
    run.last_event_seq = int(run.last_event_seq or 0) + 1
    row = CanvasRunEvent(
        run_id=run.id,
        seq=run.last_event_seq,
        execution_id=execution.id if execution is not None else None,
        event_type=event_type,
        event_key=event_key,
        payload_jsonb=payload,
    )
    db.add(row)
    await db.flush()
    return row
