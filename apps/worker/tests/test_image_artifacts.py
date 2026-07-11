from __future__ import annotations

import base64
import binascii
import importlib
import io
from collections.abc import Callable

import pytest
from PIL import Image as PILImage

from app import image_artifacts
from app.tasks import completion, generation


def _image_bytes(
    *,
    mode: str = "RGBA",
    size: tuple[int, int] = (32, 24),
    color: tuple[int, ...] = (40, 120, 200, 255),
    image_format: str = "PNG",
) -> bytes:
    buf = io.BytesIO()
    PILImage.new(mode, size, color).save(buf, format=image_format)
    return buf.getvalue()


def test_decode_upstream_image_b64_accepts_data_uri_and_whitespace() -> None:
    raw = b"generated-image"
    encoded = base64.b64encode(raw).decode("ascii")

    assert image_artifacts._decode_upstream_image_b64(encoded) == raw
    assert (
        image_artifacts._decode_upstream_image_b64(
            f"  data:image/png;base64,\n{encoded[:8]} \n{encoded[8:]}  "
        )
        == raw
    )


def test_decode_upstream_image_b64_rejects_invalid_input() -> None:
    with pytest.raises(binascii.Error):
        image_artifacts._decode_upstream_image_b64("not-valid-base64!")


@pytest.mark.parametrize("image_format", ["PNG", "WEBP", "JPEG"])
def test_validate_generated_image_metadata_accepts_supported_formats(
    image_format: str,
) -> None:
    assert (
        image_artifacts._validate_generated_image_metadata(image_format, 1, 10000)
        == image_format
    )


@pytest.mark.parametrize(
    ("image_format", "width", "height", "message"),
    [
        ("GIF", 1, 1, "unexpected image format"),
        (None, 1, 1, "unexpected image format"),
        ("PNG", 0, 1, "dimensions out of range"),
        ("PNG", 1, 0, "dimensions out of range"),
        ("PNG", 10001, 1, "dimensions out of range"),
        ("PNG", 1, 10001, "dimensions out of range"),
    ],
)
def test_validate_generated_image_metadata_rejects_invalid_metadata(
    image_format: str | None,
    width: int,
    height: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        image_artifacts._validate_generated_image_metadata(
            image_format,
            width,
            height,
        )


@pytest.mark.parametrize(
    "variant_factory",
    [image_artifacts._make_display, image_artifacts._make_preview],
)
def test_webp_variants_preserve_alpha(
    variant_factory: Callable[[PILImage.Image], tuple[bytes, tuple[int, int]]],
) -> None:
    src = PILImage.new("RGBA", (32, 32), (255, 0, 0, 0))
    src.putpixel((16, 16), (20, 40, 60, 255))

    data, size = variant_factory(src)

    assert size == (32, 32)
    with PILImage.open(io.BytesIO(data)) as reloaded:
        reloaded.load()
        assert reloaded.format == "WEBP"
        assert reloaded.mode == "RGBA"
        assert reloaded.getchannel("A").getextrema() == (0, 255)


def test_jpeg_thumb_flattens_transparency_onto_white() -> None:
    src = PILImage.new("RGBA", (32, 32), (255, 0, 0, 0))
    for x in range(8, 24):
        for y in range(8, 24):
            src.putpixel((x, y), (0, 0, 0, 255))

    data, size = image_artifacts._make_thumb(src)

    assert size == (32, 32)
    with PILImage.open(io.BytesIO(data)) as reloaded:
        reloaded.load()
        assert reloaded.format == "JPEG"
        assert reloaded.mode == "RGB"
        corner = reloaded.getpixel((0, 0))
        center = reloaded.getpixel((16, 16))
        assert isinstance(corner, tuple)
        assert isinstance(center, tuple)
        assert min(corner) >= 245
        assert max(center) <= 10


def test_pil_variants_resize_to_limits_without_upscaling() -> None:
    large = image_artifacts._make_variants_with_pil_sync(
        _image_bytes(size=(3000, 1500))
    )
    small = image_artifacts._make_variants_with_pil_sync(_image_bytes(size=(120, 80)))

    assert (large.width, large.height) == (3000, 1500)
    assert large.display.size == (2048, 1024)
    assert large.preview.size == (1024, 512)
    assert large.thumb.size == (256, 128)
    assert (small.width, small.height) == (120, 80)
    assert small.display.size == (120, 80)
    assert small.preview.size == (120, 80)
    assert small.thumb.size == (120, 80)


def test_make_image_variants_falls_back_when_libvips_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import_module = importlib.import_module

    def import_without_pyvips(name: str):
        if name == "pyvips":
            raise ModuleNotFoundError(name)
        return real_import_module(name)

    monkeypatch.setattr(
        image_artifacts.importlib,
        "import_module",
        import_without_pyvips,
    )

    result = image_artifacts._make_image_variants_sync(_image_bytes())

    assert result.engine == "pil"
    assert result.display.size == (32, 24)


def test_generation_facade_preserves_type_and_leaf_function_identity() -> None:
    identity_names = (
        "_ALLOWED_UPSTREAM_IMAGE_FORMATS",
        "_GeneratedImageInspection",
        "_ImageVariantBundle",
        "_MAX_UPSTREAM_IMAGE_SIDE",
        "_PostprocessedGeneratedImage",
        "_VariantPayload",
        "_compute_blurhash",
        "_decode_upstream_image_b64",
        "_image_has_alpha",
        "_image_has_transparency",
        "_inspect_generated_image_sync",
        "_make_display",
        "_make_preview",
        "_make_thumb",
        "_make_variants_with_pil_sync",
        "_make_variants_with_vips_sync",
        "_resize_vips_image",
        "_rgb_image_for_flat_variant",
        "_sha256",
        "_validate_generated_image_metadata",
        "_webp_image_for_variant",
    )

    for name in identity_names:
        assert getattr(generation, name) is getattr(image_artifacts, name)

    assert completion._generation_compute_blurhash is image_artifacts._compute_blurhash
    assert completion._make_display is image_artifacts._make_display
    assert completion._make_preview is image_artifacts._make_preview
    assert completion._make_thumb is image_artifacts._make_thumb
    assert completion._sha256 is image_artifacts._sha256


def test_generation_variant_facades_resolve_extracted_functions_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = image_artifacts._VariantPayload(b"variant", (1, 1))
    standard = image_artifacts._ImageVariantBundle(
        orig_format="PNG",
        width=1,
        height=1,
        display=payload,
        preview=payload,
        thumb=payload,
        engine="standard",
    )
    pil_only = image_artifacts._ImageVariantBundle(
        orig_format="PNG",
        width=1,
        height=1,
        display=payload,
        preview=payload,
        thumb=payload,
        engine="pil-only",
    )
    calls: list[tuple[str, bytes]] = []

    def fake_standard(raw_image: bytes) -> image_artifacts._ImageVariantBundle:
        calls.append(("standard", raw_image))
        return standard

    def fake_pil_only(raw_image: bytes) -> image_artifacts._ImageVariantBundle:
        calls.append(("pil-only", raw_image))
        return pil_only

    monkeypatch.setattr(
        image_artifacts,
        "_make_image_variants_sync",
        fake_standard,
    )
    monkeypatch.setattr(
        image_artifacts,
        "_make_image_variants_pil_only_sync",
        fake_pil_only,
    )

    assert generation._make_image_variants_sync(b"a") is standard
    assert generation._make_image_variants_pil_only_sync(b"b") is pil_only
    assert calls == [("standard", b"a"), ("pil-only", b"b")]
