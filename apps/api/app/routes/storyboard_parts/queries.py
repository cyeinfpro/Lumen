"""Storyboard read-side queries and response transactions."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import WorkflowRun

from ...services.storyboard.common import STORYBOARD_WORKFLOW_TYPE
from ...services.storyboard.contracts import StoryboardRunListOut, StoryboardRunOut
from ...services.storyboard.patching import decode_cursor, encode_cursor
from .runtime import StoryboardRuntime


async def list_storyboards(
    *,
    db: AsyncSession,
    user_id: str,
    cursor: str | None,
    limit: int,
    runtime: StoryboardRuntime,
) -> StoryboardRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user_id,
        WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
        WorkflowRun.deleted_at.is_(None),
    )
    decoded = decode_cursor(cursor)
    if decoded is not None:
        updated_at, row_id = decoded
        stmt = stmt.where(
            (WorkflowRun.updated_at < updated_at)
            | ((WorkflowRun.updated_at == updated_at) & (WorkflowRun.id < row_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    items = [await runtime.list_item_out(db, row) for row in page]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit and page else None
    await db.commit()
    return StoryboardRunListOut(items=items, next_cursor=next_cursor)


async def get_storyboard(
    *,
    db: AsyncSession,
    user_id: str,
    run_id: str,
    runtime: StoryboardRuntime,
) -> StoryboardRunOut:
    run = await runtime.get_run(db, user_id=user_id, run_id=run_id)
    out = await runtime.build_run_out(db, run)
    await db.commit()
    return out
