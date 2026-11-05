"""Validation for Images-only GPT Image 2.6 controls, without upstream calls."""
from __future__ import annotations

import io
from dataclasses import replace

from PIL import Image, ImageOps

from .image_execution import ImageExecutionRequest

IMAGE_26_MODELS = frozenset({"gpt-image-2.6-flare", "gpt-image-2.6-sunburst"})
MARKUP_MAX_PIXELS = 16_777_216
MARKUP_MAX_BYTES = 50 * 1024 * 1024


def requires_images_controls(request: ImageExecutionRequest) -> bool:
    return (
        request.complexity is not None
        or request.markup is not None
        or request.output_format in {"svg", "gif"}
        or request.frame_count is not None
        or request.frame_duration_ms is not None
    )


def validate_image_controls(request: ImageExecutionRequest) -> ImageExecutionRequest:
    """Fail before dispatch, preserving original input order and separate markup."""
    if not requires_images_controls(request):
        return request
    if request.image_model not in IMAGE_26_MODELS:
        raise ValueError("complexity, markup, SVG and GIF require GPT Image 2.6")
    if request.complexity is not None and request.complexity not in {"low", "medium", "high"}:
        raise ValueError("complexity must be low, medium or high")
    if request.output_format in {"svg", "gif"} and (
        request.mask is not None or request.markup is not None
    ):
        raise ValueError("SVG/GIF output currently cannot be combined with mask or markup")
    if request.markup is None:
        return request
    return _validate_markup_edit(request)


def _validate_markup_edit(request: ImageExecutionRequest) -> ImageExecutionRequest:
    """Validate the independent overlay and normalize only the first source."""
    if request.action != "edit" or not request.images or not request.images[0]:
        raise ValueError("markup requires an edit and a first source image")
    if request.mask is not None:
        raise ValueError("markup and mask cannot be combined")
    if len(request.markup) > MARKUP_MAX_BYTES:
        raise ValueError("markup PNG must not exceed 50 MB")
    try:
        with Image.open(io.BytesIO(request.markup)) as overlay:
            if overlay.format != "PNG":
                raise ValueError("markup must be a PNG")
            if overlay.width * overlay.height > MARKUP_MAX_PIXELS:
                raise ValueError("markup must not exceed 16,777,216 pixels")
            if "A" not in overlay.getbands() and "transparency" not in overlay.info:
                raise ValueError("markup must be a separate transparent PNG layer")
            overlay.load()
            overlay_size = overlay.size
        with Image.open(io.BytesIO(request.images[0])) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) > 1:
                raise ValueError("markup source must be a static PNG, JPEG or WebP image")
            if source.width * source.height > MARKUP_MAX_PIXELS:
                raise ValueError("first source image must not exceed 16,777,216 pixels")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented.size != overlay_size:
                raise ValueError("markup dimensions must match the orientation-corrected first image")
            mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
            oriented = oriented.convert(mode)
            # Match ordinary reference normalization: do not forward EXIF/GPS or text metadata.
            oriented.info.clear()
            with io.BytesIO() as buffer:
                oriented.save(buffer, format="PNG")
                normalized_source = buffer.getvalue()
            if len(normalized_source) > MARKUP_MAX_BYTES:
                raise ValueError("orientation-corrected first image must not exceed 50 MB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("invalid markup or source image") from exc
    return replace(request, images=[normalized_source, *request.images[1:]])
