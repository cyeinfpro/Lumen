import io

import pytest
from PIL import Image
from lumen_core.svg_output import validate_static_svg
from app import image_artifacts as artifacts

SAFE = b'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="48"><rect width="32" height="48" fill="red"/></svg>'


@pytest.mark.parametrize("xml", [
    '<svg width="10" height="10"><script>alert(1)</script></svg>',
    '<svg width="10" height="10" onload="alert(1)"/>',
    '<svg width="10" height="10"><image href="https://bad.test/x"/></svg>',
    '<svg width="10" height="10"><foreignObject/></svg>',
    '<svg width="10" height="10"><path fill="url(https://bad.test/x)"/></svg>',
    '<svg width="10" height="10"><use href="https://bad.test/x"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]><svg width="10" height="10">&x;</svg>',
    '<?xml-stylesheet href="https://bad.test/x"?><svg width="10" height="10"/>',
    '<svg width="10" height="10"><style>@import "https://bad.test/x"</style></svg>',
])
def test_svg_rejects_active_or_external_content(xml):
    with pytest.raises(ValueError):
        validate_static_svg(xml.encode())


def test_svg_inspection_and_viewbox():
    assert validate_static_svg(SAFE) == (32, 48)
    assert validate_static_svg(b'<svg viewBox="0 0 100 200"/>') == (100, 200)
    info = artifacts._inspect_generated_image_sync(SAFE)
    assert (info.orig_format, info.width, info.height) == ("SVG", 32, 48)


def test_svg_native_renderer():
    try:
        artifacts._VIPS_ADAPTER.load()
    except (ImportError, OSError):
        pytest.skip("Local native libvips is unavailable; validate in existing deployment image")
    bundle = artifacts._make_image_variants_sync(SAFE)
    assert bundle.orig_format == "SVG"
    with Image.open(io.BytesIO(bundle.display.bytes)) as image:
        assert image.size == (32, 48)
        assert image.format == "WEBP"


def test_gif_frames_and_timing_survive_preview():
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue", "green")]
    original = io.BytesIO()
    frames[0].save(original, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[100, 200, 300], loop=2)
    raw = original.getvalue()
    bundle = artifacts._make_image_variants_sync(raw)
    assert bundle.orig_format == "GIF"
    for data in (bundle.display.bytes, bundle.preview.bytes):
        with Image.open(io.BytesIO(data)) as preview:
            assert preview.n_frames == 3
            assert preview.info["loop"] == 2
            durations = []
            for index in range(preview.n_frames):
                preview.seek(index)
                preview.load()
                durations.append(preview.info["duration"])
            assert durations == [100, 200, 300]
    assert original.getvalue() == raw
    for frame in frames:
        frame.close()


def test_gif_safety_frame_limit():
    frames = [Image.new("RGB", (2, 2), (index, 0, 0)) for index in range(65)]
    encoded = io.BytesIO()
    frames[0].save(encoded, format="GIF", save_all=True, append_images=frames[1:],
                   duration=20, optimize=False)
    with pytest.raises(ValueError, match="safety limit"):
        artifacts._inspect_generated_image_sync(encoded.getvalue())
    for frame in frames:
        frame.close()


@pytest.mark.parametrize("fmt,raw,ext,mime", [
    ("SVG", SAFE, "svg", "image/svg+xml"),
    ("GIF", b"GIF89a-original-animation", "gif", "image/gif"),
])
def test_native_original_metadata_preserves_exact_bytes(fmt, raw, ext, mime):
    import hashlib
    from app.tasks.generation_parts.success import _embed_model_metadata, generation_artifact_keys
    from app.tasks.generation_parts.bonus_artifacts import _embed_bonus_metadata, _bonus_format
    sha = hashlib.sha256(raw).hexdigest()
    assert _embed_model_metadata(None, raw, sha, fmt, {"test": True}, None) == (raw, sha)
    assert _embed_bonus_metadata(None, raw, sha, fmt, {"test": True}) == (raw, sha)
    assert _bonus_format(fmt) == (ext, mime)
    keys = generation_artifact_keys(user_id="user", task_id="task", execution_epoch=1,
                                    attempt=1, orig_ext=ext)
    assert keys[0].endswith("/orig." + ext)


def test_missing_svg_renderer_is_clear(monkeypatch):
    def unavailable():
        raise OSError("missing native library")
    monkeypatch.setattr(artifacts._VIPS_ADAPTER, "load", unavailable)
    with pytest.raises(ValueError, match="runtime image lacks native libvips"):
        artifacts.require_svg_renderer()


@pytest.mark.asyncio
async def test_missing_svg_renderer_blocks_before_services(monkeypatch):
    from types import SimpleNamespace
    from app.upstream_parts import direct_requests
    def unavailable():
        raise ValueError("SVG output is unavailable")
    def network_services(*args):
        pytest.fail("services must not be resolved when SVG rendering is unavailable")
    monkeypatch.setattr(artifacts, "require_svg_renderer", unavailable)
    monkeypatch.setattr(direct_requests, "_runtime_services", network_services)
    with pytest.raises(ValueError, match="SVG output is unavailable"):
        await direct_requests._direct_generate_image_once(
            SimpleNamespace(output_format="svg"), base_url_override="https://test.invalid",
            api_key_override="test-unused",
        )


@pytest.mark.parametrize("raw", [SAFE, b"GIF89a-data", b"GIF87a-data"])
@pytest.mark.asyncio
async def test_native_originals_blocked_before_edit_network(raw, monkeypatch):
    from types import SimpleNamespace
    from app.upstream_parts import direct_requests
    def network_services(*args):
        pytest.fail("unsupported input must fail before network services")
    monkeypatch.setattr(direct_requests, "_runtime_services", network_services)
    with pytest.raises(ValueError, match="cannot be editing inputs"):
        await direct_requests._direct_edit_image_once(
            SimpleNamespace(output_format="png", images=[raw]),
            base_url_override="https://test.invalid", api_key_override="test-unused",
        )


@pytest.mark.parametrize("unit", ["in", "cm", "mm", "q", "pt", "pc", "%", "em", "ex", "rem", "vw", "vh", "vmin", "vmax"])
@pytest.mark.parametrize("dimension", ["width", "height"])
def test_svg_rejects_explicit_non_pixel_dimensions(unit, dimension):
    source = f'<svg {dimension}="100000{unit}" viewBox="0 0 32 32"/>'
    with pytest.raises(ValueError, match="numeric/px"):
        validate_static_svg(source.encode())


def test_svg_missing_dimensions_use_viewbox_but_empty_does_not():
    assert validate_static_svg(b'<svg viewBox="0 0 32 48"/>') == (32, 48)
    for source in (b'<svg width="10000" viewBox="0 0 32 32"/>', b'<svg height="16" viewBox="0 0 32 48"/>'):
        with pytest.raises(ValueError, match="both"):
            validate_static_svg(source)
    with pytest.raises(ValueError, match="numeric/px"):
        validate_static_svg(b'<svg width="" viewBox="0 0 32 48"/>')


@pytest.mark.parametrize("size,frames", [("3840x2160", 9), ("2048x2048", 16), ("10016x5008", 2)])
@pytest.mark.asyncio
async def test_gif_budget_rejects_before_network(size, frames, monkeypatch):
    from types import SimpleNamespace
    from app.upstream_parts import direct_requests
    def network_services(*args):
        pytest.fail("Lumen GIF resource budget must reject before network services")
    monkeypatch.setattr(direct_requests, "_runtime_services", network_services)
    with pytest.raises(ValueError, match="Lumen safety limit"):
        await direct_requests._direct_generate_image_once(
            SimpleNamespace(output_format="gif", size=size, frame_count=frames),
            base_url_override="https://test.invalid", api_key_override="test-unused",
        )



def test_svg_renderer_viewport_is_explicit_without_changing_original():
    import xml.etree.ElementTree as ET
    from lumen_core.svg_output import validated_svg_render_bytes
    original = b'<svg viewBox="0 0 32 48"><rect width="32" height="48"/></svg>'
    rendered = ET.fromstring(validated_svg_render_bytes(original))
    assert (rendered.get("width"), rendered.get("height")) == ("32", "48")
    assert b'width="32"' not in original.split(b'>')[0]


@pytest.mark.parametrize("viewbox", [
    "NaN 0 32 32", "0 Inf 32 32", "0 0 NaN 32", "0 0 32 -Inf",
    "0 0 -32 32", "0 0 32 0", "", "0 0 32", "a 0 32 32",
])
@pytest.mark.parametrize("dimensions", ["", 'width="32" height="32"'])
def test_svg_rejects_invalid_viewbox_even_with_explicit_dimensions(viewbox, dimensions):
    raw = f'<svg {dimensions} viewBox="{viewbox}"/>'.encode()
    with pytest.raises(ValueError, match="viewBox"):
        validate_static_svg(raw)
