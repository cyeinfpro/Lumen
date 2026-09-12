"""Supported Image API models and quality capabilities."""

from typing import Literal, get_args

ImageModel = Literal["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"]
ImageRenderQuality = Literal["auto", "low", "medium", "high", "xhigh", "max"]
DEFAULT_IMAGE_MODEL: ImageModel = "gpt-image-2"
MAX_IMAGE_COUNT = 10
IMAGE_MODELS = frozenset(get_args(ImageModel))
IMAGE_RENDER_QUALITIES = frozenset(get_args(ImageRenderQuality))


def validate_image_model_quality(model: str, quality: str) -> None:
    if model not in IMAGE_MODELS:
        raise ValueError(f"unsupported image model: {model}")
    if quality not in IMAGE_RENDER_QUALITIES:
        raise ValueError(f"unsupported image quality: {quality}")
    if model == DEFAULT_IMAGE_MODEL and quality in {"xhigh", "max"}:
        raise ValueError(f"{model} does not support {quality} quality")
