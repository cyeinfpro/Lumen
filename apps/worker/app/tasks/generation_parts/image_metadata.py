from __future__ import annotations

import io
from typing import Any

from PIL import Image as PILImage

from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    save_image_with_model_metadata,
)


def clean_model_style_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        output.append(tag[:32])
        if len(output) >= 12:
            break
    return output


def model_image_metadata_from_request(
    *,
    image_id: str,
    mime: str,
    request: dict[str, Any] | None,
    prompt: str | None = None,
) -> dict[str, Any]:
    request_value = request if isinstance(request, dict) else {}
    if request_value.get("workflow_action") != "model_library_generate":
        return {}
    age_segment = request_value.get("workflow_model_library_age_segment")
    gender = request_value.get("workflow_model_library_gender")
    appearance_direction = request_value.get(
        "workflow_model_library_appearance_direction"
    )
    style_tags = clean_model_style_tags(
        request_value.get("workflow_model_library_style_tags") or []
    )
    payload = build_model_image_metadata(
        age_segment=age_segment if isinstance(age_segment, str) else None,
        gender=gender if isinstance(gender, str) else None,
        appearance_direction=(
            appearance_direction if isinstance(appearance_direction, str) else None
        ),
        style_tags=style_tags,
        source="model_library_generate",
        prompt_hint=prompt,
    )
    if not payload:
        return {}
    extension = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        extension = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return {
        "model_library": payload,
        "suggested_filename": model_image_filename(
            image_id=image_id,
            ext=extension,
            age_segment=payload.get("age_segment"),
            gender=payload.get("gender"),
            appearance_direction=payload.get("appearance_direction"),
            style_tags=style_tags,
        ),
    }


def maybe_embed_model_image_metadata_bytes(
    *,
    image: PILImage.Image,
    fmt: str,
    raw_image: bytes,
    metadata: dict[str, Any],
) -> bytes:
    payload = metadata.get("model_library") if isinstance(metadata, dict) else None
    if fmt.upper() != "PNG" or not isinstance(payload, dict) or not payload:
        return raw_image
    output = io.BytesIO()
    save_image_with_model_metadata(
        image,
        output,
        fmt="PNG",
        metadata=payload,
    )
    return output.getvalue()
