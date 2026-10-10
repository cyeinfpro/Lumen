"""Explicit owner-scoped preparation retries, with no generation or billing."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Video

from .video_preparation import PREPARATION_KEY
from ..video_reference_videos import VideoReferenceVideoError


async def retry_video_preparation(
    db: AsyncSession,
    *,
    video_id: str,
    user_id: str,
    expected_source_sha256: str,
    expected_preparation_revision: int,
    idempotency_key: str,
) -> Video:
    row = (
        await db.execute(
            select(Video)
            .where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise VideoReferenceVideoError("not_found", "video not found", 404)
    metadata = dict(row.metadata_jsonb or {})
    state = metadata.get(PREPARATION_KEY)
    if (
        metadata.get("source") != "uploaded_reference"
        or not isinstance(state, dict)
        or state.get("source_sha256") != row.sha256
        or row.sha256 != expected_source_sha256
    ):
        raise VideoReferenceVideoError(
            "video_preparation_changed",
            "video preparation changed; refresh first",
            409,
        )
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    if state.get("retry_key_hash") == key_hash:
        if state.get("retry_expected_revision") != expected_preparation_revision:
            raise VideoReferenceVideoError(
                "idempotency_conflict",
                "retry key was already used for another revision",
                409,
            )
        await db.commit()
        await db.refresh(row)
        return row
    if int(state.get("revision") or 0) != expected_preparation_revision:
        raise VideoReferenceVideoError(
            "video_preparation_changed",
            "video preparation changed; refresh first",
            409,
        )
    if state.get("state") == "failed":
        metadata[PREPARATION_KEY] = {
            **state,
            "state": "pending",
            "lease_until": 0,
            "revision": expected_preparation_revision + 1,
            "error_code": None,
            "retry_key_hash": key_hash,
            "retry_expected_revision": expected_preparation_revision,
        }
        metadata[PREPARATION_KEY].pop("claim_token", None)
        row.metadata_jsonb = metadata
    elif state.get("state") not in {"pending", "preparing", "ready"}:
        raise VideoReferenceVideoError(
            "video_preparation_unavailable",
            "video preparation cannot be retried",
            409,
        )
    await db.commit()
    await db.refresh(row)
    return row
