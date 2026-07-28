from __future__ import annotations

from typing import Any

from PIL import Image as PILImage

from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    parse_model_image_filename,
    read_model_image_metadata,
)


MODEL_LIBRARY_METADATA_PROFILE = "model_library"


def model_metadata_json_from_upload(
    image: PILImage.Image,
    filename: str | None,
) -> dict[str, Any]:
    parsed = read_model_image_metadata(image)
    metadata_source = "embedded"
    if parsed is None and filename:
        parsed = parse_model_image_filename(filename)
        metadata_source = "filename"
    if parsed is None:
        return {}
    payload = build_model_image_metadata(
        age_segment=parsed.age_segment,
        gender=parsed.gender,
        appearance_direction=parsed.appearance_direction,
        style_tags=list(parsed.style_tags or []),
        source=parsed.source or metadata_source,
        prompt_hint=parsed.prompt_hint,
    )
    if not payload:
        return {}
    return {
        "model_library": payload,
        "model_library_metadata_source": metadata_source,
    }
