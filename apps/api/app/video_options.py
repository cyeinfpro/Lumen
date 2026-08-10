"""Video option projection helpers."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from lumen_core.video_providers import (
    SEEDANCE_25_REFERENCE_IMAGE_MAX_ASPECT_RATIO,
    SEEDANCE_25_REFERENCE_IMAGE_MAX_BYTES,
    SEEDANCE_25_REFERENCE_IMAGE_MAX_SIDE,
    SEEDANCE_25_REFERENCE_IMAGE_MIN_ASPECT_RATIO,
    SEEDANCE_25_REFERENCE_IMAGE_MIN_SIDE,
    is_seedance_25_identifier,
    seedance_allows_audio_only_reference,
    seedance_reference_media_limits,
    select_video_provider,
    video_reference_media_limits,
)

SEEDANCE_25_REFERENCE_IMAGE_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "image/gif",
    "image/heic",
    "image/heif",
)


def _provider_for_model(
    providers: list[Any],
    model: str,
    actions: Collection[str],
) -> Any | None:
    if "reference" not in actions:
        return None
    return select_video_provider(providers, model=model, action="reference")


def reference_media_limits_for_model(
    providers: list[Any],
    model: str,
    actions: Collection[str],
) -> dict[str, int]:
    provider = _provider_for_model(providers, model, actions)
    if provider is None:
        return {}
    upstream_model = provider.upstream_model_for(model, "reference")
    if provider.kind == "volcano":
        model_limits = seedance_reference_media_limits(model, upstream_model)
        if model_limits is not None:
            return model_limits
    return video_reference_media_limits(provider.kind)


def reference_media_capabilities_for_model(
    providers: list[Any],
    model: str,
    actions: Collection[str],
) -> tuple[dict[str, int], int | None, bool, dict[str, Any] | None]:
    provider = _provider_for_model(providers, model, actions)
    if provider is None:
        return {}, None, False, None
    upstream_model = provider.upstream_model_for(model, "reference")
    limits = reference_media_limits_for_model(providers, model, actions)
    total_limit = sum(limits.values()) if limits else None
    allow_audio_only = (
        provider.kind == "volcano"
        and seedance_allows_audio_only_reference(model, upstream_model)
    )
    image_constraints = None
    if provider.kind == "volcano" and is_seedance_25_identifier(
        model,
        upstream_model,
    ):
        image_constraints = {
            "min_side_px": SEEDANCE_25_REFERENCE_IMAGE_MIN_SIDE,
            "max_side_px": SEEDANCE_25_REFERENCE_IMAGE_MAX_SIDE,
            "min_aspect_ratio": SEEDANCE_25_REFERENCE_IMAGE_MIN_ASPECT_RATIO,
            "max_aspect_ratio": SEEDANCE_25_REFERENCE_IMAGE_MAX_ASPECT_RATIO,
            "max_bytes": SEEDANCE_25_REFERENCE_IMAGE_MAX_BYTES - 1,
            "mime_types": list(SEEDANCE_25_REFERENCE_IMAGE_MIME_TYPES),
        }
    return limits, total_limit, allow_audio_only, image_constraints
