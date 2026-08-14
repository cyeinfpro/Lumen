"""Normalize local media for Volcano asset submission."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lumen_core import volcano_asset_media as asset_media
from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    CapacityLeaseLost,
    maintained_capacity_lease,
    race_with_capacity_lease,
)
from lumen_core.models import Image, ImageVariant, User, Video
from lumen_core.volcano_asset_media import (
    VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
    VolcanoAssetVideoMp4,
    delete_volcano_asset_install,
    ensure_volcano_asset_image_variant,
    make_volcano_asset_video_mp4,
    volcano_asset_video_variant_metadata,
)
from lumen_core.volcano_assets import volcano_asset_reference_url
from sqlalchemy import select, text, update

from ..config import settings
from ..db import SessionLocal, affected_rows
from ..storage_writes import StorageWriteCoordinator
from .volcano_asset_background import (
    cleanup_install_in_background as _start_install_cleanup,
)
from .volcano_asset_background import (
    cleanup_receipt_in_background as _start_receipt_cleanup,
)
from .volcano_asset_background import (
    discard_path_in_background as _discard_path_in_background,
)
from .volcano_asset_background import track_background_task as _track_background_task
from .volcano_asset_file_io import (
    install_video_stage_atomic as _install_video_stage_atomic,
    write_video_stage as _write_video_stage,
)
from .volcano_asset_video_accounting import (
    quota_exceeded as _quota_exceeded,
    reference_storage_usage as _bounded_reference_storage_usage,
    variant_receipt as _variant_receipt,
)
from .volcano_assets_parts.source_common import ensure_reference_token
from .volcano_assets_parts.source_common import source_not_found as _not_found
from .volcano_assets_parts.source_video import (
    PreparedVideoVariant as _PreparedVideoVariant,
)
from .volcano_assets_parts.source_video import (
    StagedVideoVariant as _StagedVideoVariant,
)
from .volcano_assets_parts.source_video import (
    VideoSourceSnapshot as _VideoSourceSnapshot,
)
from .volcano_assets_parts.source_video import (
    video_poster_metadata as _video_poster_metadata,
)
from .volcano_assets_parts.source_video import (
    video_poster_storage_key as _video_poster_storage_key,
)
from .volcano_assets_parts.source_video import video_stage_path as _video_stage_path
from .volcano_assets_parts.source_video import video_variant as _video_variant
from .volcano_assets_parts.source_video import (
    video_variant_storage_key as _video_variant_storage_key,
)


logger = logging.getLogger(__name__)
_IMAGE_COMMIT_TIMEOUT_SECONDS = 10.0
_IMAGE_ADOPTION_PROBE_TIMEOUT_SECONDS = 5.0
_VIDEO_TRANSCODE_TOTAL_TIMEOUT_SECONDS = 360.0
_VIDEO_TRANSCODE_PROCESS_TIMEOUT_SECONDS = 270.0
_VIDEO_PREPARE_TOTAL_TIMEOUT_SECONDS = 390.0
_VIDEO_ADOPTION_TOTAL_TIMEOUT_SECONDS = 15.0
_VIDEO_ADOPTION_PROBE_TIMEOUT_SECONDS = 5.0
_VIDEO_DB_LOCK_TIMEOUT_MS = 5_000
_VIDEO_DB_STATEMENT_TIMEOUT_MS = 12_000
_VIDEO_REFERENCE_SCAN_LIMIT = 2_048
_VIDEO_ADOPTION_CAS_ATTEMPTS = 3
_VIDEO_SNAPSHOT_ATTEMPTS = 2


@dataclass
class _VideoAdoptionState:
    committed: bool = False
    commit_started: bool = False
    cleanup_safe: bool = True
    reference_token: str | None = None
    replaced_receipt: VolcanoAssetInstallReceipt | None = None


@dataclass
class _ImageAdoptionState:
    committed: bool = False
    commit_started: bool = False
    cleanup_safe: bool = True
    reference_token: str | None = None
    variant_storage_key: str | None = None


class _RetryVideoSnapshot(RuntimeError):
    pass


_CAS_RETRY = object()


async def _cleanup_install(receipt: VolcanoAssetInstallReceipt | None) -> None:
    if receipt is None:
        return
    try:
        await delete_volcano_asset_install(settings.storage_root, receipt)
    except (OSError, VolcanoAssetMediaError):
        logger.warning(
            "Volcano source install cleanup failed key=%s",
            receipt.storage_key,
            exc_info=True,
        )


async def _normalized_image_source_url(
    operation: dict[str, Any],
    storage_writes: StorageWriteCoordinator,
) -> tuple[str, str]:
    source_id = str(operation.get("local_source_id") or "")
    user_id = str(operation.get("user_id") or "")
    public_base_url = str(operation.get("public_base_url") or "")
    receipt: VolcanoAssetInstallReceipt | None = None
    state = _ImageAdoptionState()
    commit_error: BaseException | None = None
    token = ""
    try:
        async with SessionLocal() as session:
            image = (
                await session.execute(
                    select(Image).where(
                        Image.id == source_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if image is None:
                raise _not_found("Image")
            variant, receipt = await ensure_volcano_asset_image_variant(
                session,
                image,
                storage_root=settings.storage_root,
                storage_capacity=storage_writes.capacity,
                storage_lease_ttl_seconds=storage_writes.lease_ttl_seconds,
            )
            active_user = (
                await session.execute(
                    select(User.id)
                    .where(
                        User.id == user_id,
                        User.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active_user is None:
                raise _not_found("Image")
            image = (
                await session.execute(
                    select(Image)
                    .where(
                        Image.id == source_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if image is None:
                raise _not_found("Image")
            metadata = dict(image.metadata_jsonb or {})
            token = ensure_reference_token(
                metadata,
                token_key="video_reference_access_token",
                expires_key="video_reference_access_token_expires_at",
            )
            image.metadata_jsonb = metadata
            state.reference_token = token
            state.variant_storage_key = str(variant.storage_key)
            state.commit_started = True
            state.cleanup_safe = False
            try:
                async with asyncio.timeout(_IMAGE_COMMIT_TIMEOUT_SECONDS):
                    await session.commit()
                state.committed = True
            except BaseException as exc:
                commit_error = exc

        if commit_error is not None and state.commit_started:
            state.committed = await _image_adoption_is_durable(
                source_id=source_id,
                user_id=user_id,
                state=state,
            )
        if isinstance(commit_error, asyncio.CancelledError):
            raise commit_error
        if commit_error is not None and not state.committed:
            if isinstance(commit_error, TimeoutError):
                raise VolcanoAssetMediaError(
                    "image_reference_database_timeout",
                    "image reference adoption timed out",
                    503,
                ) from commit_error
            raise commit_error
        operation["preview_url"] = f"/api/images/{source_id}/binary"
        return (
            volcano_asset_reference_url(
                public_base_url,
                resource_id=source_id,
                asset_type="Image",
                token=token,
            ),
            VOLCANO_ASSET_IMAGE_KIND,
        )
    finally:
        if not state.committed and state.cleanup_safe:
            _cleanup_receipt_in_background(
                receipt,
                task_name="volcano-discard-unadopted-image",
                storage_writes=storage_writes,
            )


async def _image_adoption_is_durable(
    *,
    source_id: str,
    user_id: str,
    state: _ImageAdoptionState,
) -> bool:
    token = state.reference_token
    variant_storage_key = state.variant_storage_key
    if not token or not variant_storage_key:
        return False
    try:
        async with asyncio.timeout(_IMAGE_ADOPTION_PROBE_TIMEOUT_SECONDS):
            async with SessionLocal() as session:
                image = (
                    await session.execute(
                        select(Image).where(
                            Image.id == source_id,
                            Image.user_id == user_id,
                            Image.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                variant = (
                    await session.execute(
                        select(ImageVariant.id).where(
                            ImageVariant.image_id == source_id,
                            ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                            ImageVariant.storage_key == variant_storage_key,
                        )
                    )
                ).scalar_one_or_none()
                await session.rollback()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Volcano image adoption durability probe failed image_id=%s",
            source_id,
            exc_info=True,
        )
        return False
    metadata = dict(image.metadata_jsonb or {}) if image is not None else {}
    return bool(
        variant is not None and metadata.get("video_reference_access_token") == token
    )


async def _video_source_snapshot(
    *,
    source_id: str,
    user_id: str,
) -> _VideoSourceSnapshot:
    async with SessionLocal() as session:
        video = (
            await session.execute(
                select(Video).where(
                    Video.id == source_id,
                    Video.user_id == user_id,
                    Video.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if video is None:
            raise _not_found("Video")
        snapshot = _VideoSourceSnapshot(
            id=str(video.id),
            user_id=str(video.user_id),
            storage_key=str(video.storage_key),
            sha256=str(video.sha256),
            etag=str(video.etag),
            size_bytes=max(0, int(video.size_bytes or 0)),
            metadata_jsonb=dict(video.metadata_jsonb or {}),
            poster_storage_key=(
                str(getattr(video, "poster_storage_key", "") or "")
                if getattr(video, "poster_storage_key", None)
                else None
            ),
        )
        await session.rollback()
        return snapshot


def _cleanup_install_in_background(
    task: asyncio.Task[Any],
    *,
    storage_key: str,
    size_bytes: int,
    sha256: str,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    _start_install_cleanup(
        task,
        VolcanoAssetInstallReceipt(
            storage_key=storage_key,
            size_bytes=size_bytes,
            sha256=sha256,
        ),
        _cleanup_install,
        storage_writes,
    )


def _cleanup_receipt_in_background(
    receipt: VolcanoAssetInstallReceipt | None,
    *,
    task_name: str,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    _start_receipt_cleanup(
        receipt,
        _cleanup_install,
        task_name=task_name,
        storage_writes=storage_writes,
    )


async def _install_video_stage(
    *,
    storage_key: str,
    staged_path: Path,
    size_bytes: int,
    sha256: str,
    guard: CapacityLeaseGuard,
    storage_writes: StorageWriteCoordinator | None = None,
) -> VolcanoAssetInstallReceipt | None:
    destination = asset_media._storage_path(settings.storage_root, storage_key)
    install_task = asyncio.create_task(
        asyncio.to_thread(
            _install_video_stage_atomic,
            destination,
            staged_path,
            size_bytes=size_bytes,
            sha256=sha256,
        )
    )
    created = False
    try:
        created = bool(
            await race_with_capacity_lease(
                asyncio.shield(install_task),
                guard,
            )
        )
    except BaseException:
        _cleanup_install_in_background(
            install_task,
            storage_key=storage_key,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_writes=storage_writes,
        )
        raise
    if not created:
        return None
    return VolcanoAssetInstallReceipt(
        storage_key=storage_key,
        size_bytes=size_bytes,
        sha256=sha256,
    )


async def _render_video(
    snapshot: _VideoSourceSnapshot,
) -> VolcanoAssetVideoMp4:
    source_path = asset_media._storage_path(
        settings.storage_root,
        snapshot.storage_key,
    )
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)
    async with asset_media._video_transcode_semaphore():
        return await asyncio.to_thread(
            make_volcano_asset_video_mp4,
            source_path,
            timeout_seconds=_VIDEO_TRANSCODE_PROCESS_TIMEOUT_SECONDS,
        )


async def _transcode_video_to_stage(
    snapshot: _VideoSourceSnapshot,
    storage_writes: StorageWriteCoordinator | None = None,
    *,
    include_poster: bool = True,
) -> _StagedVideoVariant:
    render_task = asyncio.create_task(_render_video(snapshot))
    staged_path: Path | None = None
    completed = False
    try:
        try:
            async with asyncio.timeout(_VIDEO_TRANSCODE_TOTAL_TIMEOUT_SECONDS):
                try:
                    rendered = await asyncio.shield(render_task)
                except asyncio.CancelledError:
                    _track_background_task(render_task, storage_writes)
                    raise
                staged_path = _video_stage_path(snapshot)
                write_task = asyncio.create_task(
                    asyncio.to_thread(
                        _write_video_stage,
                        staged_path,
                        rendered,
                    )
                )
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    _discard_path_in_background(
                        write_task,
                        staged_path,
                        storage_writes,
                    )
                    raise
        except TimeoutError as exc:
            if not render_task.done():
                _track_background_task(render_task, storage_writes)
            raise VolcanoAssetMediaError(
                "volcano_asset_video_transcode_failed",
                "asset video transcoding timed out",
                503,
            ) from exc
        storage_key = _video_variant_storage_key(
            snapshot,
            sha256=rendered.sha256,
        )
        poster_storage_key = (
            _video_poster_storage_key(
                snapshot,
                video_sha256=rendered.sha256,
            )
            if include_poster and rendered.poster_bytes
            else None
        )
        completed = True
        return _StagedVideoVariant(
            path=staged_path,
            variant=_video_variant(
                rendered,
                storage_key=storage_key,
                poster_storage_key=poster_storage_key,
            ),
            poster_bytes=(
                rendered.poster_bytes if poster_storage_key is not None else None
            ),
        )
    finally:
        if staged_path is not None and not completed:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(staged_path.unlink, missing_ok=True),
                name="volcano-discard-incomplete-stage",
            )
            _track_background_task(cleanup_task, storage_writes)


async def _prepare_video_variant_with_deadline(
    snapshot: _VideoSourceSnapshot,
    storage_writes: StorageWriteCoordinator,
) -> _PreparedVideoVariant:
    existing = volcano_asset_video_variant_metadata(snapshot)
    existing_poster = await _video_poster_metadata(snapshot.poster_storage_key)
    if existing is not None and existing_poster is not None:
        existing_path = asset_media._storage_path(
            settings.storage_root,
            str(existing["storage_key"]),
        )
        if await asyncio.to_thread(
            asset_media._video_variant_file_is_valid,
            existing_path,
            existing,
        ):
            if all(
                existing.get(key) == value for key, value in existing_poster.items()
            ):
                return _PreparedVideoVariant(
                    variant=existing,
                    receipt=None,
                    from_snapshot=True,
                )
            return _PreparedVideoVariant(
                variant={**existing, **existing_poster},
                receipt=None,
                from_snapshot=False,
            )

    staged_path: Path | None = None
    receipt: VolcanoAssetInstallReceipt | None = None
    poster_receipt: VolcanoAssetInstallReceipt | None = None
    try:
        raw_staged = (
            await _transcode_video_to_stage(snapshot, storage_writes)
            if existing_poster is None
            else await _transcode_video_to_stage(
                snapshot,
                storage_writes,
                include_poster=False,
            )
        )
        if isinstance(raw_staged, tuple):
            staged = _StagedVideoVariant(
                path=raw_staged[0],
                variant=raw_staged[1],
                poster_bytes=None,
            )
        else:
            staged = raw_staged
        staged_path = staged.path
        variant = staged.variant
        if existing_poster is not None:
            variant.update(existing_poster)
        poster_bytes = staged.poster_bytes
        total_size = int(variant["size_bytes"]) + len(poster_bytes or b"")
        storage_lease = await asset_media._reserve_media_capacity(
            storage_writes.capacity,
            total_size,
        )
        async with maintained_capacity_lease(
            storage_lease,
            ttl_seconds=storage_writes.lease_ttl_seconds,
        ) as guard:
            receipt = await _install_video_stage(
                storage_key=str(variant["storage_key"]),
                staged_path=staged_path,
                size_bytes=int(variant["size_bytes"]),
                sha256=str(variant["sha256"]),
                guard=guard,
                storage_writes=storage_writes,
            )
            poster_storage_key = str(variant.get("poster_storage_key") or "")
            poster_sha256 = str(variant.get("poster_sha256") or "")
            if poster_storage_key and poster_bytes and poster_sha256:
                poster_receipt = await asset_media._install_rendered_media(
                    storage_root=settings.storage_root,
                    storage_key=poster_storage_key,
                    data=poster_bytes,
                    sha256=poster_sha256,
                    guard=guard,
                )
        return _PreparedVideoVariant(
            variant=variant,
            receipt=receipt,
            from_snapshot=False,
            poster_receipt=poster_receipt,
        )
    except CapacityLeaseLost as exc:
        if receipt is not None:
            await _cleanup_install(receipt)
        if poster_receipt is not None:
            await _cleanup_install(poster_receipt)
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity lease was lost",
            503,
        ) from exc
    except BaseException:
        if receipt is not None:
            await _cleanup_install(receipt)
        if poster_receipt is not None:
            await _cleanup_install(poster_receipt)
        raise
    finally:
        if staged_path is not None:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(staged_path.unlink, missing_ok=True),
                name="volcano-discard-finished-stage",
            )
            _track_background_task(cleanup_task, storage_writes)


async def _prepare_video_variant(
    snapshot: _VideoSourceSnapshot,
    storage_writes: StorageWriteCoordinator,
) -> _PreparedVideoVariant:
    try:
        async with asyncio.timeout(_VIDEO_PREPARE_TOTAL_TIMEOUT_SECONDS):
            return await _prepare_video_variant_with_deadline(
                snapshot,
                storage_writes,
            )
    except TimeoutError as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_prepare_timeout",
            "asset video preparation timed out",
            503,
        ) from exc


async def _commit_video_adoption(
    session: Any,
    state: _VideoAdoptionState,
) -> None:
    state.commit_started = True
    state.cleanup_safe = False
    await session.commit()
    state.committed = True


async def _configure_video_adoption_transaction(session: Any) -> None:
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return
    bind = get_bind()
    dialect = getattr(bind, "dialect", None)
    if getattr(dialect, "name", None) != "postgresql":
        return
    await session.execute(
        text(f"SET LOCAL lock_timeout = '{_VIDEO_DB_LOCK_TIMEOUT_MS}ms'")
    )
    await session.execute(
        text(f"SET LOCAL statement_timeout = '{_VIDEO_DB_STATEMENT_TIMEOUT_MS}ms'")
    )


async def _video_adoption_is_durable(
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    state: _VideoAdoptionState,
) -> bool:
    token = state.reference_token
    if not token:
        return False
    try:
        async with asyncio.timeout(_VIDEO_ADOPTION_PROBE_TIMEOUT_SECONDS):
            async with SessionLocal() as session:
                current = (
                    await session.execute(
                        select(Video).where(
                            Video.id == snapshot.id,
                            Video.user_id == snapshot.user_id,
                            Video.storage_key == snapshot.storage_key,
                            Video.sha256 == snapshot.sha256,
                            Video.etag == snapshot.etag,
                            Video.size_bytes == snapshot.size_bytes,
                            Video.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                await session.rollback()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Volcano video adoption durability probe failed video_id=%s",
            snapshot.id,
            exc_info=True,
        )
        return False
    if current is None:
        return False
    metadata = dict(current.metadata_jsonb or {})
    if metadata.get("reference_access_token") != token:
        return False
    expected_poster_storage_key = (
        snapshot.poster_storage_key
        or str(prepared.variant.get("poster_storage_key") or "")
        or None
    )
    if (
        str(getattr(current, "poster_storage_key", "") or "") or None
    ) != expected_poster_storage_key:
        return False
    if prepared.from_snapshot:
        return True
    current_variant = volcano_asset_video_variant_metadata(current)
    return bool(
        current_variant is not None
        and current_variant.get("storage_key") == prepared.variant.get("storage_key")
        and current_variant.get("sha256") == prepared.variant.get("sha256")
    )


def _finish_confirmed_video_adoption(
    state: _VideoAdoptionState,
    storage_writes: StorageWriteCoordinator | None = None,
) -> None:
    state.committed = True
    _cleanup_receipt_in_background(
        state.replaced_receipt,
        task_name="volcano-discard-replaced-video",
        storage_writes=storage_writes,
    )
    state.replaced_receipt = None


async def _adopt_video_variant_transaction(
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    state: _VideoAdoptionState,
) -> str | object:
    state.reference_token = None
    state.replaced_receipt = None
    async with SessionLocal() as session:
        await _configure_video_adoption_transaction(session)
        user = (
            await session.execute(
                select(User.id)
                .where(
                    User.id == snapshot.user_id,
                    User.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if user is None:
            raise _not_found("Video")
        current = (
            await session.execute(
                select(Video)
                .where(
                    Video.id == snapshot.id,
                    Video.user_id == snapshot.user_id,
                    Video.storage_key == snapshot.storage_key,
                    Video.sha256 == snapshot.sha256,
                    Video.etag == snapshot.etag,
                    Video.size_bytes == snapshot.size_bytes,
                    Video.deleted_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise VolcanoAssetMediaError(
                "video_reference_changed",
                "video changed or was deleted during asset preparation",
                409,
            )

        current_variant = volcano_asset_video_variant_metadata(current)
        if (
            str(getattr(current, "poster_storage_key", "") or "") or None
        ) != snapshot.poster_storage_key:
            await session.rollback()
            raise _RetryVideoSnapshot
        if prepared.from_snapshot and current_variant != prepared.variant:
            await session.rollback()
            raise _RetryVideoSnapshot

        current_metadata = dict(current.metadata_jsonb or {})
        replacement_key = str(prepared.variant["storage_key"])
        replaced_receipt: VolcanoAssetInstallReceipt | None = None
        if not prepared.from_snapshot:
            current_bytes = await _bounded_reference_storage_usage(
                session,
                user_id=snapshot.user_id,
                scan_limit=_VIDEO_REFERENCE_SCAN_LIMIT,
            )
            replaced_bytes = asset_media._video_variant_quota_bytes(
                current,
                VOLCANO_ASSET_VIDEO_METADATA_KEY,
            )
            projected_bytes = (
                current_bytes - min(current_bytes, replaced_bytes)
            ) + asset_media._video_variant_payload_bytes(prepared.variant)
            if (
                projected_bytes > VIDEO_REFERENCE_STORAGE_QUOTA_BYTES
                and projected_bytes > current_bytes
            ):
                raise _quota_exceeded()
            replaced_receipt = _variant_receipt(
                current_variant,
                replacement_key=replacement_key,
            )
            current_metadata[VOLCANO_ASSET_VIDEO_METADATA_KEY] = prepared.variant

        poster_storage_key = (
            snapshot.poster_storage_key
            or str(prepared.variant.get("poster_storage_key") or "")
            or None
        )
        token = ensure_reference_token(
            current_metadata,
            token_key="reference_access_token",
            expires_key="reference_access_token_expires_at",
        )
        result = await session.execute(
            update(Video)
            .where(
                Video.id == snapshot.id,
                Video.user_id == snapshot.user_id,
                Video.storage_key == snapshot.storage_key,
                Video.sha256 == snapshot.sha256,
                Video.etag == snapshot.etag,
                Video.size_bytes == snapshot.size_bytes,
                Video.poster_storage_key == snapshot.poster_storage_key,
                Video.metadata_jsonb == dict(current.metadata_jsonb or {}),
                Video.deleted_at.is_(None),
            )
            .values(
                metadata_jsonb=current_metadata,
                poster_storage_key=poster_storage_key,
            )
        )
        if affected_rows(result) != 1:
            await session.rollback()
            return _CAS_RETRY

        state.reference_token = token
        state.replaced_receipt = replaced_receipt
        await _commit_video_adoption(session, state)
        return token


async def _adopt_video_variant_once(
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    state: _VideoAdoptionState,
    storage_writes: StorageWriteCoordinator | None = None,
) -> str | object:
    try:
        async with asyncio.timeout(_VIDEO_ADOPTION_TOTAL_TIMEOUT_SECONDS):
            result = await _adopt_video_variant_transaction(
                snapshot,
                prepared,
                state,
            )
    except asyncio.CancelledError:
        if state.commit_started and await _video_adoption_is_durable(
            snapshot,
            prepared,
            state,
        ):
            _finish_confirmed_video_adoption(state, storage_writes)
        raise
    except TimeoutError as exc:
        if state.commit_started and await _video_adoption_is_durable(
            snapshot,
            prepared,
            state,
        ):
            _finish_confirmed_video_adoption(state, storage_writes)
            return str(state.reference_token)
        raise VolcanoAssetMediaError(
            "video_reference_database_timeout",
            "video reference adoption timed out",
            503,
        ) from exc
    except BaseException:
        if state.commit_started and await _video_adoption_is_durable(
            snapshot,
            prepared,
            state,
        ):
            _finish_confirmed_video_adoption(state, storage_writes)
            return str(state.reference_token)
        raise
    if state.committed:
        _finish_confirmed_video_adoption(state, storage_writes)
    return result


async def _adopt_video_variant(
    snapshot: _VideoSourceSnapshot,
    prepared: _PreparedVideoVariant,
    state: _VideoAdoptionState,
    storage_writes: StorageWriteCoordinator | None = None,
) -> str:
    for _attempt in range(_VIDEO_ADOPTION_CAS_ATTEMPTS):
        result = await _adopt_video_variant_once(
            snapshot,
            prepared,
            state,
            storage_writes,
        )
        if result is not _CAS_RETRY:
            return str(result)
    raise VolcanoAssetMediaError(
        "video_reference_changed",
        "video changed during asset adoption",
        409,
    )


async def _normalized_video_source_url(
    operation: dict[str, Any],
    storage_writes: StorageWriteCoordinator,
) -> tuple[str, str]:
    source_id = str(operation.get("local_source_id") or "")
    user_id = str(operation.get("user_id") or "")
    public_base_url = str(operation.get("public_base_url") or "")
    for snapshot_attempt in range(_VIDEO_SNAPSHOT_ATTEMPTS):
        snapshot = await _video_source_snapshot(
            source_id=source_id,
            user_id=user_id,
        )
        prepared = await _prepare_video_variant(snapshot, storage_writes)
        state = _VideoAdoptionState()
        try:
            try:
                token = await _adopt_video_variant(
                    snapshot,
                    prepared,
                    state,
                    storage_writes,
                )
            except _RetryVideoSnapshot:
                if snapshot_attempt + 1 < _VIDEO_SNAPSHOT_ATTEMPTS:
                    continue
                raise VolcanoAssetMediaError(
                    "video_reference_changed",
                    "video changed during asset preparation",
                    409,
                ) from None
            poster_storage_key = (
                snapshot.poster_storage_key
                or str(prepared.variant.get("poster_storage_key") or "")
                or None
            )
            if poster_storage_key:
                operation["preview_url"] = f"/api/videos/{snapshot.id}/poster"
            return (
                volcano_asset_reference_url(
                    public_base_url,
                    resource_id=snapshot.id,
                    asset_type="Video",
                    token=token,
                ),
                VOLCANO_ASSET_VIDEO_KIND,
            )
        finally:
            if not state.committed and state.cleanup_safe:
                _cleanup_receipt_in_background(
                    prepared.receipt,
                    task_name="volcano-discard-unadopted-video",
                    storage_writes=storage_writes,
                )
                _cleanup_receipt_in_background(
                    prepared.poster_receipt,
                    task_name="volcano-discard-unadopted-video-poster",
                    storage_writes=storage_writes,
                )
    raise VolcanoAssetMediaError(
        "video_reference_changed",
        "video changed during asset preparation",
        409,
    )


async def normalized_source_url(
    operation: dict[str, Any],
    *,
    storage_writes: StorageWriteCoordinator | None = None,
) -> tuple[str, str]:
    if storage_writes is None:
        raise RuntimeError(
            "storage_write_coordinator is required for Volcano asset media"
        )
    asset_type = str(operation.get("asset_type") or "")
    if asset_type == "Image":
        return await _normalized_image_source_url(operation, storage_writes)
    if asset_type == "Video":
        return await _normalized_video_source_url(operation, storage_writes)
    raise VolcanoAssetMediaError(
        "video_asset_type_invalid",
        "asset type must be Image or Video",
        422,
    )


__all__ = ["ensure_reference_token", "normalized_source_url"]
