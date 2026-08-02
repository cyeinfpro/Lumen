"""Locked reference-video inventory and quota reconciliation."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Video

from ..services.video.reference_snapshots import lock_user_reference_media
from ..services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    record_video_storage_cleanup,
    video_reference_declared_quota_contribution,
    video_reference_quota_contribution,
)


async def _reconcile_aged_adoption_markers(
    *,
    user_id: str,
    deps: Any,
    clear_adoption_marker: Callable[..., Awaitable[None]],
    adopted_outcome: Any,
    not_adopted_outcome: Any,
) -> None:
    try:
        markers = await deps.storage_lifecycle.aged_upload_adoption_markers(
            user_id=user_id,
            limit=8,
        )
    except Exception:
        deps.logger.warning(
            "failed to list aged reference video adoption markers user_id=%s",
            user_id,
            exc_info=True,
        )
        markers = ()
    for marker in markers:
        try:
            probe = await deps.probe_adoption(
                video_id=marker.video_id,
                user_id=marker.user_id,
                storage_key=marker.storage_key,
                sha256=marker.sha256,
                size_bytes=marker.size_bytes,
            )
        except Exception:
            deps.logger.warning(
                "aged reference video adoption probe failed video_id=%s",
                marker.video_id,
                exc_info=True,
            )
            continue
        if probe.outcome is adopted_outcome:
            await clear_adoption_marker(marker=marker, deps=deps)
        elif probe.outcome is not_adopted_outcome:
            try:
                await deps.storage_lifecycle.discard_unadopted_upload(marker)
            except Exception:
                deps.logger.warning(
                    "aged reference video discard failed video_id=%s",
                    marker.video_id,
                    exc_info=True,
                )


def _unique_rows(*row_groups: list[Any]) -> list[Any]:
    rows: list[Any] = []
    for row in (row for group in row_groups for row in group):
        if not any(
            getattr(existing_row, "id", None) == getattr(row, "id", None)
            for existing_row in rows
        ):
            rows.append(row)
    return rows


async def _cleanup_pending_inventory(
    *,
    pending_rows: list[Any],
    existing: Any | None,
    cleanup_page_size: int,
    db: AsyncSession,
    deps: Any,
) -> None:
    pending_overflow = len(pending_rows) > cleanup_page_size
    for row in pending_rows[:cleanup_page_size]:
        if row is existing or getattr(row, "deleted_at", None) is None:
            continue
        cleanup = await deps.storage_lifecycle.cleanup(row)
        record_video_storage_cleanup(row, cleanup)
        if not cleanup.complete:
            deps.logger.warning(
                "deleted reference video cleanup remains pending video_id=%s errors=%s",
                row.id,
                cleanup.errors,
            )
    if pending_overflow:
        await db.commit()
        raise deps.http_error(
            "video_cleanup_backlog",
            "reference video cleanup is still catching up; retry shortly",
            503,
        )


def _inventory_quota(
    *,
    rows: list[Any],
    variant_rows: list[Any],
    inspections: dict[str, Any],
) -> tuple[int, int]:
    quota_count = 0
    quota_bytes = 0
    for row in rows:
        count_delta, bytes_delta = video_reference_quota_contribution(
            row,
            inspections[str(row.id)],
        )
        quota_count += count_delta
        quota_bytes += bytes_delta
    inspected_ids = {str(row.id) for row in rows}
    for row in variant_rows:
        if str(row.id) in inspected_ids:
            continue
        count_delta, bytes_delta = video_reference_declared_quota_contribution(row)
        quota_count += count_delta
        quota_bytes += bytes_delta
    return quota_count, quota_bytes


async def load_reference_inventory(
    *,
    user_id: str,
    sha256: str,
    db: AsyncSession,
    deps: Any,
    cleanup_page_size: int,
    result_rows: Callable[[Any], list[Any]],
    matching_video: Callable[[list[Any], str], Any | None],
    clear_adoption_marker: Callable[..., Awaitable[None]],
    adopted_outcome: Any,
    not_adopted_outcome: Any,
) -> tuple[Any | None, dict[str, Any], int, int]:
    await lock_user_reference_media(
        db,
        user_id=user_id,
        http_error=deps.http_error,
    )
    await _reconcile_aged_adoption_markers(
        user_id=user_id,
        deps=deps,
        clear_adoption_marker=clear_adoption_marker,
        adopted_outcome=adopted_outcome,
        not_adopted_outcome=not_adopted_outcome,
    )

    exact_active = result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.sha256 == sha256,
                Video.storage_key.like(f"u/{user_id}/vref/%"),
            )
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(1)
            .with_for_update()
        )
    )
    active_rows = result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.storage_key.like(f"u/{user_id}/vref/%"),
            )
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(deps.max_count + 1)
            .with_for_update()
        )
    )
    exact_deleted = result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_not(None),
                Video.sha256 == sha256,
                Video.storage_key.like(f"u/{user_id}/vref/%"),
            )
            .order_by(Video.deleted_at.desc(), Video.created_at.desc(), Video.id.desc())
            .limit(1)
            .with_for_update()
        )
    )
    cleanup_state = Video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY][
        "state"
    ].as_string()
    cleanup_attempted_at = Video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY][
        "attempted_at"
    ].as_string()
    pending_rows = result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_not(None),
                Video.storage_key.like(f"u/{user_id}/vref/%"),
                or_(cleanup_state.is_(None), cleanup_state != "complete"),
            )
            .order_by(
                cleanup_attempted_at.asc().nulls_first(),
                Video.deleted_at.asc(),
                Video.id.asc(),
            )
            .limit(cleanup_page_size + 1)
            .with_for_update()
        )
    )
    upstream_variant_key = Video.metadata_jsonb["upstream_reference_video_variant"][
        "storage_key"
    ].as_string()
    volcano_variant_key = Video.metadata_jsonb["volcano_asset_video_variant"][
        "storage_key"
    ].as_string()
    variant_rows = result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                or_(
                    upstream_variant_key.is_not(None),
                    volcano_variant_key.is_not(None),
                ),
                or_(
                    Video.deleted_at.is_(None),
                    cleanup_state.is_(None),
                    cleanup_state != "complete",
                ),
            )
            .with_for_update()
        )
    )
    rows = _unique_rows(exact_active, active_rows, exact_deleted, pending_rows)
    existing = matching_video(rows, sha256)
    await _cleanup_pending_inventory(
        pending_rows=pending_rows,
        existing=existing,
        cleanup_page_size=cleanup_page_size,
        db=db,
        deps=deps,
    )
    inspections = await deps.storage_lifecycle.inspect_many(rows)
    quota_count, quota_bytes = _inventory_quota(
        rows=rows,
        variant_rows=variant_rows,
        inspections=inspections,
    )
    return existing, inspections, quota_count, quota_bytes
