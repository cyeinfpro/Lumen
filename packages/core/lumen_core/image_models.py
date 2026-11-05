"""Active Image API models and read-only compatibility for admitted legacy tasks."""

from typing import Literal, get_args

ImageModel = Literal["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"]
LegacyImageModel = Literal["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"]
ImageRenderQuality = Literal["auto", "low", "medium", "high", "xhigh", "max"]
DEFAULT_IMAGE_MODEL: ImageModel = "gpt-image-2.6-flare"
LEGACY_DEFAULT_IMAGE_MODEL: LegacyImageModel = "gpt-image-2"
MAX_IMAGE_COUNT = 10
IMAGE_MODELS = frozenset(get_args(ImageModel))
LEGACY_IMAGE_MODELS = frozenset(get_args(LegacyImageModel))
IMAGE_RENDER_QUALITIES = frozenset(get_args(ImageRenderQuality))


def upgrade_legacy_image_model(model: str | None) -> str:
    """Select an active model for a new request, without rewriting saved history."""
    replacements = {
        "gpt-image-2": DEFAULT_IMAGE_MODEL,
        "gpt-image-2.5-flare": "gpt-image-2.6-flare",
        "gpt-image-2.5-sunburst": "gpt-image-2.6-sunburst",
    }
    return replacements.get(model, model) if model else DEFAULT_IMAGE_MODEL


def validate_image_model_quality(
    model: str, quality: str, *, allow_legacy: bool = False
) -> None:
    supported = IMAGE_MODELS | LEGACY_IMAGE_MODELS if allow_legacy else IMAGE_MODELS
    if model not in supported:
        raise ValueError(f"unsupported image model: {model}")
    if quality not in IMAGE_RENDER_QUALITIES:
        raise ValueError(f"unsupported image quality: {quality}")
    if model == "gpt-image-2" and quality in {"xhigh", "max"}:
        raise ValueError(f"{model} does not support {quality} quality")
