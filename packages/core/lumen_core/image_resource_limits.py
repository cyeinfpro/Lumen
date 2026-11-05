"""Local decoding safety budgets and conservative generation request limits."""
from __future__ import annotations

MAX_GENERATED_IMAGE_SIDE = 10_000
MAX_GENERATED_IMAGE_PIXELS = 64_000_000
MAX_GIF_FRAMES = 64
# Lumen product limit, not a universal provider capability or pixel-budget claim.
MAX_GIF_REQUEST_FRAMES = 16
DEFAULT_GIF_FRAMES = 9


def validate_gif_resource_budget(width: int, height: int, frames: int) -> None:
    """Apply the same local safety budget before charging and while decoding."""
    if not 1 <= frames <= MAX_GIF_FRAMES:
        raise ValueError("GIF exceeds Lumen safety limit: at most 64 frames")
    if not 1 <= width <= MAX_GENERATED_IMAGE_SIDE or not 1 <= height <= MAX_GENERATED_IMAGE_SIDE:
        raise ValueError("GIF exceeds Lumen safety limit: each side must be at most 10000 pixels")
    if width * height * frames > MAX_GENERATED_IMAGE_PIXELS:
        raise ValueError("GIF exceeds Lumen safety limit: total frame pixels must be at most 64000000")


def validate_gif_request_budget(size: str | None, frames: int | None = None) -> None:
    count = frames if frames is not None else DEFAULT_GIF_FRAMES
    if not 2 <= count <= MAX_GIF_REQUEST_FRAMES:
        raise ValueError("GIF frame_count must be between 2 and 16")
    if size in {None, "", "auto"}:
        # The provider chooses unknown dimensions; decoding still checks them.
        return
    parts = size.split("x")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Invalid GIF frame dimensions")
    width, height = map(int, parts)
    if width % 16 or height % 16:
        raise ValueError("GIF frame dimensions must be multiples of 16")
    validate_gif_resource_budget(width, height, count)
