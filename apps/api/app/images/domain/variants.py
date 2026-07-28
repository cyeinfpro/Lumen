from __future__ import annotations

from pathlib import PurePosixPath
from types import MappingProxyType


DISPLAY_VARIANT = "display2048"
PREVIEW_VARIANT = "preview1024"
THUMB_VARIANT = "thumb256"
VIDEO_REFERENCE_VARIANT = "video_ref_2048_jpg"
ALLOWED_VARIANTS = frozenset({DISPLAY_VARIANT, PREVIEW_VARIANT, THUMB_VARIANT})
VARIANT_MEDIA_TYPE = MappingProxyType(
    {
        DISPLAY_VARIANT: "image/webp",
        PREVIEW_VARIANT: "image/webp",
        THUMB_VARIANT: "image/jpeg",
    }
)


def deterministic_variant_key(
    *,
    image_id: str,
    source_key: str,
    kind: str,
    extension: str = "webp",
) -> str:
    source = PurePosixPath(source_key)
    return str(source.with_name(f"{image_id}.{kind}.{extension}"))
