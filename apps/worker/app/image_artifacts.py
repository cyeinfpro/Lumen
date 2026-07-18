"""Pure generated-image inspection and variant helpers."""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

_ALLOWED_UPSTREAM_IMAGE_FORMATS = {"PNG", "WEBP", "JPEG"}
_MAX_UPSTREAM_IMAGE_SIDE = 10000
_MAX_UPSTREAM_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_UPSTREAM_IMAGE_B64_CHARS = ((_MAX_UPSTREAM_IMAGE_BYTES + 2) // 3) * 4
_MAX_UPSTREAM_IMAGE_PIXELS = 64_000_000
_MAX_BASE64_HEADER_CHARS = 4096


@dataclass(frozen=True)
class _VariantPayload:
    bytes: bytes
    size: tuple[int, int]


@dataclass(frozen=True)
class _GeneratedImageInspection:
    orig_format: str
    width: int
    height: int
    has_transparency: bool


@dataclass(frozen=True)
class _ImageVariantBundle:
    orig_format: str
    width: int
    height: int
    display: _VariantPayload
    preview: _VariantPayload
    thumb: _VariantPayload
    engine: str


@dataclass(frozen=True)
class _PostprocessedGeneratedImage:
    raw_image: bytes
    sha256: str
    orig_format: str
    width: int
    height: int
    blurhash: str | None
    display: _VariantPayload
    preview: _VariantPayload
    thumb: _VariantPayload
    transparent_alpha_recovered: bool = False
    transparent_qc_payload: dict[str, Any] | None = None
    transparent_provider: str | None = None
    engine: str = "pil"
    executor_mode: str = "inline"


def _sha256(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _decode_upstream_image_b64(value: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError("upstream image base64 must be a string")
    raw = value.strip()
    if len(raw) > _MAX_UPSTREAM_IMAGE_B64_CHARS + _MAX_BASE64_HEADER_CHARS:
        raise ValueError("upstream image base64 input exceeds size limit")
    if raw[:5].lower() == "data:" and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    if len(raw) > _MAX_UPSTREAM_IMAGE_B64_CHARS:
        raise ValueError("upstream image base64 exceeds size limit")
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) > _MAX_UPSTREAM_IMAGE_BYTES:
        raise ValueError("upstream image raw bytes exceed size limit")
    return decoded


def _validate_raw_image_bytes(raw_image: bytes) -> None:
    if len(raw_image) > _MAX_UPSTREAM_IMAGE_BYTES:
        raise ValueError(
            "upstream image raw bytes exceed size limit: "
            f"{len(raw_image)} > {_MAX_UPSTREAM_IMAGE_BYTES}"
        )


def _validate_generated_image_metadata(
    orig_format: str | None,
    width: int,
    height: int,
) -> str:
    if orig_format not in _ALLOWED_UPSTREAM_IMAGE_FORMATS:
        raise ValueError(f"upstream returned unexpected image format: {orig_format}")
    if (
        width < 1
        or height < 1
        or width > _MAX_UPSTREAM_IMAGE_SIDE
        or height > _MAX_UPSTREAM_IMAGE_SIDE
    ):
        raise ValueError(f"upstream image dimensions out of range: {width}x{height}")
    pixels = width * height
    if pixels > _MAX_UPSTREAM_IMAGE_PIXELS:
        raise ValueError(
            "upstream image pixel count out of range: "
            f"{pixels} > {_MAX_UPSTREAM_IMAGE_PIXELS}"
        )
    return orig_format


@contextmanager
def _validated_image(raw_image: bytes) -> Iterator[PILImage.Image]:
    """Open and fully decode an image only after cheap size/header checks."""
    _validate_raw_image_bytes(raw_image)
    pil: PILImage.Image | None = None
    try:
        with warnings.catch_warnings():
            # Enforce our explicit pixel ceiling below rather than Pillow's
            # warning threshold, while still translating hard bomb errors.
            warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
            pil = PILImage.open(io.BytesIO(raw_image))
            _validate_generated_image_metadata(
                pil.format,
                pil.size[0],
                pil.size[1],
            )
            pil.load()
        yield pil
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning) as exc:
        raise ValueError(f"upstream image decompression bomb: {exc}") from exc
    finally:
        if pil is not None:
            pil.close()


def _inspect_generated_image_sync(raw_image: bytes) -> _GeneratedImageInspection:
    with _validated_image(raw_image) as pil:
        return _GeneratedImageInspection(
            orig_format=str(pil.format),
            width=pil.size[0],
            height=pil.size[1],
            has_transparency=_image_has_transparency(pil),
        )


def _compute_blurhash(img: PILImage.Image) -> str | None:
    width, height = img.size
    if width < 4 or height < 4:
        return None
    try:
        _bh = importlib.import_module("blurhash")

        # blurhash 期望 RGB；用 thumbnail 来算快得多
        with img.convert("RGB") as small:
            small.thumbnail((64, 64))
            return _bh.encode(small, x_components=4, y_components=3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("blurhash failed: %s", exc)
        return None


def _make_preview(
    orig: PILImage.Image, max_side: int = 1024
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _webp_image_for_variant(im) as webp:
            webp.save(buf, format="WEBP", quality=82, method=4)
        return buf.getvalue(), im.size


def _image_has_alpha(im: PILImage.Image) -> bool:
    return im.mode in {"LA", "RGBA"} or (im.mode == "P" and "transparency" in im.info)


def _image_has_transparency(im: PILImage.Image) -> bool:
    if not _image_has_alpha(im):
        return False
    with im.convert("RGBA") as rgba:
        alpha = rgba.getchannel("A")
        minimum = alpha.getextrema()[0]
        if isinstance(minimum, tuple):
            minimum = minimum[0]
        return minimum < 255


def _webp_image_for_variant(im: PILImage.Image) -> PILImage.Image:
    return im.convert("RGBA" if _image_has_alpha(im) else "RGB")


def _rgb_image_for_flat_variant(
    im: PILImage.Image,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
) -> PILImage.Image:
    if not _image_has_alpha(im):
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    base = PILImage.new("RGB", rgba.size, background)
    base.paste(rgba, mask=rgba.getchannel("A"))
    rgba.close()
    return base


def _make_display(
    orig: PILImage.Image, max_side: int = 2048
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _webp_image_for_variant(im) as webp:
            webp.save(buf, format="WEBP", quality=86, method=4)
        return buf.getvalue(), im.size


def _make_thumb(
    orig: PILImage.Image, max_side: int = 256
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _rgb_image_for_flat_variant(im) as rgb:
            rgb.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue(), im.size


def _resize_vips_image(image: Any, max_side: int) -> Any:
    longest = max(int(getattr(image, "width", 0)), int(getattr(image, "height", 0)))
    if longest <= 0:
        raise ValueError("libvips image has invalid dimensions")
    scale = min(1.0, max_side / longest)
    if scale >= 1.0:
        return image.copy()
    return image.resize(scale)


def _make_variants_with_vips_sync(
    raw_image: bytes,
    inspection: _GeneratedImageInspection,
) -> _ImageVariantBundle:
    _validate_raw_image_bytes(raw_image)
    pyvips = importlib.import_module("pyvips")

    image = pyvips.Image.new_from_buffer(raw_image, "", access="sequential")
    display = _resize_vips_image(image, 2048)
    preview = _resize_vips_image(image, 1024)
    thumb = _resize_vips_image(image, 256)
    try:
        if bool(thumb.hasalpha()):
            thumb = thumb.flatten(background=[255, 255, 255])
    except Exception:
        # Some libvips builds expose alpha metadata differently. If this path
        # cannot prove flattening, let the caller fall back to PIL.
        raise
    return _ImageVariantBundle(
        orig_format=inspection.orig_format,
        width=inspection.width,
        height=inspection.height,
        display=_VariantPayload(
            bytes=display.write_to_buffer(".webp[Q=86]"),
            size=(int(display.width), int(display.height)),
        ),
        preview=_VariantPayload(
            bytes=preview.write_to_buffer(".webp[Q=82]"),
            size=(int(preview.width), int(preview.height)),
        ),
        thumb=_VariantPayload(
            bytes=thumb.write_to_buffer(".jpg[Q=78]"),
            size=(int(thumb.width), int(thumb.height)),
        ),
        engine="libvips",
    )


def _make_variants_with_pil_sync(
    raw_image: bytes,
    inspection: _GeneratedImageInspection | None = None,
) -> _ImageVariantBundle:
    if inspection is None:
        inspection = _inspect_generated_image_sync(raw_image)
    with _validated_image(raw_image) as pil:
        display_bytes, display_size = _make_display(pil)
        preview_bytes, preview_size = _make_preview(pil)
        thumb_bytes, thumb_size = _make_thumb(pil)
    return _ImageVariantBundle(
        orig_format=inspection.orig_format,
        width=inspection.width,
        height=inspection.height,
        display=_VariantPayload(display_bytes, display_size),
        preview=_VariantPayload(preview_bytes, preview_size),
        thumb=_VariantPayload(thumb_bytes, thumb_size),
        engine="pil",
    )


def _make_image_variants_sync(raw_image: bytes) -> _ImageVariantBundle:
    inspection = _inspect_generated_image_sync(raw_image)
    try:
        return _make_variants_with_vips_sync(raw_image, inspection)
    except Exception as exc:  # noqa: BLE001
        logger.debug("libvips image postprocess unavailable, falling back: %s", exc)
    return _make_variants_with_pil_sync(raw_image, inspection)


def _make_image_variants_pil_only_sync(raw_image: bytes) -> _ImageVariantBundle:
    return _make_variants_with_pil_sync(raw_image)
