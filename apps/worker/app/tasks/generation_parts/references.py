"""Reference and inpaint mask loading, normalization, and sizing helpers."""

from __future__ import annotations

import asyncio
import io
import logging
import math
from typing import Protocol

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_ASPECT,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
    GenerationErrorCode as EC,
)
from lumen_core.image_reference import normalized_ref_from_metadata
from lumen_core.models import Image
from lumen_core.sizing import validate_explicit_size

from ...provider_runtime.errors import UpstreamError


logger = logging.getLogger(__name__)
REFERENCE_LOAD_TIMEOUT_S = 30.0
MASK_MAX_BYTES = 50 * 1024 * 1024


class ReferenceBlobStore(Protocol):
    async def aget_bytes(self, key: str) -> bytes: ...


class MaskAlphaPredicate(Protocol):
    def __call__(self, image: PILImage.Image) -> bool: ...


class MaskBinarizer(Protocol):
    def __call__(self, image: PILImage.Image) -> PILImage.Image: ...


async def load_reference_images(
    session: AsyncSession,
    image_ids: list[str],
    *,
    storage: ReferenceBlobStore,
    timeout_seconds: float = REFERENCE_LOAD_TIMEOUT_S,
    log: logging.Logger = logger,
) -> list[tuple[str, bytes]]:
    """Load references in input order and fail instead of degrading to text-only."""
    if not image_ids:
        return []
    rows = (
        await session.execute(
            select(
                Image.id,
                Image.storage_key,
                Image.sha256,
                Image.metadata_jsonb,
            ).where(
                Image.id.in_(image_ids),
                Image.deleted_at.is_(None),
            )
        )
    ).all()
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()
    by_id = {row.id: row for row in rows}
    out: list[tuple[str, bytes]] = []
    for image_id in image_ids:
        if image_id not in by_id:
            raise UpstreamError(
                f"reference image not found id={image_id}",
                error_code=EC.REFERENCE_MISSING.value,
                status_code=404,
            )
        row = by_id[image_id]
        storage_key = row.storage_key
        sha = row.sha256
        normalized = normalized_ref_from_metadata(row.metadata_jsonb)
        read_key = storage_key
        read_sha = sha
        if normalized is not None:
            read_key = normalized["storage_key"]
            maybe_sha = normalized.get("sha256")
            if isinstance(maybe_sha, str) and maybe_sha:
                read_sha = maybe_sha
        try:
            async with asyncio.timeout(timeout_seconds):
                raw = await storage.aget_bytes(read_key)
        except TimeoutError as exc:
            raise UpstreamError(
                f"reference image bytes read timed out key={read_key}",
                error_code=EC.REFERENCE_TIMEOUT.value,
                status_code=None,
            ) from exc
        except FileNotFoundError as exc:
            if read_key != storage_key:
                log.warning(
                    "normalized reference missing; falling back to original "
                    "image_id=%s normalized_key=%s original_key=%s",
                    image_id,
                    read_key,
                    storage_key,
                )
                try:
                    async with asyncio.timeout(timeout_seconds):
                        raw = await storage.aget_bytes(storage_key)
                except TimeoutError as fallback_exc:
                    raise UpstreamError(
                        f"reference image bytes read timed out key={storage_key}",
                        error_code=EC.REFERENCE_TIMEOUT.value,
                        status_code=None,
                    ) from fallback_exc
                except FileNotFoundError as fallback_exc:
                    raise UpstreamError(
                        f"reference image bytes missing key={storage_key}",
                        error_code=EC.REFERENCE_MISSING.value,
                        status_code=404,
                    ) from fallback_exc
                out.append((sha, raw))
                continue
            raise UpstreamError(
                f"reference image bytes missing key={read_key}",
                error_code=EC.REFERENCE_MISSING.value,
                status_code=404,
            ) from exc
        out.append((read_sha, raw))
    return out


async def load_mask_image(
    session: AsyncSession,
    mask_image_id: str,
    *,
    storage: ReferenceBlobStore,
    timeout_seconds: float = REFERENCE_LOAD_TIMEOUT_S,
    max_bytes: int = MASK_MAX_BYTES,
) -> bytes:
    """Load a mask image with the same hard-failure policy as references."""
    row = (
        await session.execute(
            select(Image.id, Image.storage_key).where(
                Image.id == mask_image_id,
                Image.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise UpstreamError(
            f"mask image not found id={mask_image_id}",
            error_code=EC.REFERENCE_MISSING.value,
            status_code=404,
        )
    storage_key = row.storage_key
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()
    try:
        async with asyncio.timeout(timeout_seconds):
            raw = await storage.aget_bytes(storage_key)
    except TimeoutError as exc:
        raise UpstreamError(
            f"mask image bytes read timed out key={storage_key}",
            error_code=EC.REFERENCE_TIMEOUT.value,
            status_code=None,
        ) from exc
    except FileNotFoundError as exc:
        raise UpstreamError(
            f"mask image bytes missing key={storage_key}",
            error_code=EC.REFERENCE_MISSING.value,
            status_code=404,
        ) from exc
    if len(raw) > max_bytes:
        raise UpstreamError(
            "mask image exceeds size limit",
            error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={
                "max_bytes": max_bytes,
                "actual_bytes": len(raw),
            },
        )
    return raw


def mask_alpha_is_binary(image: PILImage.Image) -> bool:
    """Return whether an alpha channel contains only fully clear/opaque values."""
    try:
        bands = image.getbands()
    except Exception:  # noqa: BLE001
        return False
    if "A" not in bands:
        return False
    try:
        alpha = image.getchannel("A")
        extrema = alpha.getextrema()
    except Exception:  # noqa: BLE001
        return False
    if extrema is None:
        return True
    low, high = extrema
    return low in (0, 255) and high in (0, 255)


def binarize_mask_alpha(image: PILImage.Image) -> PILImage.Image:
    """Normalize a mask to RGBA with alpha values in {0, 255}."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    alpha = image.getchannel("A")
    binarized = alpha.point(lambda value: 255 if value >= 128 else 0)
    out = image.copy()
    out.putalpha(binarized)
    return out


def _mask_has_repaint_area(image: PILImage.Image) -> bool:
    try:
        alpha = image.getchannel("A")
        extrema = alpha.getextrema()
    except Exception:  # noqa: BLE001
        return False
    return extrema is not None and extrema[0] < 255


def resize_mask_to_reference(
    mask_bytes: bytes,
    reference_bytes: bytes,
    *,
    alpha_is_binary: MaskAlphaPredicate = mask_alpha_is_binary,
    binarize_alpha: MaskBinarizer = binarize_mask_alpha,
) -> bytes:
    """Align a mask to the first reference and normalize alpha to binary values."""
    try:
        with PILImage.open(io.BytesIO(reference_bytes)) as reference_image:
            reference_size = reference_image.size
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            f"reference image not decodable for mask sizing: {exc}",
            error_code=EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc
    try:
        with PILImage.open(io.BytesIO(mask_bytes)) as mask_image:
            bands = mask_image.getbands()
            has_alpha = "A" in bands or "transparency" in mask_image.info
            if not has_alpha:
                raise UpstreamError(
                    "mask image must include an alpha channel",
                    error_code=EC.BAD_REFERENCE_IMAGE.value,
                    status_code=400,
                )
            same_size = mask_image.size == reference_size
            legitimate_mode = mask_image.mode in ("RGBA", "LA")
            if (
                same_size
                and legitimate_mode
                and alpha_is_binary(mask_image)
                and _mask_has_repaint_area(mask_image)
            ):
                return mask_bytes
            normalized = (
                mask_image if mask_image.mode == "RGBA" else mask_image.convert("RGBA")
            )
            if normalized.size != reference_size:
                normalized = normalized.resize(
                    reference_size,
                    resample=PILImage.Resampling.NEAREST,
                )
            normalized = binarize_alpha(normalized)
            if not _mask_has_repaint_area(normalized):
                raise UpstreamError(
                    "mask image does not mark any repaint area",
                    error_code=EC.BAD_REFERENCE_IMAGE.value,
                    status_code=400,
                )
            out = io.BytesIO()
            normalized.save(out, format="PNG")
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, UpstreamError):
            raise
        raise UpstreamError(
            f"mask image not decodable: {exc}",
            error_code=EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


def reference_pixel_size(reference_bytes: bytes) -> tuple[int, int] | None:
    try:
        with PILImage.open(io.BytesIO(reference_bytes)) as reference_image:
            return reference_image.size
    except Exception:  # noqa: BLE001
        return None


def inpaint_size_from_reference(
    reference_width: int,
    reference_height: int,
    *,
    explicit_align: int = EXPLICIT_ALIGN,
    max_explicit_aspect: float = MAX_EXPLICIT_ASPECT,
    max_explicit_pixels: int = MAX_EXPLICIT_PIXELS,
    max_explicit_side: int = MAX_EXPLICIT_SIDE,
    min_explicit_pixels: int = MIN_EXPLICIT_PIXELS,
) -> str | None:
    """Derive the nearest valid explicit output size from reference dimensions."""
    if reference_width <= 0 or reference_height <= 0:
        return None
    long_side = max(reference_width, reference_height)
    short_side = min(reference_width, reference_height)
    if short_side <= 0:
        return None
    if long_side / short_side > max_explicit_aspect:
        return None

    scale = 1.0
    if long_side > max_explicit_side:
        scale = max_explicit_side / long_side
    pixels_at_scale = reference_width * reference_height * scale * scale
    if pixels_at_scale > max_explicit_pixels:
        scale *= math.sqrt(max_explicit_pixels / pixels_at_scale)

    pixels_at_scale = reference_width * reference_height * scale * scale
    if pixels_at_scale < min_explicit_pixels:
        scale_up = math.sqrt(min_explicit_pixels / pixels_at_scale)
        if (
            max(reference_width, reference_height) * scale * scale_up
            > max_explicit_side
        ):
            return None
        scale *= scale_up

    target_width = reference_width * scale
    target_height = reference_height * scale
    align = explicit_align
    candidates: list[tuple[int, int]] = []
    for align_value in (
        lambda value: max(align, int(round(value / align)) * align),
        lambda value: max(align, int(value // align) * align),
        lambda value: max(align, int(math.ceil(value / align)) * align),
    ):
        candidates.append((align_value(target_width), align_value(target_height)))
    seen: set[tuple[int, int]] = set()
    for width, height in candidates:
        if (width, height) in seen:
            continue
        seen.add((width, height))
        try:
            validate_explicit_size(width, height)
            return f"{width}x{height}"
        except ValueError:
            continue
    return None
