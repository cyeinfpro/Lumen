"""Neutral apparel model-library value helpers."""

from __future__ import annotations

from typing import cast

from lumen_core.model_image_metadata import model_image_filename
from lumen_core.schemas import ModelAgeSegment


MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = "model_library.sync_use_proxy_pool"
MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = "model_library.sync_proxy_name"
MODEL_LIBRARY_ROOT_KEY = "apparel-model-library"


def _model_library_download_filename(
    *,
    image_id: str,
    mime: str | None,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> str:
    ext = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        ext = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return model_image_filename(
        image_id=image_id,
        ext=ext,
        age_segment=cast(ModelAgeSegment | None, age_segment),
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
    )


__all__ = [
    "MODEL_LIBRARY_ROOT_KEY",
    "MODEL_LIBRARY_SYNC_PROXY_NAME_KEY",
    "MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY",
    "_model_library_download_filename",
]


# Public workflow contracts.
model_library_download_filename = _model_library_download_filename
