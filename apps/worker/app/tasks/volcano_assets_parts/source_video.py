"""Video source preparation primitives for Volcano asset imports."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lumen_core import volcano_asset_media as asset_media
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_MIME,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
    VolcanoAssetVideoMp4,
)
from lumen_core.volcano_asset_media_types import VOLCANO_ASSET_VIDEO_POSTER_MIME

from ...config import settings


@dataclass(frozen=True)
class VideoSourceSnapshot:
    id: str
    user_id: str
    storage_key: str
    sha256: str
    etag: str
    size_bytes: int
    metadata_jsonb: dict[str, Any]
    poster_storage_key: str | None = None


@dataclass(frozen=True)
class PreparedVideoVariant:
    variant: dict[str, Any]
    receipt: VolcanoAssetInstallReceipt | None
    from_snapshot: bool
    poster_receipt: VolcanoAssetInstallReceipt | None = None


@dataclass(frozen=True)
class StagedVideoVariant:
    path: Path
    variant: dict[str, Any]
    poster_bytes: bytes | None


def video_stage_path(snapshot: VideoSourceSnapshot) -> Path:
    source = Path(snapshot.storage_key)
    stage_key = str(
        source.with_name(
            f".{snapshot.id}.{VOLCANO_ASSET_VIDEO_KIND}.{secrets.token_hex(8)}.stage"
        )
    )
    return asset_media._storage_path(settings.storage_root, stage_key)


def video_variant_storage_key(
    snapshot: VideoSourceSnapshot,
    *,
    sha256: str,
) -> str:
    source = Path(snapshot.storage_key)
    return str(
        source.with_name(
            f"{snapshot.id}.{VOLCANO_ASSET_VIDEO_KIND}.{sha256}."
            f"{secrets.token_hex(8)}.mp4"
        )
    )


def video_poster_storage_key(
    snapshot: VideoSourceSnapshot,
    *,
    video_sha256: str,
) -> str:
    source = Path(snapshot.storage_key)
    return str(
        source.with_name(
            f"{snapshot.id}.{VOLCANO_ASSET_VIDEO_KIND}.{video_sha256}."
            f"{secrets.token_hex(8)}.poster.jpg"
        )
    )


def video_variant(
    rendered: VolcanoAssetVideoMp4,
    *,
    storage_key: str,
    poster_storage_key: str | None,
) -> dict[str, Any]:
    variant = {
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
    if poster_storage_key and rendered.poster_bytes:
        variant.update(
            {
                "poster_storage_key": poster_storage_key,
                "poster_mime": VOLCANO_ASSET_VIDEO_POSTER_MIME,
                "poster_size_bytes": len(rendered.poster_bytes),
                "poster_sha256": hashlib.sha256(rendered.poster_bytes).hexdigest(),
            }
        )
    return variant


async def video_poster_metadata(
    storage_key: str | None,
) -> dict[str, Any] | None:
    if not storage_key:
        return None
    try:
        path = asset_media._storage_path(settings.storage_root, storage_key)

        def inspect() -> dict[str, Any] | None:
            if not path.is_file():
                return None
            size_bytes = path.stat().st_size
            if size_bytes <= 0:
                return None
            return {
                "poster_storage_key": storage_key,
                "poster_mime": VOLCANO_ASSET_VIDEO_POSTER_MIME,
                "poster_size_bytes": size_bytes,
                "poster_sha256": asset_media._file_sha256(path),
            }

        return await asyncio.to_thread(inspect)
    except (OSError, VolcanoAssetMediaError):
        return None


__all__ = [
    "PreparedVideoVariant",
    "StagedVideoVariant",
    "VideoSourceSnapshot",
    "video_poster_metadata",
    "video_poster_storage_key",
    "video_stage_path",
    "video_variant",
    "video_variant_storage_key",
]
