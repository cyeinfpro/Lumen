"""Short-transaction reference-video inventory and quota reconciliation."""

from __future__ import annotations

import json
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Video

from ..services.video.reference_snapshots import lock_user_reference_media
from ..services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    video_reference_declared_quota_contribution,
    video_reference_quota_contribution,
)
from .video_upload_cleanup import (
    cleanup_pending_inventory as _cleanup_pending_inventory,
    persist_cleanup_results as _persist_cleanup_results,
)

_REFERENCE_INVENTORY_CLEANUP_CLAIM_KEY = "reference_inventory_cleanup_claim"
_REFERENCE_INVENTORY_CLEANUP_CLAIM_TTL_SECONDS = 15 * 60
_REFERENCE_INVENTORY_VARIANT_SCAN_LIMIT = 2_048


def _metadata_copy(row: Any) -> dict[str, Any]:
    metadata = getattr(row, "metadata_jsonb", None)
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def _datetime_fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def reference_video_fingerprint(row: Any) -> tuple[Any, ...]:
    metadata = json.dumps(
        _metadata_copy(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return (
        str(getattr(row, "id", "") or ""),
        str(getattr(row, "user_id", "") or ""),
        str(getattr(row, "owner_generation_id", "") or "") or None,
        str(getattr(row, "storage_key", "") or ""),
        str(getattr(row, "poster_storage_key", "") or "") or None,
        str(getattr(row, "mime", "") or ""),
        max(0, int(getattr(row, "size_bytes", 0) or 0)),
        str(getattr(row, "sha256", "") or ""),
        str(getattr(row, "etag", "") or ""),
        str(getattr(row, "visibility", "") or ""),
        _datetime_fingerprint(getattr(row, "deleted_at", None)),
        metadata,
    )


@dataclass(frozen=True)
class ReferenceVideoSnapshot:
    id: str
    user_id: str
    owner_generation_id: str | None
    storage_key: str
    poster_storage_key: str | None
    mime: str
    size_bytes: int
    sha256: str
    etag: str
    visibility: str
    deleted_at: Any | None
    created_at: Any | None
    metadata_jsonb: dict[str, Any]
    fingerprint: tuple[Any, ...]

    @classmethod
    def from_row(cls, row: Any) -> ReferenceVideoSnapshot:
        owner_generation_id = getattr(row, "owner_generation_id", None)
        return cls(
            id=str(row.id),
            user_id=str(row.user_id),
            owner_generation_id=(
                str(owner_generation_id) if owner_generation_id else None
            ),
            storage_key=str(getattr(row, "storage_key", "") or ""),
            poster_storage_key=(
                str(getattr(row, "poster_storage_key", "") or "") or None
            ),
            mime=str(getattr(row, "mime", "") or ""),
            size_bytes=max(0, int(getattr(row, "size_bytes", 0) or 0)),
            sha256=str(getattr(row, "sha256", "") or ""),
            etag=str(getattr(row, "etag", "") or ""),
            visibility=str(getattr(row, "visibility", "") or ""),
            deleted_at=getattr(row, "deleted_at", None),
            created_at=getattr(row, "created_at", None),
            metadata_jsonb=_metadata_copy(row),
            fingerprint=reference_video_fingerprint(row),
        )

    def matches(self, row: Any) -> bool:
        return self.fingerprint == reference_video_fingerprint(row)


@dataclass(frozen=True)
class ReferenceInventorySnapshot:
    existing: ReferenceVideoSnapshot | None
    rows: tuple[ReferenceVideoSnapshot, ...]
    variant_rows: tuple[ReferenceVideoSnapshot, ...]
    inspections: dict[str, Any]
    quota_count: int
    quota_bytes: int
    signature: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class LockedReferenceInventory:
    existing: Any | None
    rows: list[Any]
    variant_rows: list[Any]
    signature: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class _InventoryRows:
    exact_active: list[Any]
    active_rows: list[Any]
    exact_deleted: list[Any]
    pending_rows: list[Any]
    variant_rows: list[Any]

    @property
    def rows(self) -> list[Any]:
        return _unique_rows(
            self.exact_active,
            self.active_rows,
            self.exact_deleted,
            self.pending_rows,
        )


@dataclass(frozen=True)
class _PreparedInventory:
    existing: ReferenceVideoSnapshot | None
    rows: tuple[ReferenceVideoSnapshot, ...]
    variant_rows: tuple[ReferenceVideoSnapshot, ...]
    signature: tuple[tuple[Any, ...], ...]
    cleanup_rows: tuple[ReferenceVideoSnapshot, ...]
    cleanup_overflow: bool


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
    seen: set[str] = set()
    for row in (row for group in row_groups for row in group):
        row_id = str(getattr(row, "id", "") or "")
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(row)
    return rows


def _inventory_signature(
    rows: list[Any] | tuple[ReferenceVideoSnapshot, ...],
    variant_rows: list[Any] | tuple[ReferenceVideoSnapshot, ...],
) -> tuple[tuple[Any, ...], ...]:
    unique = _unique_rows(list(rows), list(variant_rows))
    fingerprints = [
        (
            row.fingerprint
            if isinstance(row, ReferenceVideoSnapshot)
            else reference_video_fingerprint(row)
        )
        for row in unique
    ]
    return tuple(sorted(fingerprints, key=lambda fingerprint: str(fingerprint[0])))


def _cleanup_claim(row: Any) -> dict[str, Any] | None:
    claim = _metadata_copy(row).get(_REFERENCE_INVENTORY_CLEANUP_CLAIM_KEY)
    return claim if isinstance(claim, dict) else None


def _cleanup_claim_is_live(row: Any, *, now: datetime) -> bool:
    claim = _cleanup_claim(row)
    if claim is None:
        return False
    raw_claimed_at = claim.get("claimed_at")
    if not isinstance(raw_claimed_at, str):
        return False
    try:
        claimed_at = datetime.fromisoformat(raw_claimed_at)
    except ValueError:
        return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return (
        now - claimed_at.astimezone(timezone.utc)
    ).total_seconds() < _REFERENCE_INVENTORY_CLEANUP_CLAIM_TTL_SECONDS


def _claim_pending_cleanup_rows(
    *,
    pending_rows: list[Any],
    existing: Any | None,
    cleanup_page_size: int,
) -> tuple[ReferenceVideoSnapshot, ...]:
    claimed_at = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(18)
    claimed_rows: list[ReferenceVideoSnapshot] = []
    existing_id = str(getattr(existing, "id", "") or "") if existing else None
    for row in pending_rows[:cleanup_page_size]:
        if (
            str(getattr(row, "id", "") or "") == existing_id
            or getattr(row, "deleted_at", None) is None
            or _cleanup_claim_is_live(row, now=claimed_at)
        ):
            continue
        metadata = _metadata_copy(row)
        metadata[_REFERENCE_INVENTORY_CLEANUP_CLAIM_KEY] = {
            "token": token,
            "claimed_at": claimed_at.isoformat(),
        }
        row.metadata_jsonb = metadata
        claimed_rows.append(ReferenceVideoSnapshot.from_row(row))
    return tuple(claimed_rows)


def _matching_unclaimed_video(
    rows: list[Any],
    sha256: str,
    *,
    matching_video: Callable[[list[Any], str], Any | None],
) -> Any | None:
    return matching_video(
        [row for row in rows if _cleanup_claim(row) is None],
        sha256,
    )


def _snapshot_rows(rows: list[Any]) -> tuple[ReferenceVideoSnapshot, ...]:
    return tuple(ReferenceVideoSnapshot.from_row(row) for row in rows)


async def _query_locked_inventory(
    *,
    user_id: str,
    sha256: str,
    db: AsyncSession,
    deps: Any,
    cleanup_page_size: int,
    result_rows: Callable[[Any], list[Any]],
) -> _InventoryRows:
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
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
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
            .order_by(Video.id)
            .limit(_REFERENCE_INVENTORY_VARIANT_SCAN_LIMIT + 1)
            .execution_options(populate_existing=True)
        )
    )
    if len(variant_rows) > _REFERENCE_INVENTORY_VARIANT_SCAN_LIMIT:
        raise deps.http_error(
            "reference_video_inventory_too_large",
            "reference video inventory is too large to verify safely",
            503,
        )
    return _InventoryRows(
        exact_active=exact_active,
        active_rows=active_rows,
        exact_deleted=exact_deleted,
        pending_rows=pending_rows,
        variant_rows=variant_rows,
    )


async def _prepare_inventory(
    *,
    user_id: str,
    sha256: str,
    db: AsyncSession,
    deps: Any,
    cleanup_page_size: int,
    result_rows: Callable[[Any], list[Any]],
    matching_video: Callable[[list[Any], str], Any | None],
) -> _PreparedInventory:
    try:
        await lock_user_reference_media(
            db,
            user_id=user_id,
            http_error=deps.http_error,
        )
        groups = await _query_locked_inventory(
            user_id=user_id,
            sha256=sha256,
            db=db,
            deps=deps,
            cleanup_page_size=cleanup_page_size,
            result_rows=result_rows,
        )
        rows = groups.rows
        existing = _matching_unclaimed_video(
            rows,
            sha256,
            matching_video=matching_video,
        )
        cleanup_rows = _claim_pending_cleanup_rows(
            pending_rows=groups.pending_rows,
            existing=existing,
            cleanup_page_size=cleanup_page_size,
        )
        row_snapshots = _snapshot_rows(rows)
        variant_snapshots = _snapshot_rows(groups.variant_rows)
        prepared = _PreparedInventory(
            existing=(
                ReferenceVideoSnapshot.from_row(existing)
                if existing is not None
                else None
            ),
            rows=row_snapshots,
            variant_rows=variant_snapshots,
            signature=_inventory_signature(row_snapshots, variant_snapshots),
            cleanup_rows=cleanup_rows,
            cleanup_overflow=len(groups.pending_rows) > cleanup_page_size,
        )
        if cleanup_rows:
            await db.commit()
        else:
            await db.rollback()
        return prepared
    except BaseException:
        await db.rollback()
        raise


async def _reload_inventory(
    *,
    user_id: str,
    sha256: str,
    db: AsyncSession,
    deps: Any,
    cleanup_page_size: int,
    result_rows: Callable[[Any], list[Any]],
    matching_video: Callable[[list[Any], str], Any | None],
) -> _PreparedInventory:
    try:
        await lock_user_reference_media(
            db,
            user_id=user_id,
            http_error=deps.http_error,
        )
        groups = await _query_locked_inventory(
            user_id=user_id,
            sha256=sha256,
            db=db,
            deps=deps,
            cleanup_page_size=cleanup_page_size,
            result_rows=result_rows,
        )
        rows = groups.rows
        existing = _matching_unclaimed_video(
            rows,
            sha256,
            matching_video=matching_video,
        )
        row_snapshots = _snapshot_rows(rows)
        variant_snapshots = _snapshot_rows(groups.variant_rows)
        return _PreparedInventory(
            existing=(
                ReferenceVideoSnapshot.from_row(existing)
                if existing is not None
                else None
            ),
            rows=row_snapshots,
            variant_rows=variant_snapshots,
            signature=_inventory_signature(row_snapshots, variant_snapshots),
            cleanup_rows=(),
            cleanup_overflow=len(groups.pending_rows) > cleanup_page_size,
        )
    finally:
        await db.rollback()


def _inventory_quota(
    *,
    rows: tuple[ReferenceVideoSnapshot, ...],
    variant_rows: tuple[ReferenceVideoSnapshot, ...],
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
) -> ReferenceInventorySnapshot:
    prepared = await _prepare_inventory(
        user_id=user_id,
        sha256=sha256,
        db=db,
        deps=deps,
        cleanup_page_size=cleanup_page_size,
        result_rows=result_rows,
        matching_video=matching_video,
    )
    await _reconcile_aged_adoption_markers(
        user_id=user_id,
        deps=deps,
        clear_adoption_marker=clear_adoption_marker,
        adopted_outcome=adopted_outcome,
        not_adopted_outcome=not_adopted_outcome,
    )

    cleanup_results: list[tuple[ReferenceVideoSnapshot, Any | None]] = []
    if prepared.cleanup_rows:
        try:
            cleanup_results = await _cleanup_pending_inventory(
                cleanup_rows=prepared.cleanup_rows,
                db=db,
                deps=deps,
                snapshot_from_row=ReferenceVideoSnapshot.from_row,
            )
        except BaseException:
            await _persist_cleanup_results(
                user_id=user_id,
                cleanup_results=[(row, None) for row in prepared.cleanup_rows],
                db=db,
                deps=deps,
                result_rows=result_rows,
            )
            raise
        await _persist_cleanup_results(
            user_id=user_id,
            cleanup_results=cleanup_results,
            db=db,
            deps=deps,
            result_rows=result_rows,
        )

    if prepared.cleanup_overflow:
        raise deps.http_error(
            "video_cleanup_backlog",
            "reference video cleanup is still catching up; retry shortly",
            503,
        )

    if cleanup_results:
        prepared = await _reload_inventory(
            user_id=user_id,
            sha256=sha256,
            db=db,
            deps=deps,
            cleanup_page_size=cleanup_page_size,
            result_rows=result_rows,
            matching_video=matching_video,
        )

    inspections = await deps.storage_lifecycle.inspect_many(prepared.rows)
    quota_count, quota_bytes = _inventory_quota(
        rows=prepared.rows,
        variant_rows=prepared.variant_rows,
        inspections=inspections,
    )
    return ReferenceInventorySnapshot(
        existing=prepared.existing,
        rows=prepared.rows,
        variant_rows=prepared.variant_rows,
        inspections=inspections,
        quota_count=quota_count,
        quota_bytes=quota_bytes,
        signature=prepared.signature,
    )


async def lock_reference_inventory_for_adoption(
    *,
    user_id: str,
    sha256: str,
    db: AsyncSession,
    deps: Any,
    cleanup_page_size: int,
    result_rows: Callable[[Any], list[Any]],
    matching_video: Callable[[list[Any], str], Any | None],
) -> LockedReferenceInventory:
    await lock_user_reference_media(
        db,
        user_id=user_id,
        http_error=deps.http_error,
    )
    groups = await _query_locked_inventory(
        user_id=user_id,
        sha256=sha256,
        db=db,
        deps=deps,
        cleanup_page_size=cleanup_page_size,
        result_rows=result_rows,
    )
    rows = groups.rows
    existing = _matching_unclaimed_video(
        rows,
        sha256,
        matching_video=matching_video,
    )
    return LockedReferenceInventory(
        existing=existing,
        rows=rows,
        variant_rows=groups.variant_rows,
        signature=_inventory_signature(rows, groups.variant_rows),
    )
