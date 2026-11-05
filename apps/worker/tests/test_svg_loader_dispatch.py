"""Validated prefixed SVG needs explicit libvips dispatch, not magic sniffing."""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app import image_artifacts as artifacts
from lumen_core.svg_output import validated_svg_render_bytes

SVG = b'<s:svg xmlns:s="http://www.w3.org/2000/svg" viewBox="0 0 96 64"><s:rect width="96" height="64" fill="red"/></s:svg>'


def raster_bytes(fmt="PNG"):
    with io.BytesIO() as buffer:
        Image.new("RGB", (96, 64), "red").save(buffer, format=fmt)
        return buffer.getvalue()


class RenderedImage:
    width = 96
    height = 64

    def copy(self):
        return self

    def hasalpha(self):
        return False

    def write_to_buffer(self, encoding):
        if encoding == ".png":
            return raster_bytes()
        return b"variant"


def fake_vips(monkeypatch):
    image = SimpleNamespace(
        svgload_buffer=Mock(return_value=RenderedImage()),
        new_from_buffer=Mock(return_value=RenderedImage()),
    )
    vips = SimpleNamespace(Image=image)
    monkeypatch.setattr(artifacts, "require_svg_renderer", lambda: vips)
    monkeypatch.setattr(artifacts._VIPS_ADAPTER, "load", lambda: vips)
    return image


def test_prefixed_svg_pil_fallback_uses_only_validated_explicit_loader(monkeypatch):
    loader = fake_vips(monkeypatch)
    expected = validated_svg_render_bytes(SVG)
    assert b":svg" in expected
    with artifacts._validated_image(SVG) as image:
        assert image.size == (96, 64)
        assert image.format == "PNG"
    loader.svgload_buffer.assert_called_once_with(expected, access="sequential")
    loader.new_from_buffer.assert_not_called()
    assert b'width="96"' not in SVG.split(b">")[0]


def test_prefixed_svg_variants_use_only_validated_explicit_loader(monkeypatch):
    loader = fake_vips(monkeypatch)
    inspection = artifacts._inspect_generated_image_sync(SVG)
    variants = artifacts._make_variants_with_vips_sync(SVG, inspection)
    loader.svgload_buffer.assert_called_once_with(
        validated_svg_render_bytes(SVG), access="sequential"
    )
    loader.new_from_buffer.assert_not_called()
    assert variants.orig_format == "SVG"
    assert (variants.width, variants.height) == (96, 64)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_raster_variants_keep_existing_auto_loader(monkeypatch, fmt):
    loader = fake_vips(monkeypatch)
    raw = raster_bytes(fmt)
    inspection = artifacts._inspect_generated_image_sync(raw)
    variants = artifacts._make_variants_with_vips_sync(raw, inspection)
    loader.new_from_buffer.assert_called_once_with(raw, "", access="sequential")
    loader.svgload_buffer.assert_not_called()
    assert variants.orig_format == fmt


@pytest.mark.parametrize("path", ["fallback", "variants"])
def test_unsafe_svg_is_rejected_before_explicit_loader(monkeypatch, path):
    loader = fake_vips(monkeypatch)
    raw = b'<svg width="96" height="64"><script>alert(1)</script></svg>'
    with pytest.raises(ValueError, match="Unsupported SVG element"):
        if path == "fallback":
            with artifacts._validated_image(raw):
                pytest.fail("unsafe SVG was accepted")
        else:
            safe_inspection = artifacts._inspect_generated_image_sync(SVG)
            artifacts._make_variants_with_vips_sync(raw, safe_inspection)
    loader.svgload_buffer.assert_not_called()
    loader.new_from_buffer.assert_not_called()
