from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Callable

import pytest
from PIL import Image as PILImage

from app import image_artifacts
from app.tasks.completion_parts import default_runtime as completion
from app.tasks.generation_parts import composition_support as generation


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


def test_decode_upstream_image_b64_rejects_oversized_encoded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_artifacts, "_MAX_UPSTREAM_IMAGE_B64_CHARS", 8)

    with pytest.raises(ValueError, match="base64"):
        image_artifacts._decode_upstream_image_b64("A" * 9)


def test_decode_upstream_image_b64_rejects_oversized_decoded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_artifacts, "_MAX_UPSTREAM_IMAGE_BYTES", 3)

    with pytest.raises(ValueError, match="raw bytes"):
        image_artifacts._decode_upstream_image_b64(
            base64.b64encode(b"1234").decode("ascii")
        )


def test_inspect_rejects_oversized_raw_bytes_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_artifacts, "_MAX_UPSTREAM_IMAGE_BYTES", 3)

    def unexpected_open(_value: object) -> None:
        raise AssertionError("oversized raw bytes must be rejected before Pillow")

    monkeypatch.setattr(image_artifacts.PILImage, "open", unexpected_open)

    with pytest.raises(ValueError, match="raw bytes"):
        image_artifacts._inspect_generated_image_sync(b"1234")


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


def test_validate_generated_image_metadata_rejects_excessive_pixel_count() -> None:
    with pytest.raises(ValueError, match="pixel count"):
        image_artifacts._validate_generated_image_metadata("PNG", 8001, 8001)


def test_inspect_rejects_pixel_limit_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls = 0

    class HeaderOnlyImage:
        format = "PNG"
        size = (8001, 8001)

        def load(self) -> None:
            nonlocal load_calls
            load_calls += 1

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        image_artifacts.PILImage,
        "open",
        lambda _value: HeaderOnlyImage(),
    )

    with pytest.raises(ValueError, match="pixel count"):
        image_artifacts._inspect_generated_image_sync(b"header")

    assert load_calls == 0


def test_inspect_translates_decompression_bomb_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_value: object) -> None:
        raise image_artifacts.PILImage.DecompressionBombError("too many pixels")

    monkeypatch.setattr(image_artifacts.PILImage, "open", boom)

    with pytest.raises(ValueError, match="decompression bomb"):
        image_artifacts._inspect_generated_image_sync(b"header")


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


@pytest.mark.parametrize("max_side", [256, 1024, 2048])
@pytest.mark.parametrize(
    "size",
    [(1, 1), (17, 3), (255, 256), (256, 256), (257, 1000), (3000, 1700), (9999, 13)],
)
def test_scaled_variant_size_matches_pillow_thumbnail(
    size: tuple[int, int], max_side: int
) -> None:
    """缩放改为直接 resize 后，尺寸必须与 Image.thumbnail 逐像素一致。"""
    reference = PILImage.new("RGB", size)
    reference.thumbnail((max_side, max_side))

    with image_artifacts._scaled_for_variant(PILImage.new("RGB", size), max_side) as im:
        assert im.size == reference.size


def test_variant_helpers_do_not_mutate_source_image() -> None:
    """三个变体共享同一张原图，任何一个就地改动都会污染后续变体。"""
    src = PILImage.new("RGBA", (3000, 1700), (10, 20, 30, 255))

    for factory in (
        image_artifacts._make_display,
        image_artifacts._make_preview,
        image_artifacts._make_thumb,
    ):
        factory(src)
        assert src.size == (3000, 1700)
        assert src.mode == "RGBA"


def test_scaled_variant_skips_copy_when_already_within_bounds() -> None:
    """已在目标尺寸内时不应再分配副本（省掉全分辨率拷贝的峰值内存）。"""
    src = PILImage.new("RGB", (64, 64))

    with image_artifacts._scaled_for_variant(src, 256) as im:
        assert im is src


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
    class UnavailableVips:
        def load(self) -> None:
            raise ModuleNotFoundError("pyvips")

    monkeypatch.setattr(image_artifacts, "_VIPS_ADAPTER", UnavailableVips())

    result = image_artifacts._make_image_variants_sync(_image_bytes())

    assert result.engine == "pil"
    assert result.display.size == (32, 24)


def test_generation_support_does_not_mirror_artifact_private_symbols() -> None:
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
        assert not hasattr(generation, name)

    tiny = PILImage.new("RGB", (2, 2))
    assert completion._generation_compute_blurhash(tiny) is None
    assert completion._sha256(b"artifact") == image_artifacts._sha256(b"artifact")


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

    inspection = image_artifacts._GeneratedImageInspection(
        orig_format="PNG",
        width=1,
        height=1,
        has_transparency=False,
    )

    def fake_standard(
        raw_image: bytes,
        received_inspection: image_artifacts._GeneratedImageInspection,
    ) -> image_artifacts._ImageVariantBundle:
        assert received_inspection is inspection
        calls.append(("standard", raw_image))
        return standard

    def fake_pil_only(
        raw_image: bytes,
        received_inspection: image_artifacts._GeneratedImageInspection | None = None,
    ) -> image_artifacts._ImageVariantBundle:
        calls.append(("pil-only", raw_image))
        return pil_only

    monkeypatch.setattr(
        generation.artifacts,
        "inspect_generated_image_sync",
        lambda _raw_image: inspection,
    )
    monkeypatch.setattr(
        generation.artifacts,
        "make_variants_with_vips_sync",
        fake_standard,
    )
    monkeypatch.setattr(
        generation.artifacts,
        "make_variants_with_pil_sync",
        fake_pil_only,
    )

    assert generation.make_image_variants_sync(b"a") is standard
    assert generation.make_image_variants_pil_only_sync(b"b") is pil_only
    assert calls == [("standard", b"a"), ("pil-only", b"b")]
