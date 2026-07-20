from __future__ import annotations

import asyncio
import statistics
from typing import cast

from PIL import Image as PILImage
from PIL import ImageChops
from PIL import ImageDraw
from PIL import ImageFilter

from .types import BackgroundRemovalResult, rgb_pixel_access


def _edge_pixels(rgb: PILImage.Image) -> list[tuple[int, int, int]]:
    sample = rgb.copy()
    try:
        sample.thumbnail((512, 512))
        width, height = sample.size
        if width < 2 or height < 2:
            return []
        pixels = rgb_pixel_access(sample)
        top_and_bottom = [
            pixel
            for x in range(width)
            for pixel in (pixels[x, 0], pixels[x, height - 1])
        ]
        sides = [
            pixel
            for y in range(1, height - 1)
            for pixel in (pixels[0, y], pixels[width - 1, y])
        ]
        return [*top_and_bottom, *sides]
    finally:
        sample.close()


def _background_color(
    edge_pixels: list[tuple[int, int, int]],
) -> tuple[int, int, int] | None:
    if not edge_pixels:
        return None
    channels = tuple(zip(*edge_pixels, strict=True))
    if max(statistics.pstdev(channel) for channel in channels) > 18:
        return None
    background = tuple(int(statistics.median(channel)) for channel in channels)
    close_count = sum(
        1
        for pixel in edge_pixels
        if max(abs(pixel[index] - background[index]) for index in range(3)) <= 24
    )
    if close_count / len(edge_pixels) < 0.86:
        return None
    return background


def _max_color_difference(
    rgb: PILImage.Image,
    background: tuple[int, int, int],
) -> PILImage.Image:
    bg_image = PILImage.new("RGB", rgb.size, background)
    diff = ImageChops.difference(rgb, bg_image)
    bg_image.close()
    channels = diff.split()
    max_diff = ImageChops.lighter(
        ImageChops.lighter(channels[0], channels[1]),
        channels[2],
    )
    diff.close()
    for channel in channels:
        channel.close()
    return max_diff


def _connected_background_mask(bg_like: PILImage.Image) -> PILImage.Image | None:
    fill_value = 128
    width, height = bg_like.size
    for x in range(width):
        if bg_like.getpixel((x, 0)) == 255:
            ImageDraw.floodfill(bg_like, (x, 0), fill_value)
        if bg_like.getpixel((x, height - 1)) == 255:
            ImageDraw.floodfill(bg_like, (x, height - 1), fill_value)
    for y in range(height):
        if bg_like.getpixel((0, y)) == 255:
            ImageDraw.floodfill(bg_like, (0, y), fill_value)
        if bg_like.getpixel((width - 1, y)) == 255:
            ImageDraw.floodfill(bg_like, (width - 1, y), fill_value)
    connected = bg_like.point(lambda value: 255 if value == fill_value else 0)
    if connected.getbbox() is None:
        connected.close()
        return None
    return connected


def recover_solid_background_transparency(
    orig: PILImage.Image,
) -> PILImage.Image | None:
    with orig.convert("RGB") as rgb:
        edge_pixels = _edge_pixels(rgb)
        background = _background_color(edge_pixels)
        if background is None:
            return None

        max_diff = _max_color_difference(rgb, background)
        soft_alpha = max_diff.point(
            lambda value: (
                0
                if value <= 10
                else 255
                if value >= 48
                else int(round((value - 10) * 255 / 38))
            )
        )
        bg_like = max_diff.point(lambda value: 255 if value <= 48 else 0)
        max_diff.close()

        connected_bg = _connected_background_mask(bg_like)
        bg_like.close()
        if connected_bg is None:
            soft_alpha.close()
            return None

        alpha = PILImage.new("L", rgb.size, 255)
        matte_mask = connected_bg.filter(ImageFilter.MaxFilter(5))
        connected_bg.close()
        alpha.paste(soft_alpha, mask=matte_mask)
        matte_mask.close()
        soft_alpha.close()
        alpha_min, _alpha_max = cast(tuple[int, int], alpha.getextrema())
        if alpha_min >= 255:
            alpha.close()
            return None

        rgba = rgb.convert("RGBA")
        rgba.putalpha(alpha)
        alpha.close()
        return rgba


class LocalChromaProvider:
    name = "local_chroma"

    async def remove_background(
        self,
        image: PILImage.Image,
        *,
        prompt: str | None = None,
    ) -> BackgroundRemovalResult | None:
        _ = prompt
        rgba = await asyncio.to_thread(recover_solid_background_transparency, image)
        if rgba is None:
            return None
        alpha_mask = rgba.getchannel("A")
        return BackgroundRemovalResult(
            rgba=rgba,
            alpha_mask=alpha_mask,
            provider=self.name,
        )
