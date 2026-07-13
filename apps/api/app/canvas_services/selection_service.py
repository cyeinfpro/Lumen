"""Active output selection with revision CAS."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import CanvasNodeExecution, CanvasNodeSelection

from .api_schemas import CanvasSelectOutputIn
from ..db import affected_rows
from .document_service import get_owned_canvas
from .errors import canvas_http
from .version_service import selection_dict


async def select_execution_output(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    execution_id: str,
    body: CanvasSelectOutputIn,
) -> dict[str, Any]:
    canvas = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    execution = (
        await db.execute(
            select(CanvasNodeExecution).where(
                CanvasNodeExecution.id == execution_id,
                CanvasNodeExecution.canvas_id == canvas_id,
                CanvasNodeExecution.user_id == user_id,
                CanvasNodeExecution.status.in_(
                    ("succeeded", "partial_failed", "reused")
                ),
            )
        )
    ).scalar_one_or_none()
    if execution is None:
        raise canvas_http("not_found", "canvas execution not found", 404)
    graph = canvas.graph_jsonb if isinstance(canvas.graph_jsonb, dict) else {}
    current_node = next(
        (
            node
            for node in graph.get("nodes") or []
            if isinstance(node, dict) and node.get("id") == execution.node_id
        ),
        None,
    )
    current_node_type = (
        current_node.get("type") if isinstance(current_node, dict) else None
    )
    if current_node_type != execution.node_type:
        raise canvas_http(
            "canvas_execution_stale",
            "execution no longer matches the current canvas node",
            409,
            node_id=execution.node_id,
            execution_node_type=execution.node_type,
            current_node_type=current_node_type,
        )
    outputs = execution.outputs_jsonb or []
    if body.output_index >= len(outputs) or not isinstance(
        outputs[body.output_index], dict
    ):
        raise canvas_http(
            "canvas_output_not_found",
            "execution output does not exist",
            422,
            output_index=body.output_index,
        )

    selection = (
        await db.execute(
            select(CanvasNodeSelection)
            .where(
                CanvasNodeSelection.canvas_id == canvas_id,
                CanvasNodeSelection.node_id == execution.node_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if selection is None:
        if body.selection_revision != 0:
            raise canvas_http(
                "canvas_selection_conflict",
                "active output changed",
                409,
                expected_revision=body.selection_revision,
                current_revision=0,
            )
        bind = await db.connection()
        insert = sqlite_insert if bind.dialect.name == "sqlite" else pg_insert
        result = await db.execute(
            insert(CanvasNodeSelection)
            .values(
                canvas_id=canvas_id,
                node_id=execution.node_id,
                execution_id=execution.id,
                output_index=body.output_index,
                revision=1,
                locked=False,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CanvasNodeSelection.canvas_id,
                    CanvasNodeSelection.node_id,
                ]
            )
        )
        if not affected_rows(result):
            await db.rollback()
            raise canvas_http(
                "canvas_selection_conflict",
                "active output changed",
                409,
                expected_revision=0,
            )
    else:
        if int(selection.revision) != body.selection_revision:
            raise canvas_http(
                "canvas_selection_conflict",
                "active output changed",
                409,
                expected_revision=body.selection_revision,
                current_revision=int(selection.revision),
            )
        selection.execution_id = execution.id
        selection.output_index = body.output_index
        selection.revision = int(selection.revision) + 1
        selection.updated_at = datetime.now(timezone.utc)
    await db.commit()
    selection = (
        await db.execute(
            select(CanvasNodeSelection).where(
                CanvasNodeSelection.canvas_id == canvas_id,
                CanvasNodeSelection.node_id == execution.node_id,
            )
        )
    ).scalar_one()
    return selection_dict(selection)
