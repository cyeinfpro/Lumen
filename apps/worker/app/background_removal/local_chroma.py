from __future__ import annotations

import statistics

from PIL import Image as PILImage
from PIL import ImageChops
from PIL import ImageDraw
from PIL import ImageFilter

from .types import BackgroundRemovalResult


def recover_solid_background_transparency(orig: PILImage.Image) -> PILImage.Image | None:
    with orig.convert("RGB") as rgb:
        sample = rgb.copy()
        sample.thumbnail((512, 512))
        width, height = sample.size
        if width < 2 or height < 2:
            sample.close()
            return None

        pixels = sample.load()
        edge_pixels: list[tuple[int, int, int]] = []
        for x in range(width):
            edge_pixels.append(pixels[x, 0])
            edge_pixels.append(pixels[x, height - 1])
        for y in range(1, height - 1):
            edge_pixels.append(pixels[0, y])
            edge_pixels.append(pixels[width - 1, y])
        sample.close()

        bg = tuple(
            int(statistics.median(channel))
            for channel in zip(*edge_pixels, strict=True)
        )
        edge_stdev = max(
            statistics.pstdev(channel) for channel in zip(*edge_pixels, strict=True)
        )
        if edge_stdev > 18:
            return None

        def close_to_bg(pixel: tuple[int, int, int], tolerance: int) -> bool:
            return max(abs(pixel[i] - bg[i]) for i in range(3)) <= tolerance

        edge_close_ratio = sum(
            1 for pixel in edge_pixels if close_to_bg(pixel, 24)
        ) / len(edge_pixels)
        if edge_close_ratio < 0.86:
            return None

        bg_image = PILImage.new("RGB", rgb.size, bg)
        diff = ImageChops.difference(rgb, bg_image)
        bg_image.close()
        channels = diff.split()
        max_diff = ImageChops.lighter(
            ImageChops.lighter(channels[0], channels[1]), channels[2]
        )
        diff.close()
        for channel in channels:
            channel.close()

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

        fill_value = 128
        full_width, full_height = bg_like.size
        for x in range(full_width):
            if bg_like.getpixel((x, 0)) == 255:
                ImageDraw.floodfill(bg_like, (x, 0), fill_value)
            if bg_like.getpixel((x, full_height - 1)) == 255:
                ImageDraw.floodfill(bg_like, (x, full_height - 1), fill_value)
        for y in range(full_height):
            if bg_like.getpixel((0, y)) == 255:
                ImageDraw.floodfill(bg_like, (0, y), fill_value)
            if bg_like.getpixel((full_width - 1, y)) == 255:
                ImageDraw.floodfill(bg_like, (full_width - 1, y), fill_value)

        connected_bg = bg_like.point(lambda value: 255 if value == fill_value else 0)
        bg_like.close()
        if connected_bg.getbbox() is None:
            connected_bg.close()
            soft_alpha.close()
            return None

        alpha = PILImage.new("L", rgb.size, 255)
        matte_mask = connected_bg.filter(ImageFilter.MaxFilter(5))
        connected_bg.close()
        alpha.paste(soft_alpha, mask=matte_mask)
        matte_mask.close()
        soft_alpha.close()
        if alpha.getextrema()[0] >= 255:
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
        rgba = recover_solid_background_transparency(image)
        if rgba is None:
            return None
        alpha_mask = rgba.getchannel("A")
        return BackgroundRemovalResult(
            rgba=rgba,
            alpha_mask=alpha_mask,
            provider=self.name,
        )
