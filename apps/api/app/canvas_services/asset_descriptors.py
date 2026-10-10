"""Public, read-only Canvas asset descriptors.

IDs and source hashes identify assets. URLs are replaceable locators and never
participate in identity. This projection does not prepare or retain any media.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Image, Video


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def asset_descriptor(row: Image | Video) -> dict[str, Any]:
    is_video = isinstance(row, Video)
    kind = "video" if is_video else "image"
    width = _positive_int(row.width)
    height = _positive_int(row.height)
    metadata = row.metadata_jsonb if isinstance(row.metadata_jsonb, dict) else {}
    preparation = metadata.get("canvas_preparation")
    preparation = preparation if isinstance(preparation, dict) else {}
    if is_video:
        duration_ms = _positive_int(row.duration_ms)
        state = "ready" if width and height and duration_ms else "pending"
        if preparation.get("source_sha256") == row.sha256:
            recorded = preparation.get("state")
            if recorded in {"pending", "preparing", "failed"}:
                state = recorded
        locators = {
            "original": f"/api/videos/{row.id}/binary",
            "preview": f"/api/videos/{row.id}/binary",
            "thumb": f"/api/videos/{row.id}/poster" if row.poster_storage_key else None,
        }
    else:
        duration_ms = None
        artifact_status = row.artifact_status or "ready"
        state = {
            "ready": "ready",
            "failed": "failed",
            "staging": "pending",
            "processing": "preparing",
            "publishing": "preparing",
        }.get(artifact_status, "unavailable")
        locators = {
            "original": f"/api/images/{row.id}/binary",
            "preview": f"/api/images/{row.id}/variants/preview1024",
            "thumb": f"/api/images/{row.id}/variants/thumb256",
        }
    return {
        "schema_version": 1,
        "asset_id": row.id,
        "kind": kind,
        "source_sha256": row.sha256,
        "mime": row.mime,
        "width": width,
        "height": height,
        "duration_ms": duration_ms,
        "size_bytes": row.size_bytes,
        "preparation_state": state,
        "preparation_revision": int(preparation.get("revision") or 0)
        if is_video
        else None,
        "updated_at": row.updated_at,
        "locators": locators,
    }


async def canvas_asset_descriptors(
    db: AsyncSession,
    *,
    user_id: str,
    graph: dict[str, Any],
    executions: list[Any],
) -> list[dict[str, Any]]:
    """Fetch referenced metadata in two bounded, owner-scoped queries."""
    image_ids: set[str] = set()
    video_ids: set[str] = set()
    references = [
        node.get("config", {})
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    ]
    references.extend(
        output for execution in executions for output in (execution.outputs_jsonb or [])
    )
    for reference in references:
        if not isinstance(reference, dict):
            continue
        for key, ids in (("image_id", image_ids), ("video_id", video_ids)):
            value = reference.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    descriptors = []
    for model, ids in ((Image, image_ids), (Video, video_ids)):
        if not ids:
            continue
        rows = (
            await db.execute(
                select(model).where(
                    model.id.in_(ids),
                    model.user_id == user_id,
                    model.deleted_at.is_(None),
                )
            )
        ).scalars()
        descriptors.extend(asset_descriptor(row) for row in rows)
    return sorted(descriptors, key=lambda item: (item["kind"], item["asset_id"]))
