"""Canvas document CRUD and cursor pagination."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import (
    CanvasDocument,
    CanvasMutation,
    CanvasNodeExecution,
)

from ..idempotency.advisory import lock_user_key
from .api_schemas import CanvasCreateIn, CanvasDuplicateIn, CanvasPatchIn
from .asset_ref_service import delete_head_asset_refs, sync_head_asset_refs
from .core_adapter import stable_hash, validated_graph
from .errors import canvas_http, idempotency_conflict, not_found
from .identity_fence import lock_canvas_write_identity


_DOCUMENT_ID_NAMESPACE = uuid.UUID("1e6541e8-1f09-4bbd-a0de-308a893f464b")
_DOCUMENT_CREATE_CLIENT_ID = "__document_create__"
_DOCUMENT_DUPLICATE_CLIENT_ID = "__document_duplicate__"


def _idempotent_document_id(
    *,
    operation: str,
    user_id: str,
    idempotency_key: str,
) -> str:
    return str(
        uuid.uuid5(
            _DOCUMENT_ID_NAMESPACE,
            f"{operation}:{user_id}:{idempotency_key}",
        )
    )


async def _existing_idempotent_document(
    db: AsyncSession,
    *,
    user_id: str,
    document_id: str,
    client_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> CanvasDocument | None:
    row = (
        await db.execute(
            select(CanvasDocument).where(
                CanvasDocument.id == document_id,
                CanvasDocument.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    marker = (
        await db.execute(
            select(CanvasMutation).where(
                CanvasMutation.canvas_id == document_id,
                CanvasMutation.user_id == user_id,
                CanvasMutation.client_id == client_id,
                CanvasMutation.mutation_id == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if marker is None:
        raise idempotency_conflict(
            "idempotency key resolved to a Canvas without a replay marker"
        )
    response = marker.response_jsonb
    if (
        not isinstance(response, dict)
        or response.get("request_fingerprint") != request_fingerprint
    ):
        raise idempotency_conflict(
            "idempotency key was already used for another Canvas request"
        )
    return row


def _add_document_idempotency_marker(
    db: AsyncSession,
    *,
    row: CanvasDocument,
    user_id: str,
    client_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> None:
    db.add(
        CanvasMutation(
            canvas_id=row.id,
            user_id=user_id,
            client_id=client_id,
            mutation_id=idempotency_key,
            operation_schema_version=1,
            base_revision=0,
            result_revision=1,
            operations_jsonb=[],
            response_jsonb={
                "canvas_id": row.id,
                "request_fingerprint": request_fingerprint,
            },
        )
    )


def _graph_without_execution_history(graph: dict[str, Any]) -> dict[str, Any]:
    detached = validated_graph(graph)
    for edge in detached.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("binding_mode") != "pinned":
            continue
        edge["binding_mode"] = "follow_active"
        edge["pinned_execution_id"] = None
        edge["pinned_output_index"] = None
    return validated_graph(detached)


def document_dict(row: CanvasDocument, *, graph: bool = True) -> dict[str, Any]:
    graph_value = row.graph_jsonb or {}
    nodes = graph_value.get("nodes") if isinstance(graph_value, dict) else []
    edges = graph_value.get("edges") if isinstance(graph_value, dict) else []
    out: dict[str, Any] = {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "revision": int(row.revision),
        "graph_schema_version": int(row.graph_schema_version),
        "conversation_id": row.conversation_id,
        "thumbnail_image_id": row.thumbnail_image_id,
        "thumbnail_url": (
            f"/api/images/{row.thumbnail_image_id}/binary"
            if row.thumbnail_image_id
            else None
        ),
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
        "edge_count": len(edges) if isinstance(edges, list) else 0,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if graph:
        out["graph"] = row.graph_jsonb
    return out


async def get_owned_canvas(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    lock: bool = False,
) -> CanvasDocument:
    stmt = select(CanvasDocument).where(
        CanvasDocument.id == canvas_id,
        CanvasDocument.user_id == user_id,
        CanvasDocument.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def create_canvas(
    db: AsyncSession,
    *,
    user_id: str,
    body: CanvasCreateIn,
    idempotency_key: str | None = None,
) -> CanvasDocument:
    graph = validated_graph(body.graph)
    document_id: str | None = None
    request_fingerprint: str | None = None
    if idempotency_key is not None:
        request_fingerprint = stable_hash(
            {
                "operation": "create",
                "title": body.title.strip(),
                "description": body.description,
                "template": body.template,
                "graph": graph,
            }
        )
        await lock_user_key(
            db,
            "canvas-document-create",
            user_id,
            idempotency_key,
        )
        document_id = _idempotent_document_id(
            operation="create",
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        existing = await _existing_idempotent_document(
            db,
            user_id=user_id,
            document_id=document_id,
            client_id=_DOCUMENT_CREATE_CLIENT_ID,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            return existing
    await lock_canvas_write_identity(db, user_id=user_id)
    row_values: dict[str, Any] = {}
    if document_id is not None:
        row_values["id"] = document_id
    row = CanvasDocument(
        **row_values,
        user_id=user_id,
        title=body.title.strip(),
        description=body.description,
        graph_schema_version=int(graph.get("schema_version") or 1),
        graph_jsonb=graph,
        revision=1,
    )
    db.add(row)
    try:
        await db.flush()
        if idempotency_key is not None and request_fingerprint is not None:
            _add_document_idempotency_marker(
                db,
                row=row,
                user_id=user_id,
                client_id=_DOCUMENT_CREATE_CLIENT_ID,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        await sync_head_asset_refs(
            db,
            canvas_id=row.id,
            user_id=user_id,
            graph=graph,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if (
            document_id is not None
            and idempotency_key is not None
            and request_fingerprint is not None
        ):
            existing = await _existing_idempotent_document(
                db,
                user_id=user_id,
                document_id=document_id,
                client_id=_DOCUMENT_CREATE_CLIENT_ID,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if existing is not None:
                return existing
        raise
    await db.refresh(row)
    return row


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw_timestamp, row_id = cursor.split("|", 1)
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.tzinfo is None or not row_id:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise canvas_http("invalid_cursor", "cursor is invalid", 422) from exc
    return timestamp, row_id


def encode_cursor(row: CanvasDocument) -> str:
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return f"{updated_at.isoformat()}|{row.id}"


async def list_canvases(
    db: AsyncSession,
    *,
    user_id: str,
    cursor: str | None,
    limit: int,
    q: str | None = None,
) -> dict[str, Any]:
    stmt = select(CanvasDocument).where(
        CanvasDocument.user_id == user_id,
        CanvasDocument.deleted_at.is_(None),
    )
    if q and q.strip():
        stmt = stmt.where(CanvasDocument.title.ilike(f"%{q.strip()}%"))
    decoded = decode_cursor(cursor)
    if decoded is not None:
        updated_at, row_id = decoded
        stmt = stmt.where(
            or_(
                CanvasDocument.updated_at < updated_at,
                and_(
                    CanvasDocument.updated_at == updated_at,
                    CanvasDocument.id < row_id,
                ),
            )
        )
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    CanvasDocument.updated_at.desc(),
                    CanvasDocument.id.desc(),
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    execution_rows = []
    if page:
        execution_rows = list(
            (
                await db.execute(
                    select(CanvasNodeExecution).where(
                        CanvasNodeExecution.canvas_id.in_([row.id for row in page])
                    )
                )
            ).scalars()
        )
    stats: dict[str, dict[str, int | bool]] = {}
    for execution in execution_rows:
        current = stats.setdefault(
            execution.canvas_id,
            {
                "image_output_count": 0,
                "video_output_count": 0,
                "running_count": 0,
                "has_failure": False,
            },
        )
        if execution.status in {
            "pending",
            "ready",
            "queued",
            "running",
            "reconciling",
            "canceling",
        }:
            current["running_count"] = int(current["running_count"]) + 1
        if execution.status in {"failed", "partial_failed", "blocked"}:
            current["has_failure"] = True
        for output in execution.outputs_jsonb or []:
            if not isinstance(output, dict):
                continue
            if output.get("image_id"):
                current["image_output_count"] = int(current["image_output_count"]) + 1
            if output.get("video_id"):
                current["video_output_count"] = int(current["video_output_count"]) + 1
    items = []
    for row in page:
        item = document_dict(row, graph=False)
        item.update(
            stats.get(
                row.id,
                {
                    "image_output_count": 0,
                    "video_output_count": 0,
                    "running_count": 0,
                    "has_failure": False,
                },
            )
        )
        item["has_conflict"] = False
        items.append(item)
    return {
        "items": items,
        "next_cursor": encode_cursor(page[-1]) if has_more and page else None,
    }


async def patch_canvas(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    body: CanvasPatchIn,
) -> CanvasDocument:
    await lock_canvas_write_identity(db, user_id=user_id)
    row = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    if body.title is not None:
        row.title = body.title.strip()
    if body.description is not None:
        row.description = body.description
    await db.commit()
    await db.refresh(row)
    return row


async def delete_canvas(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
) -> None:
    await lock_canvas_write_identity(db, user_id=user_id)
    row = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
        lock=True,
    )
    active_execution = (
        await db.execute(
            select(CanvasNodeExecution.id)
            .where(
                CanvasNodeExecution.canvas_id == row.id,
                CanvasNodeExecution.status.in_(
                    (
                        "pending",
                        "ready",
                        "queued",
                        "running",
                        "reconciling",
                        "canceling",
                    )
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_execution is not None:
        raise canvas_http(
            "canvas_has_active_executions",
            "canvas cannot be deleted while executions are active",
            409,
        )
    row.deleted_at = datetime.now(timezone.utc)
    await delete_head_asset_refs(db, canvas_id=row.id)
    await db.commit()


async def duplicate_canvas(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    body: CanvasDuplicateIn,
    idempotency_key: str | None = None,
) -> CanvasDocument:
    document_id: str | None = None
    request_fingerprint: str | None = None
    if idempotency_key is not None:
        request_fingerprint = stable_hash(
            {
                "operation": "duplicate",
                "source_canvas_id": canvas_id,
                "title": body.title,
            }
        )
        await lock_user_key(
            db,
            "canvas-document-duplicate",
            user_id,
            idempotency_key,
        )
        document_id = _idempotent_document_id(
            operation="duplicate",
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        existing = await _existing_idempotent_document(
            db,
            user_id=user_id,
            document_id=document_id,
            client_id=_DOCUMENT_DUPLICATE_CLIENT_ID,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            return existing
    await lock_canvas_write_identity(db, user_id=user_id)
    source = await get_owned_canvas(
        db,
        user_id=user_id,
        canvas_id=canvas_id,
    )
    graph = _graph_without_execution_history(source.graph_jsonb)
    row_values: dict[str, Any] = {}
    if document_id is not None:
        row_values["id"] = document_id
    row = CanvasDocument(
        **row_values,
        user_id=user_id,
        title=(body.title or f"{source.title} 副本").strip(),
        description=source.description,
        graph_schema_version=source.graph_schema_version,
        graph_jsonb=graph,
        revision=1,
        thumbnail_image_id=source.thumbnail_image_id,
    )
    db.add(row)
    try:
        await db.flush()
        if idempotency_key is not None and request_fingerprint is not None:
            _add_document_idempotency_marker(
                db,
                row=row,
                user_id=user_id,
                client_id=_DOCUMENT_DUPLICATE_CLIENT_ID,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        await sync_head_asset_refs(
            db,
            canvas_id=row.id,
            user_id=user_id,
            graph=graph,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if (
            document_id is not None
            and idempotency_key is not None
            and request_fingerprint is not None
        ):
            existing = await _existing_idempotent_document(
                db,
                user_id=user_id,
                document_id=document_id,
                client_id=_DOCUMENT_DUPLICATE_CLIENT_ID,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if existing is not None:
                return existing
        raise
    await db.refresh(row)
    return row
