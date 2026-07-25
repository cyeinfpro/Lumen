from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage, ImageOps


def has_transparency(image: PILImage.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def normalize_to_jpeg(
    source_path: Path,
    output_path: Path,
) -> tuple[int, int]:
    with PILImage.open(source_path) as original:
        normalized = ImageOps.exif_transpose(original)
        width, height = normalized.size
        if has_transparency(normalized):
            with normalized.convert("RGBA") as rgba:
                with PILImage.new("RGB", rgba.size, (255, 255, 255)) as flattened:
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    flattened.save(output_path, format="JPEG", quality=95)
        else:
            if normalized.mode == "RGB":
                normalized.save(output_path, format="JPEG", quality=95)
            else:
                with normalized.convert("RGB") as rgb:
                    rgb.save(output_path, format="JPEG", quality=95)
    return width, height
