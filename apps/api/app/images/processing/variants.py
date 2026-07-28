from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from ..ports.image_processing import (
    ImageVariantProcessingRequest,
    PreparedImageVariant,
)
from .service import ProcessingError


def _too_many_pixels(max_pixels: int) -> ProcessingError:
    return ProcessingError(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({max_pixels} pixels)",
        413,
    )


def _invalid_image() -> ProcessingError:
    return ProcessingError("invalid_image", "unreadable image", 400)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_display_webp(
    image: PILImage.Image,
    output_path: Path,
    *,
    max_side: int,
) -> tuple[int, int]:
    image.thumbnail((max_side, max_side))
    with image.convert("RGB") as rgb:
        with output_path.open("w+b") as handle:
            rgb.save(handle, format="WEBP", quality=86, method=4)
            handle.flush()
            os.fsync(handle.fileno())
    return image.size


def _flatten_alpha(image: PILImage.Image) -> PILImage.Image:
    if image.mode not in {"RGBA", "LA"} and "transparency" not in image.info:
        return image.convert("RGB")
    with image.convert("RGBA") as rgba:
        with PILImage.new("RGBA", rgba.size, (255, 255, 255, 255)) as background:
            background.alpha_composite(rgba)
            return background.convert("RGB")


def _write_video_reference_jpeg(
    image: PILImage.Image,
    output_path: Path,
    *,
    max_side: int,
) -> tuple[int, int]:
    transposed = ImageOps.exif_transpose(image)
    try:
        transposed.thumbnail(
            (max_side, max_side),
            PILImage.Resampling.LANCZOS,
        )
        with _flatten_alpha(transposed) as rgb:
            with output_path.open("w+b") as handle:
                try:
                    rgb.save(
                        handle,
                        format="JPEG",
                        quality=88,
                        optimize=True,
                    )
                except OSError:
                    handle.seek(0)
                    handle.truncate()
                    rgb.save(
                        handle,
                        format="JPEG",
                        quality=88,
                    )
                handle.flush()
                os.fsync(handle.fileno())
            return rgb.size
    finally:
        if transposed is not image:
            transposed.close()


def render_image_variant(
    request: ImageVariantProcessingRequest,
) -> PreparedImageVariant:
    try:
        with PILImage.open(request.source_path) as image:
            width, height = image.size
            if request.variant == "display_webp":
                if width <= 0 or height <= 0 or width * height > request.max_pixels:
                    raise _too_many_pixels(request.max_pixels)
            elif width <= 0 or height <= 0:
                raise ProcessingError(
                    "invalid_image",
                    "invalid image size",
                    400,
                )
            elif width * height > request.max_pixels:
                raise _too_many_pixels(request.max_pixels)

            image.load()
            if request.variant == "display_webp":
                rendered_width, rendered_height = _write_display_webp(
                    image,
                    request.output_path,
                    max_side=request.max_side,
                )
                mime = "image/webp"
            else:
                rendered_width, rendered_height = _write_video_reference_jpeg(
                    image,
                    request.output_path,
                    max_side=request.max_side,
                )
                mime = "image/jpeg"
    except ProcessingError:
        request.output_path.unlink(missing_ok=True)
        raise
    except (
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
    ) as exc:
        request.output_path.unlink(missing_ok=True)
        raise _too_many_pixels(request.max_pixels) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        request.output_path.unlink(missing_ok=True)
        raise _invalid_image() from exc

    size_bytes = request.output_path.stat().st_size
    return PreparedImageVariant(
        output_path=request.output_path,
        mime=mime,
        width=rendered_width,
        height=rendered_height,
        size_bytes=size_bytes,
        sha256=_sha256_file(request.output_path),
    )
