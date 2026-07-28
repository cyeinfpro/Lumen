"""Normalize local media for Volcano asset submission."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from lumen_core.models import Image, Video
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
    delete_volcano_asset_install,
    ensure_volcano_asset_image_variant,
    ensure_volcano_asset_video_variant,
)
from lumen_core.volcano_assets import volcano_asset_reference_url
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..storage_writes import StorageWriteCoordinator


logger = logging.getLogger(__name__)
_REFERENCE_TOKEN_TTL = timedelta(hours=24)


def ensure_reference_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> str:
    existing_token = str(metadata.get(token_key) or "")
    raw_expires_at = str(metadata.get(expires_key) or "")
    try:
        expires_at = datetime.fromisoformat(raw_expires_at)
    except ValueError:
        expires_at = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if existing_token and expires_at > datetime.now(timezone.utc):
            return existing_token
    token = secrets.token_urlsafe(32)
    metadata[token_key] = token
    metadata[expires_key] = (
        datetime.now(timezone.utc) + _REFERENCE_TOKEN_TTL
    ).isoformat()
    return token


async def _cleanup_install(receipt: VolcanoAssetInstallReceipt | None) -> None:
    if receipt is None:
        return
    try:
        await delete_volcano_asset_install(settings.storage_root, receipt)
    except OSError:
        logger.warning(
            "Volcano source install cleanup failed key=%s",
            receipt.storage_key,
            exc_info=True,
        )


def _not_found(asset_type: str) -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        f"video_asset_{asset_type.lower()}_not_found",
        f"asset {asset_type.lower()} was not found",
        404,
    )


async def _normalized_image_source_url(
    operation: dict[str, Any],
    storage_writes: StorageWriteCoordinator,
) -> tuple[str, str]:
    source_id = str(operation.get("local_source_id") or "")
    user_id = str(operation.get("user_id") or "")
    public_base_url = str(operation.get("public_base_url") or "")
    receipt: VolcanoAssetInstallReceipt | None = None
    committed = False
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
        try:
            _variant, receipt = await ensure_volcano_asset_image_variant(
                session,
                image,
                storage_root=settings.storage_root,
                storage_capacity=storage_writes.capacity,
                storage_lease_ttl_seconds=storage_writes.lease_ttl_seconds,
            )
            image = (
                await session.execute(
                    select(Image)
                    .where(
                        Image.id == source_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                    .with_for_update()
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
            await session.commit()
            committed = True
            return (
                volcano_asset_reference_url(
                    public_base_url,
                    resource_id=image.id,
                    asset_type="Image",
                    token=token,
                ),
                VOLCANO_ASSET_IMAGE_KIND,
            )
        finally:
            if not committed:
                await _cleanup_install(receipt)


async def _normalized_video_source_url(
    operation: dict[str, Any],
    storage_writes: StorageWriteCoordinator,
) -> tuple[str, str]:
    source_id = str(operation.get("local_source_id") or "")
    user_id = str(operation.get("user_id") or "")
    public_base_url = str(operation.get("public_base_url") or "")
    receipt: VolcanoAssetInstallReceipt | None = None
    committed = False
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
        try:
            _variant, receipt = await ensure_volcano_asset_video_variant(
                session,
                video,
                storage_root=settings.storage_root,
                storage_capacity=storage_writes.capacity,
                storage_lease_ttl_seconds=storage_writes.lease_ttl_seconds,
            )
            video = (
                await session.execute(
                    select(Video)
                    .where(
                        Video.id == source_id,
                        Video.user_id == user_id,
                        Video.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if video is None:
                raise _not_found("Video")
            metadata = dict(video.metadata_jsonb or {})
            token = ensure_reference_token(
                metadata,
                token_key="reference_access_token",
                expires_key="reference_access_token_expires_at",
            )
            video.metadata_jsonb = metadata
            await session.commit()
            committed = True
            return (
                volcano_asset_reference_url(
                    public_base_url,
                    resource_id=video.id,
                    asset_type="Video",
                    token=token,
                ),
                VOLCANO_ASSET_VIDEO_KIND,
            )
        finally:
            if not committed:
                await _cleanup_install(receipt)


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
