"""Revision-conditional Canvas mutation application."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasDocument,
    CanvasMutation,
    CanvasNodeSelection,
)

from .api_schemas import CanvasMutationIn
from .asset_ref_service import sync_head_asset_refs
from .core_adapter import apply_graph_operations, stable_hash
from .document_service import get_owned_canvas
from .errors import canvas_http, idempotency_conflict


async def _lock_mutation_key(
    db: AsyncSession,
    *,
    canvas_id: str,
    client_id: str,
    mutation_id: str,
) -> None:
    connection = getattr(db, "connection", None)
    if connection is None:
        return
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return
    key = f"canvas-mutation:{canvas_id}:{client_id}:{mutation_id}"
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


async def _existing_mutation(
    db: AsyncSession,
    *,
    canvas_id: str,
    client_id: str,
    mutation_id: str,
) -> CanvasMutation | None:
    return (
        await db.execute(
            select(CanvasMutation).where(
                CanvasMutation.canvas_id == canvas_id,
                CanvasMutation.client_id == client_id,
                CanvasMutation.mutation_id == mutation_id,
            )
        )
    ).scalar_one_or_none()


def _replay_response(
    row: CanvasMutation,
    *,
    body: CanvasMutationIn,
) -> dict[str, Any]:
    existing_hash = stable_hash(
        {
            "base_revision": int(row.base_revision),
            "operations": row.operations_jsonb,
        }
    )
    request_hash = stable_hash(
        {
            "base_revision": body.base_revision,
            "operations": body.operations,
        }
    )
    if existing_hash != request_hash:
        raise idempotency_conflict("mutation id was already used for another request")
    response = row.response_jsonb or {}
    if isinstance(response, dict) and response:
        return response
    return {
        "revision": int(row.result_revision),
        "updated_at": row.created_at,
        "replayed": True,
    }


async def _revision_conflict_details(
    db: AsyncSession,
    *,
    canvas: CanvasDocument,
    client_revision: int,
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(CanvasMutation)
                .where(
                    CanvasMutation.canvas_id == canvas.id,
                    CanvasMutation.result_revision > client_revision,
                    CanvasMutation.result_revision <= canvas.revision,
                )
                .order_by(CanvasMutation.result_revision.asc())
            )
        )
        .scalars()
        .all()
    )
    expected = list(range(client_revision + 1, int(canvas.revision) + 1))
    actual = [int(row.result_revision) for row in rows]
    details: dict[str, Any] = {
        "base_revision": client_revision,
        "current_revision": int(canvas.revision),
        "updated_at": canvas.updated_at,
    }
    if actual == expected:
        details["remote_mutations"] = [
            {
                "base_revision": int(row.base_revision),
                "result_revision": int(row.result_revision),
                "operation_schema_version": int(row.operation_schema_version),
                "operations": row.operations_jsonb,
            }
            for row in rows
        ]
        details["rebase_unavailable"] = False
    else:
        details["remote_mutations"] = []
        details["rebase_unavailable"] = True
        details["snapshot"] = {
            "revision": int(canvas.revision),
            "graph_schema_version": int(canvas.graph_schema_version),
            "graph": canvas.graph_jsonb,
        }
    return details


async def apply_mutation(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    body: CanvasMutationIn,
    header_idempotency_key: str | None,
) -> dict[str, Any]:
    if header_idempotency_key != body.mutation_id:
        raise canvas_http(
            "idempotency_key_mismatch",
            "Idempotency-Key must match mutation_id",
            422,
        )
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    await _lock_mutation_key(
        db,
        canvas_id=canvas_id,
        client_id=body.client_id,
        mutation_id=body.mutation_id,
    )
    existing = await _existing_mutation(
        db,
        canvas_id=canvas_id,
        client_id=body.client_id,
        mutation_id=body.mutation_id,
    )
    if existing is not None:
        return _replay_response(existing, body=body)

    canvas = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    existing = await _existing_mutation(
        db,
        canvas_id=canvas_id,
        client_id=body.client_id,
        mutation_id=body.mutation_id,
    )
    if existing is not None:
        return _replay_response(existing, body=body)
    if int(canvas.revision) != body.base_revision:
        details = await _revision_conflict_details(
            db,
            canvas=canvas,
            client_revision=body.base_revision,
        )
        raise canvas_http(
            "canvas_revision_conflict",
            "canvas revision changed",
            409,
            **details,
        )

    graph = apply_graph_operations(canvas.graph_jsonb, body.operations)
    result_revision = body.base_revision + 1
    now = datetime.now(timezone.utc)
    response: dict[str, Any] = {
        "revision": result_revision,
        "updated_at": now,
    }
    row = CanvasMutation(
        canvas_id=canvas.id,
        user_id=user_id,
        client_id=body.client_id,
        mutation_id=body.mutation_id,
        operation_schema_version=max(
            [
                int(item.get("operation_schema_version") or 1)
                for item in body.operations
            ],
            default=1,
        ),
        base_revision=body.base_revision,
        result_revision=result_revision,
        operations_jsonb=body.operations,
        response_jsonb={
            "revision": result_revision,
            "updated_at": now.isoformat(),
        },
    )
    canvas.graph_jsonb = graph
    canvas.graph_schema_version = int(graph.get("schema_version") or 1)
    canvas.revision = result_revision
    await sync_head_asset_refs(
        db,
        canvas_id=canvas.id,
        user_id=user_id,
        graph=graph,
    )
    removed_node_ids = {
        node_id
        for item in body.operations
        if item.get("op") == "remove_nodes"
        for node_id in item.get("node_ids") or []
        if isinstance(node_id, str)
    }
    if removed_node_ids:
        await db.execute(
            delete(CanvasNodeSelection).where(
                CanvasNodeSelection.canvas_id == canvas.id,
                CanvasNodeSelection.node_id.in_(removed_node_ids),
            )
        )
    db.add(row)
    await db.commit()
    return response
