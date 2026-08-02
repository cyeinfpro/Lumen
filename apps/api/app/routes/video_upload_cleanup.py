"""Reference-video cleanup execution outside inventory transactions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Video

from ..services.video.reference_snapshots import lock_user_reference_media
from ..services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    VideoReferenceStorageLockTimeout,
    record_video_storage_cleanup,
)

_CLEANUP_CLAIM_KEY = "reference_inventory_cleanup_claim"


def _metadata_copy(row: Any) -> dict[str, Any]:
    metadata = getattr(row, "metadata_jsonb", None)
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def _cleanup_claim_token(row: Any) -> str | None:
    claim = _metadata_copy(row).get(_CLEANUP_CLAIM_KEY)
    token = claim.get("token") if isinstance(claim, dict) else None
    return token if isinstance(token, str) and token else None


async def cleanup_pending_inventory(
    *,
    cleanup_rows: tuple[Any, ...],
    db: AsyncSession,
    deps: Any,
    snapshot_from_row: Callable[[Any], Any],
) -> list[tuple[Any, Any | None]]:
    results: list[tuple[Any, Any | None]] = []
    for row in cleanup_rows:
        token = _cleanup_claim_token(row)
        if token is None:
            results.append((row, None))
            continue
        try:
            async with deps.storage_lifecycle.reference_mutation_lock(
                user_id=row.user_id,
                video_id=row.id,
            ):
                await lock_user_reference_media(
                    db,
                    user_id=row.user_id,
                    http_error=deps.http_error,
                )
                current = (
                    await db.execute(
                        select(Video)
                        .where(
                            Video.id == row.id,
                            Video.user_id == row.user_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    current is None
                    or current.deleted_at is None
                    or _cleanup_claim_token(current) != token
                ):
                    await db.commit()
                    results.append((row, None))
                    continue
                snapshot = snapshot_from_row(current)
                await db.commit()
                cleanup_metadata = snapshot.metadata_jsonb.get(
                    VIDEO_STORAGE_CLEANUP_METADATA_KEY
                )
                prior_token = (
                    cleanup_metadata.get("quarantine_token")
                    if isinstance(cleanup_metadata, dict)
                    else None
                )
                detached = (
                    deps.storage_lifecycle.detached_cleanup(
                        user_id=snapshot.user_id,
                        video_id=snapshot.id,
                        token=prior_token,
                    )
                    if isinstance(prior_token, str) and prior_token
                    else None
                )
                if detached is None or detached.path is None:
                    detached = deps.storage_lifecycle.detach_cleanup(
                        snapshot,
                        token=token,
                    )
        except VideoReferenceStorageLockTimeout as exc:
            raise deps.http_error(
                "video_storage_busy",
                "reference video storage is busy; retry shortly",
                503,
            ) from exc
        cleanup = await deps.storage_lifecycle.cleanup_detached(detached)
        results.append((row, cleanup))
        if not cleanup.complete:
            deps.logger.warning(
                "deleted reference video cleanup remains pending video_id=%s errors=%s",
                row.id,
                cleanup.errors,
            )
    return results


async def persist_cleanup_results(
    *,
    user_id: str,
    cleanup_results: list[tuple[Any, Any | None]],
    db: AsyncSession,
    deps: Any,
    result_rows: Callable[[Any], list[Any]],
) -> None:
    if not cleanup_results:
        return
    row_ids = [row.id for row, _cleanup in cleanup_results]
    try:
        await lock_user_reference_media(
            db,
            user_id=user_id,
            http_error=deps.http_error,
        )
        current_rows = result_rows(
            await db.execute(
                select(Video)
                .where(
                    Video.user_id == user_id,
                    Video.id.in_(row_ids),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        current_by_id = {str(row.id): row for row in current_rows}
        for snapshot, cleanup in cleanup_results:
            current = current_by_id.get(snapshot.id)
            if current is None:
                continue
            expected_token = _cleanup_claim_token(snapshot)
            if (
                expected_token is None
                or _cleanup_claim_token(current) != expected_token
            ):
                continue
            snapshot_matches = snapshot.matches(current)
            metadata = _metadata_copy(current)
            metadata.pop(_CLEANUP_CLAIM_KEY, None)
            current.metadata_jsonb = metadata
            if (
                cleanup is not None
                and snapshot_matches
                and getattr(current, "deleted_at", None) is not None
            ):
                record_video_storage_cleanup(current, cleanup)
            elif cleanup is not None:
                deps.logger.warning(
                    "reference video cleanup result lost CAS video_id=%s",
                    snapshot.id,
                )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


__all__ = ("cleanup_pending_inventory", "persist_cleanup_results")
