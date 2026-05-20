"""Reference-image normalization and mask preflight helpers.

The helpers live in core so API upload and worker reference loading can share
one metadata contract without importing app-specific modules.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any


REFERENCE_VARIANT_MIME = "image/webp"
REFERENCE_VARIANT_EXT = "webp"
DEFAULT_REFERENCE_MAX_SIDE = 2048


class ImageReferenceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


class MaskPreflightError(ImageReferenceError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        preflight: "MaskPreflight",
        status_code: int = 400,
    ) -> None:
        super().__init__(
            code,
            message,
            status_code=status_code,
            payload={"mask_preflight": preflight.to_metadata()},
        )
        self.preflight = preflight


@dataclass(frozen=True)
class ReferenceVariant:
    data: bytes
    width: int
    height: int
    mime: str
    sha256: str
    bytes: int
    max_side: int

    def metadata(self, *, storage_key: str) -> dict[str, Any]:
        return {
            "storage_key": storage_key,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "max_side": self.max_side,
        }


@dataclass(frozen=True)
class MaskPreflight:
    width: int
    height: int
    mime: str | None
    mode: str
    has_alpha: bool
    has_luminance: bool
    alpha_min: int | None
    alpha_max: int | None
    luminance_min: int | None
    luminance_max: int | None
    repaint_ratio: float | None
    alpha_is_binary: bool | None
    is_empty: bool | None
    is_full: bool | None
    reference_width: int | None = None
    reference_height: int | None = None
    size_matches_reference: bool | None = None

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "width": self.width,
            "height": self.height,
            "mime": self.mime,
            "mode": self.mode,
            "has_alpha": self.has_alpha,
            "has_luminance": self.has_luminance,
            "alpha_min": self.alpha_min,
            "alpha_max": self.alpha_max,
            "luminance_min": self.luminance_min,
            "luminance_max": self.luminance_max,
            "repaint_ratio": self.repaint_ratio,
            "alpha_is_binary": self.alpha_is_binary,
            "is_empty": self.is_empty,
            "is_full": self.is_full,
        }
        if self.reference_width is not None and self.reference_height is not None:
            data["reference_width"] = self.reference_width
            data["reference_height"] = self.reference_height
            data["size_matches_reference"] = self.size_matches_reference
        data["usable_as_mask"] = (
            self.mime == "image/png"
            and self.has_alpha
            and self.repaint_ratio is not None
            and self.repaint_ratio > 0
            and self.repaint_ratio < 1
            and self.size_matches_reference is not False
        )
        return data


def _pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image as PILImage
        from PIL import ImageOps, UnidentifiedImageError
    except Exception as exc:  # noqa: BLE001
        raise ImageReferenceError(
            "image_processing_unavailable",
            "image processing dependencies are unavailable",
            status_code=500,
        ) from exc
    return PILImage, ImageOps, UnidentifiedImageError


def _has_transparency(im: Any) -> bool:
    try:
        return "A" in im.getbands() or "transparency" in getattr(im, "info", {})
    except Exception:  # noqa: BLE001
        return False


def make_reference_variant(
    data: bytes,
    *,
    max_side: int = DEFAULT_REFERENCE_MAX_SIDE,
    quality: int = 90,
) -> ReferenceVariant:
    """Build a clean, bounded WebP reference variant for repeated i2i use."""
    PILImage, ImageOps, UnidentifiedImageError = _pillow()
    try:
        with PILImage.open(io.BytesIO(data)) as original:
            im = ImageOps.exif_transpose(original)
            width, height = im.size
            if width <= 0 or height <= 0:
                raise ImageReferenceError("invalid_image", "image has invalid dimensions")
            if max(width, height) > max_side:
                ratio = max_side / max(width, height)
                new_size = (
                    max(1, int(round(width * ratio))),
                    max(1, int(round(height * ratio))),
                )
                im = im.resize(new_size, PILImage.LANCZOS)
            target_mode = "RGBA" if _has_transparency(im) else "RGB"
            if im.mode != target_mode:
                im = im.convert(target_mode)
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=quality, method=4)
    except ImageReferenceError:
        raise
    except PILImage.DecompressionBombError as exc:
        raise ImageReferenceError(
            "too_many_pixels",
            "image exceeds safe pixel limit",
            status_code=413,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageReferenceError("invalid_image", "unreadable image") from exc

    normalized = out.getvalue()
    try:
        with PILImage.open(io.BytesIO(normalized)) as check:
            normalized_width, normalized_height = check.size
    except PILImage.DecompressionBombError as exc:
        raise ImageReferenceError(
            "too_many_pixels",
            "image exceeds safe pixel limit",
            status_code=413,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageReferenceError("invalid_image", "unreadable image") from exc
    digest = hashlib.sha256(normalized).hexdigest()
    return ReferenceVariant(
        data=normalized,
        width=normalized_width,
        height=normalized_height,
        mime=REFERENCE_VARIANT_MIME,
        sha256=digest,
        bytes=len(normalized),
        max_side=max_side,
    )


def analyze_mask_image(
    data: bytes,
    *,
    reference_size: tuple[int, int] | None = None,
) -> MaskPreflight:
    """Inspect mask shape without mutating it.

    Alpha masks use alpha=0 as the repaint area. Images without alpha still get
    luminance stats so callers can return a useful "black/white mask was
    uploaded without alpha" error instead of a generic invalid image message.
    """
    PILImage, _ImageOps, UnidentifiedImageError = _pillow()
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            mime = (im.get_format_mimetype() or "").lower() or None
            width, height = im.size
            mode = im.mode
            bands = im.getbands()
            has_alpha = "A" in bands or "transparency" in getattr(im, "info", {})
            alpha_min: int | None = None
            alpha_max: int | None = None
            repaint_ratio: float | None = None
            alpha_is_binary: bool | None = None
            is_empty: bool | None = None
            is_full: bool | None = None
            if has_alpha:
                alpha_source = im.convert("RGBA") if "A" not in bands else im
                alpha = alpha_source.getchannel("A")
                extrema = alpha.getextrema()
                if extrema is not None:
                    alpha_min, alpha_max = int(extrema[0]), int(extrema[1])
                hist = alpha.histogram()
                total = max(1, width * height)
                transparent = int(hist[0])
                opaque = int(hist[255])
                repaint_ratio = transparent / total
                alpha_is_binary = transparent + opaque == total
                is_empty = transparent == 0
                is_full = transparent == total

            luminance = im.convert("L")
            lum_extrema = luminance.getextrema()
            luminance_min, luminance_max = (
                (int(lum_extrema[0]), int(lum_extrema[1]))
                if lum_extrema is not None
                else (None, None)
            )
            ref_w: int | None = None
            ref_h: int | None = None
            size_matches: bool | None = None
            if reference_size is not None:
                ref_w, ref_h = reference_size
                size_matches = (width, height) == (ref_w, ref_h)
    except PILImage.DecompressionBombError as exc:
        raise ImageReferenceError(
            "too_many_pixels",
            "mask image exceeds safe pixel limit",
            status_code=413,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageReferenceError("invalid_mask_image", "unreadable mask image") from exc

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
        reference_width=ref_w,
        reference_height=ref_h,
        size_matches_reference=size_matches,
    )


def validate_mask_preflight(preflight: MaskPreflight) -> None:
    if preflight.mime != "image/png":
        raise MaskPreflightError(
            "invalid_mask_mime",
            "mask must be a PNG image with an alpha channel",
            preflight=preflight,
        )
    if preflight.size_matches_reference is False:
        raise MaskPreflightError(
            "mask_size_mismatch",
            (
                f"mask size {preflight.width}x{preflight.height} must match "
                f"reference size {preflight.reference_width}x{preflight.reference_height}"
            ),
            preflight=preflight,
        )
    if not preflight.has_alpha:
        raise MaskPreflightError(
            "invalid_mask_alpha",
            (
                "mask must include transparency: alpha=0 marks the repaint area; "
                "this image has no alpha channel"
            ),
            preflight=preflight,
        )
    if preflight.is_empty:
        raise MaskPreflightError(
            "empty_mask",
            "mask does not mark any repaint area; draw over the area to change",
            preflight=preflight,
        )
    if preflight.is_full:
        raise MaskPreflightError(
            "full_mask",
            "mask marks the entire image; reduce the mask area or use image-to-image",
            preflight=preflight,
        )


def normalized_ref_from_metadata(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    normalized = metadata.get("normalized_ref")
    if not isinstance(normalized, dict):
        return None
    storage_key = normalized.get("storage_key")
    if not isinstance(storage_key, str) or not storage_key.strip():
        return None
    return normalized
