"""Transaction-safe adoption workflows for normalized Volcano asset media."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from lumen_core.capacity_leases import (
    CapacityLeaseLost,
    maintained_capacity_lease,
)
from lumen_core.storage_capacity import StorageCapacityPort

from .model_entities.accounts import User
from .model_entities.media_workflows import Image, ImageVariant, Video
from .volcano_asset_variant_contracts import (
    VariantWorkflowRuntime,
    database_timeout as _database_timeout,
    prepare_timeout as _prepare_timeout,
    quota_exceeded as _quota_exceeded,
    source_changed as _source_changed,
)
from .volcano_asset_media_types import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
    VOLCANO_ASSET_VIDEO_MIME,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
)


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_RETRY = object()


@dataclass(frozen=True)
class _ImageVariantSnapshot:
    id: str
    storage_key: str
    width: int
    height: int


@dataclass(frozen=True)
class _ImageSourceSnapshot:
    id: str
    user_id: str
    storage_key: str
    sha256: str
    mime: str
    width: int
    height: int
    size_bytes: int
    variant: _ImageVariantSnapshot | None


@dataclass(frozen=True)
class _PreparedImageVariant:
    storage_key: str
    width: int
    height: int
    receipt: VolcanoAssetInstallReceipt | None
    from_snapshot: bool


@dataclass(frozen=True)
class _VideoSourceSnapshot:
    id: str
    user_id: str
    storage_key: str
    sha256: str
    etag: str
    size_bytes: int
    metadata_jsonb: dict[str, Any]
    variant: dict[str, Any] | None


@dataclass(frozen=True)
class _PreparedVideoVariant:
    variant: dict[str, Any]
    receipt: VolcanoAssetInstallReceipt | None
    from_snapshot: bool


def _consume_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _schedule_cleanup(
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt | None,
    runtime: VariantWorkflowRuntime,
) -> None:
    if receipt is None:
        return
    task = asyncio.create_task(
        runtime.cleanup_install_best_effort(storage_root, receipt)
    )
    task.add_done_callback(_consume_task)


async def _cleanup_receipt(
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt | None,
    runtime: VariantWorkflowRuntime,
) -> None:
    if receipt is None:
        return
    task = asyncio.create_task(
        runtime.cleanup_install_best_effort(storage_root, receipt)
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=runtime.cleanup_timeout_seconds,
        )
    except asyncio.CancelledError:
        task.add_done_callback(_consume_task)
        raise
    except TimeoutError:
        task.add_done_callback(_consume_task)


async def _rollback_session(
    db: AsyncSession,
    runtime: VariantWorkflowRuntime,
) -> bool:
    task = asyncio.create_task(
        asyncio.wait_for(
            db.rollback(),
            timeout=runtime.rollback_timeout_seconds,
        )
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_consume_task)
        return False
    except Exception:
        logger.warning("Volcano asset database rollback failed", exc_info=True)
        return False
    return True


def _discard_prepared(
    task: asyncio.Task[_T],
    *,
    storage_root: str,
    runtime: VariantWorkflowRuntime,
) -> None:
    try:
        prepared = task.result()
    except BaseException:
        return
    receipt = getattr(prepared, "receipt", None)
    if isinstance(receipt, VolcanoAssetInstallReceipt):
        _schedule_cleanup(storage_root, receipt, runtime)


async def _run_prepare(
    work: Awaitable[_T],
    *,
    storage_root: str,
    runtime: VariantWorkflowRuntime,
) -> _T:
    task = asyncio.create_task(work)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=runtime.prepare_timeout_seconds,
        )
    except TimeoutError as exc:
        task.cancel()
        task.add_done_callback(
            lambda done: _discard_prepared(
                done,
                storage_root=storage_root,
                runtime=runtime,
            )
        )
        raise _prepare_timeout() from exc
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(
            lambda done: _discard_prepared(
                done,
                storage_root=storage_root,
                runtime=runtime,
            )
        )
        raise


def _attempt_storage_key(
    source_key: str,
    *,
    source_id: str,
    kind: str,
    sha256: str,
    suffix: str,
) -> str:
    source = Path(source_key)
    token = secrets.token_hex(8)
    return str(
        source.with_name(
            f"{source_id}.{kind}.{sha256}.{token}.{suffix}"
        )
    )


def _image_variant_snapshot(
    variant: ImageVariant | None,
) -> _ImageVariantSnapshot | None:
    if variant is None:
        return None
    return _ImageVariantSnapshot(
        id=str(variant.id),
        storage_key=str(variant.storage_key),
        width=int(variant.width),
        height=int(variant.height),
    )


async def _read_image_snapshot(
    db: AsyncSession,
    *,
    image_id: str,
    runtime: VariantWorkflowRuntime,
) -> _ImageSourceSnapshot:
    try:
        async with asyncio.timeout(runtime.finalize_timeout_seconds):
            current = (
                await db.execute(
                    select(Image).where(
                        Image.id == image_id,
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            existing = None
            if current is not None:
                existing = (
                    await db.execute(
                        select(ImageVariant)
                        .where(
                            ImageVariant.image_id == image_id,
                            ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                        )
                        .order_by(ImageVariant.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                snapshot = _ImageSourceSnapshot(
                    id=str(current.id),
                    user_id=str(current.user_id),
                    storage_key=str(current.storage_key),
                    sha256=str(current.sha256),
                    mime=str(current.mime),
                    width=int(current.width),
                    height=int(current.height),
                    size_bytes=max(0, int(current.size_bytes or 0)),
                    variant=_image_variant_snapshot(existing),
                )
            else:
                snapshot = None
    except asyncio.CancelledError:
        await _rollback_session(db, runtime)
        raise
    except TimeoutError as exc:
        await _rollback_session(db, runtime)
        raise _database_timeout() from exc
    except BaseException:
        await _rollback_session(db, runtime)
        raise
    if not await _rollback_session(db, runtime):
        raise _database_timeout()
    if snapshot is None:
        raise VolcanoAssetMediaError("not_found", "image was deleted", 404)
    return snapshot


async def _prepare_image_variant(
    snapshot: _ImageSourceSnapshot,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
    runtime: VariantWorkflowRuntime,
) -> _PreparedImageVariant:
    existing = snapshot.variant
    if existing is not None:
        existing_path = runtime.storage_path(storage_root, existing.storage_key)
        if await asyncio.to_thread(
            runtime.image_variant_file_is_valid,
            existing_path,
            width=existing.width,
            height=existing.height,
        ):
            return _PreparedImageVariant(
                storage_key=existing.storage_key,
                width=existing.width,
                height=existing.height,
                receipt=None,
                from_snapshot=True,
            )

    source_path = runtime.storage_path(storage_root, snapshot.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "image binary is missing", 404)
    rendered = await asyncio.to_thread(
        runtime.make_image_jpeg,
        source_path,
    )
    key = _attempt_storage_key(
        snapshot.storage_key,
        source_id=snapshot.id,
        kind=VOLCANO_ASSET_IMAGE_KIND,
        sha256=rendered.sha256,
        suffix="jpg",
    )
    receipt: VolcanoAssetInstallReceipt | None = None
    try:
        lease = await runtime.reserve_media_capacity(
            storage_capacity,
            rendered.size_bytes,
        )
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=storage_lease_ttl_seconds,
        ) as guard:
            receipt = await runtime.install_rendered_media(
                storage_root=storage_root,
                storage_key=key,
                data=rendered.data,
                sha256=rendered.sha256,
                guard=guard,
            )
        return _PreparedImageVariant(
            storage_key=key,
            width=rendered.width,
            height=rendered.height,
            receipt=receipt,
            from_snapshot=False,
        )
    except CapacityLeaseLost as exc:
        await _cleanup_receipt(storage_root, receipt, runtime)
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity lease was lost",
            503,
        ) from exc
    except asyncio.CancelledError:
        _schedule_cleanup(storage_root, receipt, runtime)
        raise
    except BaseException:
        await _cleanup_receipt(storage_root, receipt, runtime)
        raise


async def _lock_active_user(
    db: AsyncSession,
    *,
    user_id: str,
    asset_type: str,
) -> None:
    locked = (
        await db.execute(
            select(User.id)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        raise VolcanoAssetMediaError(
            "not_found",
            f"{asset_type} was deleted",
            404,
        )


def _image_source_filters(snapshot: _ImageSourceSnapshot) -> tuple[Any, ...]:
    return (
        Image.id == snapshot.id,
        Image.user_id == snapshot.user_id,
        Image.storage_key == snapshot.storage_key,
        Image.sha256 == snapshot.sha256,
        Image.mime == snapshot.mime,
        Image.width == snapshot.width,
        Image.height == snapshot.height,
        Image.size_bytes == snapshot.size_bytes,
        Image.deleted_at.is_(None),
    )


def _rows_affected(result: Any) -> int:
    value = getattr(result, "rowcount", 0)
    return value if isinstance(value, int) and value > 0 else 0


def _sqlite_is_locked(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


async def _insert_image_variant(
    db: AsyncSession,
    snapshot: _ImageSourceSnapshot,
    prepared: _PreparedImageVariant,
) -> ImageVariant | object:
    variant = ImageVariant(
        image_id=snapshot.id,
        kind=VOLCANO_ASSET_IMAGE_KIND,
        storage_key=prepared.storage_key,
        width=prepared.width,
        height=prepared.height,
    )
    try:
        async with db.begin_nested():
            db.add(variant)
            await db.flush()
    except IntegrityError:
        if variant in db:
            db.expunge(variant)
        return _RETRY
    return variant


async def _update_image_variant(
    db: AsyncSession,
    current: ImageVariant,
    expected: _ImageVariantSnapshot,
    prepared: _PreparedImageVariant,
) -> ImageVariant | object:
    result = await db.execute(
        update(ImageVariant)
        .where(
            ImageVariant.id == expected.id,
            ImageVariant.image_id == current.image_id,
            ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
            ImageVariant.storage_key == expected.storage_key,
            ImageVariant.width == expected.width,
            ImageVariant.height == expected.height,
        )
        .values(
            storage_key=prepared.storage_key,
            width=prepared.width,
            height=prepared.height,
        )
        .execution_options(synchronize_session=False)
    )
    if _rows_affected(result) != 1:
        return _RETRY
    set_committed_value(current, "storage_key", prepared.storage_key)
    set_committed_value(current, "width", prepared.width)
    set_committed_value(current, "height", prepared.height)
    return current


async def _finalize_image_variant_tx(
    db: AsyncSession,
    snapshot: _ImageSourceSnapshot,
    prepared: _PreparedImageVariant,
) -> ImageVariant | object:
    await _lock_active_user(
        db,
        user_id=snapshot.user_id,
        asset_type="image",
    )
    current_image = (
        await db.execute(
            select(Image)
            .where(*_image_source_filters(snapshot))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_image is None:
        raise _source_changed("image")
    current = (
        await db.execute(
            select(ImageVariant)
            .where(
                ImageVariant.image_id == snapshot.id,
                ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
            )
            .order_by(ImageVariant.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current_snapshot = _image_variant_snapshot(current)
    if prepared.from_snapshot:
        return current if current_snapshot == snapshot.variant else _RETRY
    if current_snapshot != snapshot.variant:
        return _RETRY
    if current is None:
        return await _insert_image_variant(db, snapshot, prepared)
    if current_snapshot is None:
        return _RETRY
    return await _update_image_variant(
        db,
        current,
        current_snapshot,
        prepared,
    )


async def _finalize_image_variant(
    db: AsyncSession,
    snapshot: _ImageSourceSnapshot,
    prepared: _PreparedImageVariant,
    runtime: VariantWorkflowRuntime,
) -> ImageVariant | object:
    try:
        async with asyncio.timeout(runtime.finalize_timeout_seconds):
            return await _finalize_image_variant_tx(db, snapshot, prepared)
    except TimeoutError as exc:
        raise _database_timeout() from exc
    except OperationalError as exc:
        if _sqlite_is_locked(exc):
            return _RETRY
        raise


async def _finish_attempt_failure(
    db: AsyncSession,
    *,
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt | None,
    background_cleanup: bool,
    runtime: VariantWorkflowRuntime,
) -> bool:
    rolled_back = await _rollback_session(db, runtime)
    if not rolled_back:
        return False
    if background_cleanup:
        _schedule_cleanup(storage_root, receipt, runtime)
    else:
        await _cleanup_receipt(storage_root, receipt, runtime)
    return True


async def ensure_image_variant(
    db: AsyncSession,
    image: Image,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
    runtime: VariantWorkflowRuntime,
) -> tuple[ImageVariant, VolcanoAssetInstallReceipt | None]:
    image_id = str(image.id)
    for attempt in range(runtime.convergence_attempts):
        snapshot = await _read_image_snapshot(
            db,
            image_id=image_id,
            runtime=runtime,
        )
        prepared = await _run_prepare(
            _prepare_image_variant(
                snapshot,
                storage_root=storage_root,
                storage_capacity=storage_capacity,
                storage_lease_ttl_seconds=storage_lease_ttl_seconds,
                runtime=runtime,
            ),
            storage_root=storage_root,
            runtime=runtime,
        )
        try:
            result = await _finalize_image_variant(
                db,
                snapshot,
                prepared,
                runtime,
            )
        except asyncio.CancelledError:
            await _finish_attempt_failure(
                db,
                storage_root=storage_root,
                receipt=prepared.receipt,
                background_cleanup=True,
                runtime=runtime,
            )
            raise
        except BaseException:
            await _finish_attempt_failure(
                db,
                storage_root=storage_root,
                receipt=prepared.receipt,
                background_cleanup=False,
                runtime=runtime,
            )
            raise
        if result is not _RETRY:
            return result, prepared.receipt
        if not await _finish_attempt_failure(
            db,
            storage_root=storage_root,
            receipt=prepared.receipt,
            background_cleanup=False,
            runtime=runtime,
        ):
            raise _database_timeout()
        if attempt + 1 < runtime.convergence_attempts:
            await asyncio.sleep(0)
    raise _source_changed("image")


async def _read_video_snapshot(
    db: AsyncSession,
    *,
    video_id: str,
    runtime: VariantWorkflowRuntime,
) -> _VideoSourceSnapshot:
    try:
        async with asyncio.timeout(runtime.finalize_timeout_seconds):
            current = (
                await db.execute(
                    select(Video)
                    .where(
                        Video.id == video_id,
                        Video.deleted_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if current is None:
                snapshot = None
            else:
                metadata = dict(current.metadata_jsonb or {})
                snapshot = _VideoSourceSnapshot(
                    id=str(current.id),
                    user_id=str(current.user_id),
                    storage_key=str(current.storage_key),
                    sha256=str(current.sha256),
                    etag=str(current.etag),
                    size_bytes=max(0, int(current.size_bytes or 0)),
                    metadata_jsonb=metadata,
                    variant=runtime.video_variant_metadata(current),
                )
    except asyncio.CancelledError:
        await _rollback_session(db, runtime)
        raise
    except TimeoutError as exc:
        await _rollback_session(db, runtime)
        raise _database_timeout() from exc
    except BaseException:
        await _rollback_session(db, runtime)
        raise
    if not await _rollback_session(db, runtime):
        raise _database_timeout()
    if snapshot is None:
        raise VolcanoAssetMediaError("not_found", "video was deleted", 404)
    return snapshot


def _video_variant(
    rendered: Any,
    *,
    storage_key: str,
) -> dict[str, Any]:
    return {
        "kind": VOLCANO_ASSET_VIDEO_KIND,
        "storage_key": storage_key,
        "mime": VOLCANO_ASSET_VIDEO_MIME,
        "width": rendered.width,
        "height": rendered.height,
        "duration_ms": rendered.duration_ms,
        "fps": rendered.fps,
        "has_audio": rendered.has_audio,
        "size_bytes": rendered.size_bytes,
        "sha256": rendered.sha256,
    }


async def _prepare_video_variant(
    snapshot: _VideoSourceSnapshot,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
    runtime: VariantWorkflowRuntime,
) -> _PreparedVideoVariant:
    if snapshot.variant is not None:
        existing_path = runtime.storage_path(
            storage_root,
            str(snapshot.variant["storage_key"]),
        )
        if await asyncio.to_thread(
            runtime.video_variant_file_is_valid,
            existing_path,
            snapshot.variant,
        ):
            return _PreparedVideoVariant(
                variant=snapshot.variant,
                receipt=None,
                from_snapshot=True,
            )

    source_path = runtime.storage_path(storage_root, snapshot.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)
    async with runtime.video_transcode_semaphore():
        rendered = await asyncio.to_thread(
            runtime.make_video_mp4,
            source_path,
        )
    key = _attempt_storage_key(
        snapshot.storage_key,
        source_id=snapshot.id,
        kind=VOLCANO_ASSET_VIDEO_KIND,
        sha256=rendered.sha256,
        suffix="mp4",
    )
    variant = _video_variant(rendered, storage_key=key)
    receipt: VolcanoAssetInstallReceipt | None = None
    try:
        lease = await runtime.reserve_media_capacity(
            storage_capacity,
            rendered.size_bytes,
        )
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=storage_lease_ttl_seconds,
        ) as guard:
            receipt = await runtime.install_rendered_media(
                storage_root=storage_root,
                storage_key=key,
                data=rendered.data,
                sha256=rendered.sha256,
                guard=guard,
            )
        return _PreparedVideoVariant(
            variant=variant,
            receipt=receipt,
            from_snapshot=False,
        )
    except CapacityLeaseLost as exc:
        await _cleanup_receipt(storage_root, receipt, runtime)
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity lease was lost",
            503,
        ) from exc
    except asyncio.CancelledError:
        _schedule_cleanup(storage_root, receipt, runtime)
        raise
    except BaseException:
        await _cleanup_receipt(storage_root, receipt, runtime)
        raise


def _video_source_filters(snapshot: _VideoSourceSnapshot) -> tuple[Any, ...]:
    return (
        Video.id == snapshot.id,
        Video.user_id == snapshot.user_id,
        Video.storage_key == snapshot.storage_key,
        Video.sha256 == snapshot.sha256,
        Video.etag == snapshot.etag,
        Video.size_bytes == snapshot.size_bytes,
        Video.deleted_at.is_(None),
    )


async def _reference_storage_usage(
    db: AsyncSession,
    *,
    user_id: str,
    runtime: VariantWorkflowRuntime,
) -> int:
    cleanup_state = Video.metadata_jsonb["video_storage_cleanup"][
        "state"
    ].as_string()
    upstream_variant_key = Video.metadata_jsonb[
        "upstream_reference_video_variant"
    ]["storage_key"].as_string()
    volcano_variant_key = Video.metadata_jsonb[VOLCANO_ASSET_VIDEO_METADATA_KEY][
        "storage_key"
    ].as_string()
    rows = runtime.result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                or_(
                    Video.storage_key.like(f"u/{user_id}/vref/%"),
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
            .limit(runtime.reference_scan_limit + 1)
        )
    )
    if len(rows) > runtime.reference_scan_limit:
        raise _quota_exceeded()
    return sum(runtime.video_reference_declared_bytes(row) for row in rows)


async def _finalize_video_variant_tx(
    db: AsyncSession,
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    runtime: VariantWorkflowRuntime,
) -> dict[str, Any] | object:
    await _lock_active_user(
        db,
        user_id=snapshot.user_id,
        asset_type="video",
    )
    current = (
        await db.execute(
            select(Video)
            .where(*_video_source_filters(snapshot))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current is None:
        raise _source_changed("video")
    current_variant = runtime.video_variant_metadata(current)
    if prepared.from_snapshot:
        return current_variant if current_variant == prepared.variant else _RETRY
    if current_variant != snapshot.variant:
        return _RETRY

    current_bytes = await _reference_storage_usage(
        db,
        user_id=snapshot.user_id,
        runtime=runtime,
    )
    replaced_bytes = runtime.video_variant_quota_bytes(
        current,
        VOLCANO_ASSET_VIDEO_METADATA_KEY,
    )
    variant_bytes = int(prepared.variant["size_bytes"])
    projected_bytes = (
        current_bytes - min(current_bytes, replaced_bytes)
    ) + variant_bytes
    if (
        projected_bytes > runtime.video_reference_storage_quota_bytes
        and projected_bytes > current_bytes
    ):
        raise _quota_exceeded()

    current_metadata = dict(current.metadata_jsonb or {})
    next_metadata = dict(current_metadata)
    next_metadata[VOLCANO_ASSET_VIDEO_METADATA_KEY] = prepared.variant
    result = await db.execute(
        update(Video)
        .where(
            *_video_source_filters(snapshot),
            Video.metadata_jsonb == current_metadata,
        )
        .values(metadata_jsonb=next_metadata)
        .execution_options(synchronize_session=False)
    )
    if _rows_affected(result) != 1:
        return _RETRY
    set_committed_value(current, "metadata_jsonb", next_metadata)
    return prepared.variant


async def _finalize_video_variant(
    db: AsyncSession,
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    runtime: VariantWorkflowRuntime,
) -> dict[str, Any] | object:
    try:
        async with asyncio.timeout(runtime.finalize_timeout_seconds):
            return await _finalize_video_variant_tx(
                db,
                snapshot,
                prepared,
                runtime,
            )
    except TimeoutError as exc:
        raise _database_timeout() from exc
    except OperationalError as exc:
        if _sqlite_is_locked(exc):
            return _RETRY
        raise


async def ensure_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
    runtime: VariantWorkflowRuntime,
) -> tuple[dict[str, Any], VolcanoAssetInstallReceipt | None]:
    video_id = str(video.id)
    for attempt in range(runtime.convergence_attempts):
        snapshot = await _read_video_snapshot(
            db,
            video_id=video_id,
            runtime=runtime,
        )
        prepared = await _run_prepare(
            _prepare_video_variant(
                snapshot,
                storage_root=storage_root,
                storage_capacity=storage_capacity,
                storage_lease_ttl_seconds=storage_lease_ttl_seconds,
                runtime=runtime,
            ),
            storage_root=storage_root,
            runtime=runtime,
        )
        try:
            result = await _finalize_video_variant(
                db,
                snapshot,
                prepared,
                runtime,
            )
        except asyncio.CancelledError:
            await _finish_attempt_failure(
                db,
                storage_root=storage_root,
                receipt=prepared.receipt,
                background_cleanup=True,
                runtime=runtime,
            )
            raise
        except BaseException:
            await _finish_attempt_failure(
                db,
                storage_root=storage_root,
                receipt=prepared.receipt,
                background_cleanup=False,
                runtime=runtime,
            )
            raise
        if result is not _RETRY:
            return result, prepared.receipt
        if not await _finish_attempt_failure(
            db,
            storage_root=storage_root,
            receipt=prepared.receipt,
            background_cleanup=False,
            runtime=runtime,
        ):
            raise _database_timeout()
        if attempt + 1 < runtime.convergence_attempts:
            await asyncio.sleep(0)
    raise _source_changed("video")


__all__ = [
    "VariantWorkflowRuntime",
    "ensure_image_variant",
    "ensure_video_variant",
]
