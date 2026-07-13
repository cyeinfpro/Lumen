"""Immutable Canvas version snapshots."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasDocument,
    CanvasNodeSelection,
    CanvasVersion,
)

from .core_adapter import stable_hash, validated_graph
from .asset_ref_service import materialize_version_asset_refs, sync_head_asset_refs
from .document_service import get_owned_canvas
from .errors import canvas_http


def selection_dict(row: CanvasNodeSelection) -> dict[str, Any]:
    return {
        "node_id": row.node_id,
        "execution_id": row.execution_id,
        "output_index": int(row.output_index),
        "revision": int(row.revision),
        "locked": bool(row.locked),
    }


def version_dict(row: CanvasVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "canvas_id": row.canvas_id,
        "source_revision": int(row.source_revision),
        "version_no": int(row.version_no),
        "kind": row.kind,
        "name": row.name,
        "graph_schema_version": int(row.graph_schema_version),
        "graph_hash": row.graph_hash,
        "selection_hash": row.selection_hash,
        "created_at": row.created_at,
    }


async def load_selection_snapshot(
    db: AsyncSession,
    *,
    canvas_id: str,
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(CanvasNodeSelection.canvas_id == canvas_id)
                .order_by(CanvasNodeSelection.node_id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {"selections": [selection_dict(row) for row in rows]}


async def create_version(
    db: AsyncSession,
    *,
    canvas: CanvasDocument,
    user_id: str,
    kind: str,
    name: str | None = None,
    reuse_exact: bool = False,
) -> CanvasVersion:
    graph = validated_graph(canvas.graph_jsonb)
    selections = await load_selection_snapshot(db, canvas_id=canvas.id)
    graph_hash = stable_hash(graph)
    selection_hash = stable_hash(selections)
    if reuse_exact:
        existing = (
            await db.execute(
                select(CanvasVersion)
                .where(
                    CanvasVersion.canvas_id == canvas.id,
                    CanvasVersion.user_id == user_id,
                    CanvasVersion.source_revision == canvas.revision,
                    CanvasVersion.graph_hash == graph_hash,
                    CanvasVersion.selection_hash == selection_hash,
                )
                .order_by(CanvasVersion.version_no.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    latest_no = (
        await db.execute(
            select(func.max(CanvasVersion.version_no)).where(
                CanvasVersion.canvas_id == canvas.id
            )
        )
    ).scalar_one_or_none()
    row = CanvasVersion(
        canvas_id=canvas.id,
        user_id=user_id,
        source_revision=canvas.revision,
        version_no=int(latest_no or 0) + 1,
        kind=kind,
        name=name,
        graph_schema_version=canvas.graph_schema_version,
        graph_hash=graph_hash,
        graph_jsonb=graph,
        selection_snapshot_jsonb=selections,
        selection_hash=selection_hash,
    )
    db.add(row)
    await db.flush()
    await materialize_version_asset_refs(
        db,
        version=row,
        user_id=user_id,
        graph=graph,
        selection_snapshot=selections,
    )
    canvas.last_version_id = row.id
    return row


async def list_versions(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    rows = (
        (
            await db.execute(
                select(CanvasVersion)
                .where(
                    CanvasVersion.canvas_id == canvas_id,
                    CanvasVersion.user_id == user_id,
                )
                .order_by(CanvasVersion.version_no.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [version_dict(row) for row in rows]


async def create_named_version(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    name: str,
) -> CanvasVersion:
    canvas = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    row = await create_version(
        db,
        canvas=canvas,
        user_id=user_id,
        kind="named",
        name=name.strip(),
    )
    await db.commit()
    await db.refresh(row)
    return row


async def restore_version(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    version_id: str,
) -> CanvasVersion:
    canvas = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    source = (
        await db.execute(
            select(CanvasVersion).where(
                CanvasVersion.id == version_id,
                CanvasVersion.canvas_id == canvas_id,
                CanvasVersion.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise canvas_http("not_found", "canvas version not found", 404)

    graph = validated_graph(source.graph_jsonb)
    canvas.graph_jsonb = graph
    canvas.graph_schema_version = source.graph_schema_version
    canvas.revision = int(canvas.revision) + 1
    await sync_head_asset_refs(
        db,
        canvas_id=canvas.id,
        user_id=user_id,
        graph=graph,
    )

    current_selections = list(
        (
            await db.execute(
                select(CanvasNodeSelection)
                .where(CanvasNodeSelection.canvas_id == canvas_id)
                .with_for_update()
            )
        ).scalars()
    )
    current_revisions = {
        row.node_id: int(row.revision) for row in current_selections
    }
    await db.execute(
        delete(CanvasNodeSelection).where(CanvasNodeSelection.canvas_id == canvas_id)
    )
    snapshot = source.selection_snapshot_jsonb or {}
    items = snapshot.get("selections") if isinstance(snapshot, dict) else []
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("node_id"), str):
            continue
        db.add(
            CanvasNodeSelection(
                canvas_id=canvas_id,
                node_id=item["node_id"],
                execution_id=item.get("execution_id"),
                output_index=int(item.get("output_index") or 0),
                revision=max(
                    int(item.get("revision") or 0),
                    current_revisions.get(item["node_id"], 0),
                )
                + 1,
                locked=bool(item.get("locked")),
            )
        )
    restored = await create_version(
        db,
        canvas=canvas,
        user_id=user_id,
        kind="restore",
        name=source.name,
    )
    await db.commit()
    await db.refresh(restored)
    return restored
