"""Durable, bounded metadata preparation for newly uploaded reference videos.

Original bytes, retention, credentials, and transport variants are unchanged.
Each claim has a time-limited token; stale completions cannot publish metadata.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import User, Video
from ..media_process import cancellable_media_thread

from ..video_reference_videos import (
    reference_storage_usage,
    inspect_video_reference_original,
)
from .video_preparation_posters import (
    POSTER_KEY,
    POSTER_MAX_BYTES,
    install_preparation_poster,
    poster_slot,
    render_preparation_poster,
    validate_poster,
    verified_original,
)
from .video_storage_capacity import (
    build_video_storage_capacity_manager,
    build_video_transcode_capacity_manager,
    enforce_video_reference_storage_quota,
)


logger = logging.getLogger(__name__)
PREPARATION_KEY = "canvas_preparation"
PREPARATION_LEASE_SECONDS = 120
PREPARATION_BATCH_SIZE = 4


@dataclass(frozen=True, slots=True)
class PreparationClaim:
    video_id: str
    user_id: str
    storage_key: str
    source_sha256: str
    size_bytes: int
    token: str
    lease_until: float
    artifact_revision: int = 1


def pending_preparation(source_sha256: str) -> dict[str, Any]:
    return {
        "state": "pending",
        "source_sha256": source_sha256,
        "revision": 0,
        "lease_until": 0,
        "attempt": 0,
    }


async def claim_preparation(
    db: AsyncSession,
    video_id: str,
    *,
    now: float | None = None,
) -> PreparationClaim | None:
    current_time = time.time() if now is None else now
    row = (
        await db.execute(
            select(Video)
            .where(Video.id == video_id, Video.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    metadata = dict(row.metadata_jsonb or {})
    state = metadata.get(PREPARATION_KEY)
    if (
        metadata.get("source") != "uploaded_reference"
        or not isinstance(state, dict)
        or state.get("source_sha256") != row.sha256
    ):
        return None
    if state.get("state") not in {"pending", "preparing"}:
        return None
    if float(state.get("lease_until") or 0) > current_time:
        return None
    token = secrets.token_hex(16)
    lease_until = current_time + PREPARATION_LEASE_SECONDS
    state = {
        **state,
        "state": "preparing",
        "claim_token": token,
        "lease_until": lease_until,
        "attempt": int(state.get("attempt") or 0) + 1,
        "revision": int(state.get("revision") or 0) + 1,
        "artifact_revision": int(state.get("artifact_revision") or 1),
    }
    metadata[PREPARATION_KEY] = state
    row.metadata_jsonb = metadata
    claim = PreparationClaim(
        row.id,
        row.user_id,
        row.storage_key,
        row.sha256,
        row.size_bytes,
        token,
        lease_until,
        state["artifact_revision"],
    )
    await db.commit()
    return claim


async def current_preparation(
    db: AsyncSession,
    claim: PreparationClaim,
    *,
    now: float | None = None,
) -> Video | None:
    current_time = time.time() if now is None else now
    row = (
        await db.execute(
            select(Video)
            .where(
                Video.id == claim.video_id,
                Video.user_id == claim.user_id,
                Video.storage_key == claim.storage_key,
                Video.sha256 == claim.source_sha256,
                Video.size_bytes == claim.size_bytes,
                Video.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    metadata = dict(row.metadata_jsonb or {})
    state = metadata.get(PREPARATION_KEY)
    if (
        metadata.get("source") != "uploaded_reference"
        or not isinstance(state, dict)
        or state.get("source_sha256") != claim.source_sha256
        or state.get("claim_token") != claim.token
        or state.get("state") != "preparing"
        or state.get("lease_until") != claim.lease_until
        or claim.lease_until <= current_time
    ):
        return None
    return row


async def reserve_poster_slot(
    db: AsyncSession,
    claim: PreparationClaim,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    # Match the lock order used by reference-variant quota admission.
    await db.execute(select(User.id).where(User.id == claim.user_id).with_for_update())
    row = await current_preparation(db, claim, now=now)
    if row is None:
        await db.rollback()
        return None
    metadata = dict(row.metadata_jsonb or {})
    planned = poster_slot(claim)
    existing = metadata.get(POSTER_KEY)
    if isinstance(existing, dict):
        for key in (
            "storage_key",
            "source_sha256",
            "preparation_revision",
            "format_revision",
        ):
            if existing.get(key) != planned[key]:
                raise ValueError("prepared poster identity changed")
        await db.commit()
        return dict(existing)
    current_bytes = await reference_storage_usage(db, user_id=claim.user_id)
    enforce_video_reference_storage_quota(
        current_bytes=current_bytes,
        replaced_bytes=0,
        added_bytes=POSTER_MAX_BYTES,
    )
    metadata[POSTER_KEY] = planned
    row.metadata_jsonb = metadata
    await db.commit()
    return planned


async def stage_poster_metadata(
    db: AsyncSession,
    claim: PreparationClaim,
    data: bytes,
    *,
    now: float | None = None,
) -> bool:
    row = await current_preparation(db, claim, now=now)
    if row is None:
        return False
    metadata = dict(row.metadata_jsonb or {})
    slot = metadata.get(POSTER_KEY)
    if (
        not isinstance(slot, dict)
        or slot["storage_key"] != poster_slot(claim)["storage_key"]
    ):
        raise ValueError("poster reservation missing")
    info = validate_poster(data)
    metadata[POSTER_KEY] = {
        **slot,
        "sha256": info["sha256"],
        "width": info["width"],
        "height": info["height"],
        "rendered_size_bytes": info["size_bytes"],
    }
    row.metadata_jsonb = metadata
    await db.commit()
    return True


async def bounded_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
    # A cancelled asyncio.to_thread continues using CPU/disk. Keep its leases
    # until the bounded operation has actually stopped, then honor cancellation.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def finish_preparation(
    db: AsyncSession,
    claim: PreparationClaim,
    *,
    inspected: dict[str, Any] | None = None,
    error_code: str | None = None,
    now: float | None = None,
    poster_data: bytes | None = None,
    storage_root: str | None = None,
    capacity_guard: Any = None,
) -> bool:
    row = await current_preparation(db, claim, now=now)
    if row is None:
        return False
    metadata = dict(row.metadata_jsonb or {})
    state = metadata[PREPARATION_KEY]
    if inspected is not None:
        for key in ("width", "height", "duration_ms"):
            if type(inspected.get(key)) is not int or inspected[key] <= 0:
                raise ValueError("invalid prepared metadata")
        if poster_data is not None:
            if storage_root is None or capacity_guard is None:
                raise ValueError("poster publication requires a capacity lease")
            await capacity_guard.assert_owned()
            slot = metadata.get(POSTER_KEY)
            if (
                not isinstance(slot, dict)
                or slot["storage_key"] != poster_slot(claim)["storage_key"]
            ):
                raise ValueError("poster reservation missing")
            artifact = await bounded_thread(
                install_preparation_poster,
                claim,
                slot,
                poster_data,
                storage_root=storage_root,
            )
            await capacity_guard.assert_owned()
            if claim.lease_until <= (time.time() if now is None else now):
                await db.rollback()
                return False
            metadata[POSTER_KEY] = artifact
            row.poster_storage_key = artifact["storage_key"]
        for key in ("width", "height", "duration_ms"):
            setattr(row, key, inspected[key])
        row.fps = inspected.get("fps")
        row.has_audio = bool(inspected.get("has_audio"))
    metadata[PREPARATION_KEY] = {
        **state,
        "state": "ready" if inspected is not None else "failed",
        "source_sha256": claim.source_sha256,
        "revision": int(state.get("revision") or 0) + 1,
        "attempt": int(state.get("attempt") or 0),
        "lease_until": 0,
        "error_code": error_code if inspected is None else None,
    }
    metadata[PREPARATION_KEY].pop("claim_token", None)
    row.metadata_jsonb = metadata
    await db.commit()
    return True


async def prepare_video_metadata(
    session_factory: Any,
    video_id: str,
    *,
    storage_root: str,
) -> bool:
    async with session_factory() as db:
        claim = await claim_preparation(db, video_id)
    if claim is None:
        return False
    try:
        async with build_video_transcode_capacity_manager().hold(user_id=claim.user_id):
            async with session_factory() as db:
                if await current_preparation(db, claim) is None:
                    return False
            await bounded_thread(verified_original, claim, storage_root)
            inspected = await cancellable_media_thread(
                inspect_video_reference_original,
                storage_root=storage_root,
                storage_key=claim.storage_key,
                size_bytes=claim.size_bytes,
                sha256=claim.source_sha256,
            )
            async with build_video_storage_capacity_manager().reserve(
                2 * POSTER_MAX_BYTES
            ) as guard:
                async with session_factory() as db:
                    slot = await reserve_poster_slot(db, claim)
                if slot is None:
                    return False
                data = await cancellable_media_thread(
                    render_preparation_poster,
                    claim,
                    slot,
                    storage_root=storage_root,
                )
                await guard.assert_owned()
                async with session_factory() as db:
                    if not await stage_poster_metadata(db, claim, data):
                        return False
                async with session_factory() as db:
                    return await finish_preparation(
                        db,
                        claim,
                        inspected=inspected,
                        poster_data=data,
                        storage_root=storage_root,
                        capacity_guard=guard,
                    )
    except asyncio.CancelledError:
        # The durable slot keeps any installed artifact accounted and recoverable.
        raise
    except Exception as exc:  # noqa: BLE001
        error_code = getattr(exc, "code", None) or "video_preparation_failed"
        logger.warning(
            "video preparation failed video_id=%s code=%s", claim.video_id, error_code
        )
        async with session_factory() as db:
            return await finish_preparation(db, claim, error_code=error_code)


async def run_preparation_job(
    session_factory: Any,
    video_id: str,
    *,
    storage_root: str,
) -> bool:
    task = asyncio.create_task(
        prepare_video_metadata(session_factory, video_id, storage_root=storage_root),
        name="video-preparation-job",
    )
    try:
        return await task
    except asyncio.CancelledError:
        parent = asyncio.current_task()
        if parent is not None and parent.cancelling():
            raise
        # Distributed capacity can cancel only this child. Its durable claim
        # remains recoverable; do not terminate the application's entire loop.
        logger.warning("video preparation claim interrupted video_id=%s", video_id)
        return False


async def video_preparation_loop(stop: asyncio.Event) -> None:
    from ..config import settings
    from ..db import SessionLocal

    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                state = Video.metadata_jsonb[PREPARATION_KEY]
                ids = list(
                    (
                        await db.execute(
                            select(Video.id)
                            .where(
                                Video.deleted_at.is_(None),
                                state["state"]
                                .as_string()
                                .in_(("pending", "preparing")),
                                or_(
                                    state["lease_until"].as_float() <= time.time(),
                                    state["lease_until"].as_float().is_(None),
                                ),
                            )
                            .order_by(Video.created_at, Video.id)
                            .limit(PREPARATION_BATCH_SIZE)
                        )
                    ).scalars()
                )
            for video_id in ids:
                if stop.is_set():
                    break
                await run_preparation_job(
                    SessionLocal, video_id, storage_root=settings.storage_root
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("video metadata preparation pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
