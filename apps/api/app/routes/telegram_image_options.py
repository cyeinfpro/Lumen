"""Explicit image size calculation for Telegram generation requests."""

from __future__ import annotations

from lumen_core.constants import (
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
)


def align_pair(a: int, b: int) -> str:
    a = max(EXPLICIT_ALIGN, (a // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    b = max(EXPLICIT_ALIGN, (b // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    return f"{a}x{b}"


def aspect_ratio_to_size(ratio: str, max_long_side: int) -> str:
    a, _, b = ratio.partition(":")
    try:
        ratio_a, ratio_b = float(a), float(b)
    except ValueError:
        return align_pair(max_long_side, max_long_side)
    if ratio_a <= 0 or ratio_b <= 0:
        return align_pair(max_long_side, max_long_side)

    long_ratio = max(ratio_a, ratio_b)
    short_ratio = min(ratio_a, ratio_b)
    pixel_cap_long = int(
        (MAX_EXPLICIT_PIXELS * long_ratio / short_ratio) ** 0.5
    )
    long_side = min(max_long_side, MAX_EXPLICIT_SIDE, pixel_cap_long)
    short_side = int(round(long_side * short_ratio / long_ratio))

    long_side = max(EXPLICIT_ALIGN, (long_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    short_side = max(EXPLICIT_ALIGN, (short_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    while (
        long_side * short_side < MIN_EXPLICIT_PIXELS
        and long_side < MAX_EXPLICIT_SIDE
    ):
        long_side += EXPLICIT_ALIGN
        short_side = int(round(long_side * short_ratio / long_ratio))
        short_side = max(
            EXPLICIT_ALIGN, (short_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN
        )

    if ratio_a >= ratio_b:
        return f"{long_side}x{short_side}"
    return f"{short_side}x{long_side}"
