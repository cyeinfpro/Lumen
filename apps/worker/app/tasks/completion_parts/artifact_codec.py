"""Completion-owned image artifact codec contract.

The completion image tool persists the same variants as generation, but it
must not depend on generation runtime internals for basic image handling.
"""

from __future__ import annotations

import base64
import hashlib
import io

from PIL import Image as PILImage


def decode_upstream_image_b64(value: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError("upstream image base64 must be a string")
    raw = value.strip()
    if raw[:5].lower() == "data:" and "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode("".join(raw.split()), validate=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_blurhash(image: PILImage.Image) -> str | None:
    width, height = image.size
    if width < 4 or height < 4:
        return None
    try:
        from blurhash import encode

        with image.convert("RGB") as small:
            small.thumbnail((64, 64))
            return encode(small, x_components=4, y_components=3)
    except Exception:  # noqa: BLE001
        return None


def _webp_variant(image: PILImage.Image) -> PILImage.Image:
    if image.mode in {"LA", "RGBA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        return image.convert("RGBA")
    return image.convert("RGB")


def _rgb_variant(image: PILImage.Image) -> PILImage.Image:
    if image.mode not in {"LA", "RGBA"} and not (
        image.mode == "P" and "transparency" in image.info
    ):
        return image.convert("RGB")
    with image.convert("RGBA") as rgba:
        background = PILImage.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background


def _webp_bytes(
    image: PILImage.Image,
    *,
    max_side: int,
    quality: int,
) -> tuple[bytes, tuple[int, int]]:
    with image.copy() as resized:
        resized.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        with _webp_variant(resized) as webp:
            webp.save(buffer, format="WEBP", quality=quality, method=4)
        return buffer.getvalue(), resized.size


def make_preview(image: PILImage.Image) -> tuple[bytes, tuple[int, int]]:
    return _webp_bytes(image, max_side=1024, quality=82)


def make_display(image: PILImage.Image) -> tuple[bytes, tuple[int, int]]:
    return _webp_bytes(image, max_side=2048, quality=86)


def make_thumb(image: PILImage.Image) -> tuple[bytes, tuple[int, int]]:
    with image.copy() as resized:
        resized.thumbnail((256, 256))
        buffer = io.BytesIO()
        with _rgb_variant(resized) as rgb:
            rgb.save(buffer, format="JPEG", quality=78, optimize=True)
        return buffer.getvalue(), resized.size
