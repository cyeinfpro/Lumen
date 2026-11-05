from __future__ import annotations

import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.upstream_parts import image_controls, image_dispatch
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.tasks.generation_parts.request_options import image_request_options


def png(size=(8, 6), mode="RGBA", color=None):
    with io.BytesIO() as buffer:
        Image.new(mode, size, color=color).save(buffer, format="PNG")
        return buffer.getvalue()



def native_bytes(fmt):
    if fmt == "svg":
        return b'<svg width="16" height="16"><rect width="16" height="16"/></svg>'
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    with io.BytesIO() as buffer:
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=200)
        result = buffer.getvalue()
    for frame in frames:
        frame.close()
    return result


def request(**changes):
    values = dict(
        action="generate", prompt="test", size="1024x1024", images=None,
        mask=None, n=1, quality="medium", output_format="png",
        output_compression=None, background="auto", moderation="auto",
        model=None, image_model="gpt-image-2.6-flare",
        progress_callback=None, provider_override=None, user_id=None,
    )
    values.update(changes)
    return ImageExecutionRequest(**values)


@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_complexity_is_independent_and_preserved(value):
    item = request(complexity=value)
    assert image_controls.validate_image_controls(item) is item
    options = image_request_options(
        {"model": item.image_model, "complexity": value, "render_quality": "medium"},
        size="1024x1024",
    )
    assert options["complexity"] == value
    assert options["render_quality"] == "medium"
    assert "complexity" not in image_request_options(
        {"model": item.image_model}, size="1024x1024"
    )


@pytest.mark.parametrize("value", ["auto", "minimal", "max", ""])
def test_complexity_rejects_reasoning_and_auto(value):
    with pytest.raises(ValueError, match="complexity"):
        image_controls.validate_image_controls(request(complexity=value))


def test_legacy_request_is_untouched_but_controls_require_26():
    item = request(image_model="gpt-image-2")
    assert image_controls.validate_image_controls(item) is item
    with pytest.raises(ValueError, match="2.6"):
        image_controls.validate_image_controls(replace(item, complexity="low"))


def test_markup_preserves_separate_layer_and_reference_order():
    original = png(mode="RGB", color="blue")
    layer = png(color=(255, 0, 0, 0))
    reference = b"second reference is not transformed"
    item = request(action="edit", images=[original, reference], markup=layer)
    result = image_controls.validate_image_controls(item)
    assert result.markup == layer
    assert result.mask is None
    assert result.images[1] == reference
    with Image.open(io.BytesIO(result.images[0])) as first:
        assert first.getpixel((0, 0)) == (0, 0, 255)



def test_markup_source_normalization_drops_private_metadata():
    source = Image.new("RGB", (8, 6), color="blue")
    exif = source.getexif()
    exif[274] = 6
    exif[315] = "private metadata test marker"
    with io.BytesIO() as buffer:
        source.save(buffer, "JPEG", exif=exif)
        raw = buffer.getvalue()
    result = image_controls.validate_image_controls(
        request(action="edit", images=[raw], markup=png((6, 8)))
    )
    with Image.open(io.BytesIO(result.images[0])) as first:
        assert first.size == (6, 8)
        assert not first.getexif()
        assert "exif" not in first.info


def test_markup_source_palette_transparency_survives_metadata_removal():
    source = Image.new("P", (8, 6), color=0)
    with io.BytesIO() as buffer:
        source.save(buffer, "PNG", transparency=0)
        raw = buffer.getvalue()
    result = image_controls.validate_image_controls(
        request(action="edit", images=[raw], markup=png())
    )
    with Image.open(io.BytesIO(result.images[0])) as first:
        assert first.mode == "RGBA"
        assert first.getpixel((0, 0))[3] == 0


def test_normalized_markup_source_has_byte_limit(monkeypatch):
    layer = png((32, 32))
    source = Image.frombytes("RGB", (32, 32), bytes(i % 251 for i in range(32 * 32 * 3)))
    with io.BytesIO() as buffer:
        source.save(buffer, "PNG")
        raw = buffer.getvalue()
    monkeypatch.setattr(image_controls, "MARKUP_MAX_BYTES", len(layer) + 1)
    with pytest.raises(ValueError, match="orientation-corrected first image"):
        image_controls.validate_image_controls(request(action="edit", images=[raw], markup=layer))


def test_markup_uses_exif_oriented_dimensions():
    source = Image.new("RGB", (8, 6), color="blue")
    exif = source.getexif()
    exif[274] = 6
    with io.BytesIO() as buffer:
        source.save(buffer, "JPEG", exif=exif)
        raw = buffer.getvalue()
    result = image_controls.validate_image_controls(
        request(action="edit", images=[raw], markup=png((6, 8)))
    )
    with Image.open(io.BytesIO(result.images[0])) as first:
        assert first.size == (6, 8)
        assert first.getexif().get(274, 1) == 1


@pytest.mark.parametrize("changes,match", [
    ({"action": "generate"}, "requires an edit"),
    ({"images": []}, "first source"),
    ({"mask": b"mask"}, "cannot be combined"),
    ({"markup": png((2, 3))}, "dimensions"),
    ({"markup": png(mode="RGB")}, "transparent"),
    ({"markup": b"invalid"}, "invalid"),
])
def test_markup_invalid_inputs_fail_before_dispatch(changes, match):
    values = dict(action="edit", images=[png()], markup=png())
    values.update(changes)
    with pytest.raises(ValueError, match=match):
        image_controls.validate_image_controls(request(**values))


def test_markup_non_png_rejected():
    with io.BytesIO() as buffer:
        Image.new("RGB", (8, 6)).save(buffer, "JPEG")
        with pytest.raises(ValueError, match="PNG"):
            image_controls.validate_image_controls(request(
                action="edit", images=[png()], markup=buffer.getvalue()
            ))


def test_markup_limits_checked_before_decode(monkeypatch):
    monkeypatch.setattr(image_controls, "MARKUP_MAX_BYTES", 2)
    with pytest.raises(ValueError, match="50 MB"):
        image_controls.validate_image_controls(request(
            action="edit", images=[png()], markup=png()
        ))
    monkeypatch.setattr(image_controls, "MARKUP_MAX_BYTES", 10000)
    monkeypatch.setattr(image_controls, "MARKUP_MAX_PIXELS", 20)
    with pytest.raises(ValueError, match="pixels"):
        image_controls.validate_image_controls(request(
            action="edit", images=[png()], markup=png()
        ))


@pytest.mark.parametrize("output_format", ["svg", "gif"])
def test_vector_animation_preserves_static_edit_references(output_format):
    item = request(action="edit", output_format=output_format, images=[png()])
    assert image_controls.validate_image_controls(item).images == item.images


@pytest.mark.asyncio
async def test_images_only_route_rejects_jobs_only_before_provider_selection(monkeypatch):
    class LocalError(Exception):
        def __init__(self, message, **kwargs):
            super().__init__(message)
    services = SimpleNamespace(
        core=SimpleNamespace(
            resolve_image_channel=AsyncMock(return_value="image_jobs_only"),
            resolve_image_engine=AsyncMock(return_value="responses"),
            IMAGE_CHANNEL_IMAGE_JOBS_ONLY="image_jobs_only",
        ),
        infrastructure=SimpleNamespace(
            UpstreamError=LocalError,
            EC=SimpleNamespace(INVALID_REQUEST_ERROR=SimpleNamespace(value="invalid")),
        ),
    )
    monkeypatch.setattr(image_dispatch, "_request_services", lambda _: services)
    with pytest.raises(LocalError, match="direct Images API"):
        await anext(image_dispatch._dispatch_fresh_image(request(complexity="low")))


@pytest.mark.asyncio
async def test_markup_adapter_keeps_independent_bytes(monkeypatch):
    from app.tasks.generation_parts import composition_ports
    from app.tasks.generation_parts.services import (
        GenerationProviderContext, GenerationProviderRequest, GenerationProviderEditRequest,
    )
    seen = []
    monkeypatch.setattr(composition_ports, "edit_image", lambda item: seen.append(item))
    provider = composition_ports.DefaultGenerationProvider(None, None, None)
    common = GenerationProviderRequest(
        prompt="edit", size="1024x1024", n=1, quality="medium",
        output_format="png", output_compression=None, background="auto",
        moderation="auto", model=None, progress_callback=None,
        provider_override=None, user_id=None,
        context=GenerationProviderContext("trace", 1, "task", 1),
        image_model="gpt-image-2.6-flare", complexity="low",
    )
    provider.edit(GenerationProviderEditRequest(common, [b"source"], None, b"markup"))
    assert seen[0].markup == b"markup"
    assert seen[0].mask is None
    assert seen[0].images == [b"source"]
    assert seen[0].complexity == "low"


@pytest.mark.asyncio
async def test_markup_loads_original_not_resized_reference(monkeypatch):
    from contextlib import asynccontextmanager
    from app.tasks.generation_parts import runner_dispatch_phase
    @asynccontextmanager
    async def session():
        yield object()
    provider = SimpleNamespace(
        load_reference_images=AsyncMock(return_value=[("sha", b"resized"), ("sha2", b"ref2")]),
        load_markup_image=AsyncMock(side_effect=[b"layer", b"original"]),
    )
    services = SimpleNamespace(store=SimpleNamespace(session=session), provider=provider)
    monkeypatch.setattr(runner_dispatch_phase.DispatchGenerationServices, "from_deps", lambda _: services)
    state = SimpleNamespace(
        services=None, input_image_ids=["first", "second"], mask_image_id=None,
        action="edit", generation=SimpleNamespace(upstream_request={"markup_image_id": "layer"}),
    )
    await runner_dispatch_phase.load_references_and_mask(state)
    assert state.markup_bytes == b"layer"
    assert state.ref_for_body == [("sha", b"original"), ("sha2", b"ref2")]
    assert state.mask_bytes is None



@pytest.mark.asyncio
@pytest.mark.parametrize("complexity", [None, "low", "high"])
async def test_direct_generation_sends_complexity_only_when_explicit(monkeypatch, complexity):
    import httpx
    from app.upstream_parts.upstream_impl import build_image_upstream_runtime
    runtime = build_image_upstream_runtime()
    services = runtime.services
    bodies = []
    async def post(**kwargs):
        bodies.append(kwargs["json_body"])
        await kwargs["before_attempt"](1)
        return httpx.Response(200, json={"data": [{"b64_json": "ZmluYWw="}]})
    monkeypatch.setattr(services.lifecycle, "get_images_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(services.core, "post_with_retry", post)
    await services.direct.direct_generate_image_once(
        request(complexity=complexity, upstream_runtime=runtime),
        base_url_override="https://example.invalid/v1", api_key_override="test",
    )
    if complexity is None:
        assert "complexity" not in bodies[0]
    else:
        assert bodies[0]["complexity"] == complexity
    assert "reasoning" not in bodies[0]


@pytest.mark.asyncio
async def test_direct_edit_transmits_markup_as_own_multipart_field(monkeypatch):
    from app.upstream_parts.upstream_impl import build_image_upstream_runtime
    runtime = build_image_upstream_runtime()
    services = runtime.services
    calls = []
    async def multipart(**kwargs):
        calls.append(kwargs)
        await kwargs["on_dispatch_ready"]()
        return 200, {"data": [{"b64_json": "ZmluYWw="}]}
    monkeypatch.setattr(services.transport, "curl_post_multipart", multipart)
    source, overlay, reference = png(), png(color=(255, 0, 0, 40)), png((3, 3))
    await services.direct.direct_edit_image_once(
        request(action="edit", images=[source, reference], markup=overlay,
                complexity="medium", upstream_runtime=runtime),
        base_url_override="https://example.invalid/v1", api_key_override="test",
    )
    assert calls[0]["data"]["complexity"] == "medium"
    assert [name for name, _ in calls[0]["files"]] == ["image[]", "image[]", "markup"]
    assert calls[0]["files"][-1][1] == ("markup.png", overlay, "image/png")
    assert "mask" not in calls[0]["data"]
    assert "prompt_rewriter" not in calls[0]["data"]


@pytest.mark.asyncio
async def test_explicit_controls_use_images_even_with_responses_default(monkeypatch):
    from app.upstream_parts.upstream_impl import build_image_upstream_runtime
    runtime = build_image_upstream_runtime()
    services = runtime.services
    provider = SimpleNamespace(name="direct", image_jobs_enabled=True)
    observed = []
    monkeypatch.setattr(services.core, "resolve_image_channel", AsyncMock(return_value="auto"))
    monkeypatch.setattr(services.core, "resolve_image_engine", AsyncMock(return_value="responses"))
    monkeypatch.setattr(services.dispatch, "image_dispatch_candidates", AsyncMock(return_value=[provider]))
    async def generate(item):
        observed.append(item)
        return [(b"result", None)]
    monkeypatch.setattr(services.direct, "direct_generate_image_with_failover", generate)
    monkeypatch.setattr(services.providers, "is_byok_provider", lambda _: False)
    result = [item async for item in image_dispatch._dispatch_image(
        request(complexity="low", upstream_runtime=runtime, provider_override=provider)
    )]
    assert result == [(b"result", None)]
    assert observed[0].complexity == "low"
    assert services.dispatch.image_dispatch_candidates.await_args.kwargs["engine"] == "image2"



@pytest.mark.asyncio
@pytest.mark.parametrize("fmt,frames,duration", [("svg", None, None), ("gif", None, None), ("gif", 12, 300)])
async def test_vector_animation_direct_body_has_no_raster_stream_options(monkeypatch, fmt, frames, duration):
    import httpx
    import base64
    from app.upstream_parts import direct_requests
    monkeypatch.setattr(direct_requests.image_artifacts, "require_svg_renderer", lambda: None, raising=False)
    from app.upstream_parts.upstream_impl import build_image_upstream_runtime
    runtime = build_image_upstream_runtime()
    services = runtime.services
    bodies = []
    async def post(**kwargs):
        bodies.append(kwargs["json_body"])
        await kwargs["before_attempt"](1)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(native_bytes(fmt)).decode()}]})
    monkeypatch.setattr(services.lifecycle, "get_images_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(services.core, "post_with_retry", post)
    await services.direct.direct_generate_image_once(
        request(output_format=fmt, output_compression=80, size="512x512",
                frame_count=frames, frame_duration_ms=duration, upstream_runtime=runtime),
        base_url_override="https://example.invalid/v1", api_key_override="test",
        streaming_override=True,
    )
    body = bodies[0]
    assert body["output_format"] == fmt
    assert body["n"] == 1
    assert not {"stream", "partial_images", "output_compression", "prompt_rewriter"} & body.keys()
    if fmt == "gif":
        assert body["frame_count"] == (frames or 9)
        assert body["frame_duration_ms"] == (duration or 200)
    else:
        assert "frame_count" not in body
