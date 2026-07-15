"""Reference and inpaint mask loading, normalization, and sizing helpers."""

from __future__ import annotations

import asyncio
import io
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage


@dataclass(frozen=True)
class ReferenceHooks:
    select: Callable[..., Any]
    image_model: Any
    normalized_ref_from_metadata: Callable[[Any], Any]
    storage_get_bytes: Callable[[str], Awaitable[bytes]]
    upstream_error_factory: Callable[..., Exception]
    upstream_error_type: type[Exception]
    reference_missing_code: str
    reference_timeout_code: str
    reference_image_too_large_code: str
    bad_reference_image_code: str
    logger: logging.Logger
    reference_load_timeout_s: float
    mask_max_bytes: int


@dataclass(frozen=True)
class InpaintSizingLimits:
    explicit_align: int
    max_explicit_aspect: float
    max_explicit_pixels: int
    max_explicit_side: int
    min_explicit_pixels: int
    validate_explicit_size: Callable[[int, int], Any]


async def load_reference_images(
    session: Any,
    image_ids: list[str],
    *,
    hooks: ReferenceHooks,
) -> list[tuple[str, bytes]]:
    """Load references in input order and fail instead of degrading to text-only."""
    if not image_ids:
        return []
    image_model = hooks.image_model
    rows = (
        await session.execute(
            hooks.select(
                image_model.id,
                image_model.storage_key,
                image_model.sha256,
                image_model.metadata_jsonb,
            ).where(
                image_model.id.in_(image_ids),
                image_model.deleted_at.is_(None),
            )
        )
    ).all()
    by_id = {row.id: row for row in rows}
    out: list[tuple[str, bytes]] = []
    for image_id in image_ids:
        if image_id not in by_id:
            raise hooks.upstream_error_factory(
                f"reference image not found id={image_id}",
                error_code=hooks.reference_missing_code,
                status_code=404,
            )
        row = by_id[image_id]
        storage_key = row.storage_key
        sha = row.sha256
        normalized = hooks.normalized_ref_from_metadata(row.metadata_jsonb)
        read_key = storage_key
        read_sha = sha
        if normalized is not None:
            read_key = normalized["storage_key"]
            maybe_sha = normalized.get("sha256")
            if isinstance(maybe_sha, str) and maybe_sha:
                read_sha = maybe_sha
        try:
            async with asyncio.timeout(hooks.reference_load_timeout_s):
                raw = await hooks.storage_get_bytes(read_key)
        except TimeoutError as exc:
            raise hooks.upstream_error_factory(
                f"reference image bytes read timed out key={read_key}",
                error_code=hooks.reference_timeout_code,
                status_code=None,
            ) from exc
        except FileNotFoundError as exc:
            if read_key != storage_key:
                hooks.logger.warning(
                    "normalized reference missing; falling back to original "
                    "image_id=%s normalized_key=%s original_key=%s",
                    image_id,
                    read_key,
                    storage_key,
                )
                try:
                    async with asyncio.timeout(hooks.reference_load_timeout_s):
                        raw = await hooks.storage_get_bytes(storage_key)
                except TimeoutError as fallback_exc:
                    raise hooks.upstream_error_factory(
                        f"reference image bytes read timed out key={storage_key}",
                        error_code=hooks.reference_timeout_code,
                        status_code=None,
                    ) from fallback_exc
                except FileNotFoundError as fallback_exc:
                    raise hooks.upstream_error_factory(
                        f"reference image bytes missing key={storage_key}",
                        error_code=hooks.reference_missing_code,
                        status_code=404,
                    ) from fallback_exc
                out.append((sha, raw))
                continue
            raise hooks.upstream_error_factory(
                f"reference image bytes missing key={read_key}",
                error_code=hooks.reference_missing_code,
                status_code=404,
            ) from exc
        out.append((read_sha, raw))
    return out


async def load_mask_image(
    session: Any,
    mask_image_id: str,
    *,
    hooks: ReferenceHooks,
) -> bytes:
    """Load a mask image with the same hard-failure policy as references."""
    image_model = hooks.image_model
    row = (
        await session.execute(
            hooks.select(image_model.id, image_model.storage_key).where(
                image_model.id == mask_image_id,
                image_model.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise hooks.upstream_error_factory(
            f"mask image not found id={mask_image_id}",
            error_code=hooks.reference_missing_code,
            status_code=404,
        )
    storage_key = row.storage_key
    try:
        async with asyncio.timeout(hooks.reference_load_timeout_s):
            raw = await hooks.storage_get_bytes(storage_key)
    except TimeoutError as exc:
        raise hooks.upstream_error_factory(
            f"mask image bytes read timed out key={storage_key}",
            error_code=hooks.reference_timeout_code,
            status_code=None,
        ) from exc
    except FileNotFoundError as exc:
        raise hooks.upstream_error_factory(
            f"mask image bytes missing key={storage_key}",
            error_code=hooks.reference_missing_code,
            status_code=404,
        ) from exc
    if len(raw) > hooks.mask_max_bytes:
        raise hooks.upstream_error_factory(
            "mask image exceeds size limit",
            error_code=hooks.reference_image_too_large_code,
            status_code=413,
            payload={
                "max_bytes": hooks.mask_max_bytes,
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
    upstream_error_factory: Callable[..., Exception],
    upstream_error_type: type[Exception],
    bad_reference_image_code: str,
    alpha_is_binary: Callable[[PILImage.Image], bool] = mask_alpha_is_binary,
    binarize_alpha: Callable[[PILImage.Image], PILImage.Image] = binarize_mask_alpha,
) -> bytes:
    """Align a mask to the first reference and normalize alpha to binary values."""
    try:
        with PILImage.open(io.BytesIO(reference_bytes)) as reference_image:
            reference_size = reference_image.size
    except Exception as exc:  # noqa: BLE001
        raise upstream_error_factory(
            f"reference image not decodable for mask sizing: {exc}",
            error_code=bad_reference_image_code,
            status_code=400,
        ) from exc
    try:
        with PILImage.open(io.BytesIO(mask_bytes)) as mask_image:
            bands = mask_image.getbands()
            has_alpha = "A" in bands or "transparency" in mask_image.info
            if not has_alpha:
                raise upstream_error_factory(
                    "mask image must include an alpha channel",
                    error_code=bad_reference_image_code,
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
                raise upstream_error_factory(
                    "mask image does not mark any repaint area",
                    error_code=bad_reference_image_code,
                    status_code=400,
                )
            out = io.BytesIO()
            normalized.save(out, format="PNG")
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, upstream_error_type):
            raise
        raise upstream_error_factory(
            f"mask image not decodable: {exc}",
            error_code=bad_reference_image_code,
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
    limits: InpaintSizingLimits,
) -> str | None:
    """Derive the nearest valid explicit output size from reference dimensions."""
    if reference_width <= 0 or reference_height <= 0:
        return None
    long_side = max(reference_width, reference_height)
    short_side = min(reference_width, reference_height)
    if short_side <= 0:
        return None
    if long_side / short_side > limits.max_explicit_aspect:
        return None

    scale = 1.0
    if long_side > limits.max_explicit_side:
        scale = limits.max_explicit_side / long_side
    pixels_at_scale = reference_width * reference_height * scale * scale
    if pixels_at_scale > limits.max_explicit_pixels:
        scale *= math.sqrt(limits.max_explicit_pixels / pixels_at_scale)

    pixels_at_scale = reference_width * reference_height * scale * scale
    if pixels_at_scale < limits.min_explicit_pixels:
        scale_up = math.sqrt(limits.min_explicit_pixels / pixels_at_scale)
        if (
            max(reference_width, reference_height) * scale * scale_up
            > limits.max_explicit_side
        ):
            return None
        scale *= scale_up

    target_width = reference_width * scale
    target_height = reference_height * scale
    align = limits.explicit_align
    candidates: list[tuple[int, int]] = []
    for align_value in (
        lambda value: max(align, int(round(value / align)) * align),
        lambda value: max(align, int(value // align) * align),
        lambda value: max(align, int(math.ceil(value / align)) * align),
    ):
        candidates.append(
            (align_value(target_width), align_value(target_height))
        )
    seen: set[tuple[int, int]] = set()
    for width, height in candidates:
        if (width, height) in seen:
            continue
        seen.add((width, height))
        try:
            limits.validate_explicit_size(width, height)
            return f"{width}x{height}"
        except ValueError:
            continue
    return None
