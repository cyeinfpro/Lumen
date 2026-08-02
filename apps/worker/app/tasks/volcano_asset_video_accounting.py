"""Bounded storage accounting helpers for Volcano video variants."""

from __future__ import annotations

from typing import Any

from lumen_core import volcano_asset_media as asset_media
from lumen_core.model_entities.media_workflows import Video
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
)
from sqlalchemy import or_, select


def quota_exceeded() -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        "reference_video_quota_exceeded",
        "reference video storage quota exceeded",
        429,
    )


async def reference_storage_usage(
    session: Any,
    *,
    user_id: str,
    scan_limit: int,
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
    rows = asset_media._result_rows(
        await session.execute(
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
            .limit(scan_limit + 1)
        )
    )
    if len(rows) > scan_limit:
        raise quota_exceeded()
    return sum(asset_media._video_reference_declared_bytes(row) for row in rows)


def variant_receipt(
    variant: dict[str, Any] | None,
    *,
    replacement_key: str,
) -> VolcanoAssetInstallReceipt | None:
    if variant is None:
        return None
    storage_key = str(variant.get("storage_key") or "")
    sha256 = str(variant.get("sha256") or "")
    try:
        size_bytes = max(0, int(variant.get("size_bytes") or 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not storage_key
        or storage_key == replacement_key
        or size_bytes <= 0
        or len(sha256) != 64
    ):
        return None
    return VolcanoAssetInstallReceipt(
        storage_key=storage_key,
        size_bytes=size_bytes,
        sha256=sha256,
    )


__all__ = ["quota_exceeded", "reference_storage_usage", "variant_receipt"]
