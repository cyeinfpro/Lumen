from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage

from lumen_core.image_reference import MaskPreflight


def analyze_mask_file(
    path: Path,
    *,
    reference_size: tuple[int, int] | None,
) -> MaskPreflight:
    with PILImage.open(path) as image:
        mime = (image.get_format_mimetype() or "").lower() or None
        width, height = image.size
        mode = image.mode
        bands = image.getbands()
        has_alpha = "A" in bands or "transparency" in image.info
        alpha_min: int | None = None
        alpha_max: int | None = None
        repaint_ratio: float | None = None
        alpha_is_binary: bool | None = None
        is_empty: bool | None = None
        is_full: bool | None = None
        if has_alpha:
            alpha_source = image.convert("RGBA") if "A" not in bands else image
            try:
                alpha = alpha_source.getchannel("A")
                extrema = alpha.getextrema()
                if extrema is not None:
                    alpha_min, alpha_max = int(extrema[0]), int(extrema[1])
                histogram = alpha.histogram()
                total = max(1, width * height)
                transparent = int(histogram[0])
                opaque = int(histogram[255])
                repaint_ratio = transparent / total
                alpha_is_binary = transparent + opaque == total
                is_empty = transparent == 0
                is_full = transparent == total
            finally:
                if alpha_source is not image:
                    alpha_source.close()

        luminance = image.convert("L")
        try:
            luminance_extrema = luminance.getextrema()
        finally:
            luminance.close()
        luminance_min, luminance_max = (
            (
                int(luminance_extrema[0]),
                int(luminance_extrema[1]),
            )
            if luminance_extrema is not None
            else (None, None)
        )
        reference_width: int | None = None
        reference_height: int | None = None
        size_matches_reference: bool | None = None
        if reference_size is not None:
            reference_width, reference_height = reference_size
            size_matches_reference = (width, height) == reference_size

    return MaskPreflight(
        width=width,
        height=height,
        mime=mime,
        mode=mode,
        has_alpha=has_alpha,
        has_luminance=luminance_min != luminance_max,
        alpha_min=alpha_min,
        alpha_max=alpha_max,
        luminance_min=luminance_min,
        luminance_max=luminance_max,
        repaint_ratio=repaint_ratio,
        alpha_is_binary=alpha_is_binary,
        is_empty=is_empty,
        is_full=is_full,
        reference_width=reference_width,
        reference_height=reference_height,
        size_matches_reference=size_matches_reference,
    )
