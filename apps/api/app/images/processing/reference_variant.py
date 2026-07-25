from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage, ImageOps

from .normalize import has_transparency


REFERENCE_VARIANT_MIME = "image/webp"


def make_reference_variant_file(
    source_path: Path,
    output_path: Path,
    *,
    max_side: int,
) -> tuple[int, int]:
    with PILImage.open(source_path) as original:
        image = ImageOps.exif_transpose(original)
        width, height = image.size
        if max(width, height) > max_side:
            ratio = max_side / max(width, height)
            resized = image.resize(
                (
                    max(1, int(round(width * ratio))),
                    max(1, int(round(height * ratio))),
                ),
                PILImage.Resampling.LANCZOS,
            )
            if image is not original:
                image.close()
            image = resized
        try:
            target_mode = "RGBA" if has_transparency(image) else "RGB"
            if image.mode == target_mode:
                image.save(output_path, format="WEBP", quality=90, method=4)
            else:
                with image.convert(target_mode) as converted:
                    converted.save(output_path, format="WEBP", quality=90, method=4)
        finally:
            if image is not original:
                image.close()
    with PILImage.open(output_path) as check:
        return check.size
