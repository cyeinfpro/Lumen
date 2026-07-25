from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


_MODE_CHANNELS = MappingProxyType(
    {
        "1": 1,
        "L": 1,
        "P": 4,
        "LA": 2,
        "RGB": 3,
        "RGBA": 4,
        "CMYK": 4,
        "YCbCr": 3,
        "I": 4,
        "F": 4,
    }
)


@dataclass(frozen=True)
class ImageResourceEstimate:
    upload_bytes: int
    decoded_bytes: int
    transform_peak_bytes: int
    output_reserve_bytes: int
    pixels: int
    cpu_weight: int

    @property
    def peak_bytes(self) -> int:
        return self.upload_bytes + self.transform_peak_bytes + self.output_reserve_bytes


def estimate_image_resources(
    *,
    width: int,
    height: int,
    mode: str,
    upload_bytes: int,
    reference_max_side: int = 2048,
) -> ImageResourceEstimate:
    if width <= 0 or height <= 0 or upload_bytes < 0:
        raise ValueError("invalid image resource estimate inputs")
    pixels = width * height
    channels = _MODE_CHANNELS.get(mode, 4)
    decoded_bytes = pixels * channels
    oriented_copy = decoded_bytes
    rgba_copy = pixels * 4 if channels != 4 else decoded_bytes
    flattened_rgb = pixels * 3
    alpha_or_luminance = pixels
    scale = min(1.0, reference_max_side / max(width, height))
    reference_pixels = max(1, int(width * scale)) * max(1, int(height * scale))
    reference_buffer = reference_pixels * 4
    encoder_scratch = max(8 * 1024 * 1024, reference_buffer // 2)
    transform_peak = (
        decoded_bytes
        + oriented_copy
        + rgba_copy
        + flattened_rgb
        + alpha_or_luminance
        + reference_buffer
        + encoder_scratch
    )
    output_reserve = upload_bytes * 2 + max(
        8 * 1024 * 1024,
        reference_pixels * 2,
    )
    cpu_weight = max(1, min(64, (pixels + 999_999) // 1_000_000))
    return ImageResourceEstimate(
        upload_bytes=upload_bytes,
        decoded_bytes=decoded_bytes,
        transform_peak_bytes=transform_peak,
        output_reserve_bytes=output_reserve,
        pixels=pixels,
        cpu_weight=cpu_weight,
    )
