from __future__ import annotations

import io

import pytest
from PIL import Image as PILImage

from lumen_core.image_reference import (
    MaskPreflightError,
    analyze_mask_image,
    make_reference_variant,
    normalized_ref_from_metadata,
    validate_mask_preflight,
)


def _image_bytes(
    mode: str,
    size: tuple[int, int],
    color,
    *,
    fmt: str = "PNG",
) -> bytes:
    buf = io.BytesIO()
    PILImage.new(mode, size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def test_make_reference_variant_downsizes_to_webp() -> None:
    src = _image_bytes("RGB", (3000, 1500), (12, 34, 56))

    variant = make_reference_variant(src, max_side=2048)

    assert variant.mime == "image/webp"
    assert variant.width == 2048
    assert variant.height == 1024
    assert variant.bytes == len(variant.data)
    assert len(variant.sha256) == 64
    with PILImage.open(io.BytesIO(variant.data)) as im:
        assert im.format == "WEBP"
        assert im.size == (2048, 1024)


def test_alpha_mask_preflight_reports_coverage_and_passes() -> None:
    mask = _image_bytes("RGBA", (10, 10), (255, 255, 255, 255))
    with PILImage.open(io.BytesIO(mask)) as im:
        im.putpixel((0, 0), (255, 255, 255, 0))
        out = io.BytesIO()
        im.save(out, format="PNG")
        mask = out.getvalue()

    preflight = analyze_mask_image(mask, reference_size=(10, 10))

    assert preflight.has_alpha is True
    assert preflight.repaint_ratio == 0.01
    assert preflight.size_matches_reference is True
    assert preflight.to_metadata()["usable_as_mask"] is True
    validate_mask_preflight(preflight)


def test_no_alpha_mask_preflight_has_clear_error() -> None:
    mask = _image_bytes("RGB", (10, 10), (0, 0, 0))

    preflight = analyze_mask_image(mask, reference_size=(10, 10))

    assert preflight.has_alpha is False
    assert preflight.has_luminance is False
    assert preflight.to_metadata()["usable_as_mask"] is False
    with pytest.raises(MaskPreflightError) as excinfo:
        validate_mask_preflight(preflight)
    assert excinfo.value.code == "invalid_mask_alpha"
    assert "no alpha channel" in excinfo.value.message


def test_mask_preflight_rejects_size_mismatch() -> None:
    mask = _image_bytes("RGBA", (8, 8), (255, 255, 255, 0))
    preflight = analyze_mask_image(mask, reference_size=(10, 10))

    with pytest.raises(MaskPreflightError) as excinfo:
        validate_mask_preflight(preflight)

    assert excinfo.value.code == "mask_size_mismatch"
    assert "8x8" in excinfo.value.message
    assert "10x10" in excinfo.value.message


def test_normalized_ref_from_metadata_validates_storage_key() -> None:
    assert normalized_ref_from_metadata({}) is None
    assert normalized_ref_from_metadata({"normalized_ref": {}}) is None
    assert normalized_ref_from_metadata(
        {"normalized_ref": {"storage_key": "u/1/img.ref.webp", "sha256": "abc"}}
    ) == {"storage_key": "u/1/img.ref.webp", "sha256": "abc"}
