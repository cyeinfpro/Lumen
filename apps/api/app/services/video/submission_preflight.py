"""Side-effect-free provider contracts shared by preview and submission."""

from .errors import video_http_error
from .reference_media import (
    HAPPYHORSE_ASPECT_RATIOS,
    OMNI_FLASH_ASPECT_RATIOS,
    validate_provider_reference_media,
)


def validate_provider_submission(
    provider,
    body,
    reference_media,
    *,
    reference_validator=validate_provider_reference_media,
):
    reference_validator(
        provider.kind,
        reference_media,
        model=body.model,
        upstream_model=provider.upstream_model_for(body.model, body.action),
    )
    validate_provider_aspect_ratio(provider.kind, body)


def validate_provider_aspect_ratio(provider_kind, body):
    if (
        provider_kind == "dashscope"
        and body.action in {"t2v", "reference"}
        and body.aspect_ratio != "adaptive"
        and body.aspect_ratio not in HAPPYHORSE_ASPECT_RATIOS
    ):
        raise video_http_error(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for HappyHorse",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(HAPPYHORSE_ASPECT_RATIOS),
        )
    if (
        provider_kind == "omni_flash"
        and body.aspect_ratio not in OMNI_FLASH_ASPECT_RATIOS
    ):
        raise video_http_error(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for Omni Flash",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(OMNI_FLASH_ASPECT_RATIOS),
        )
