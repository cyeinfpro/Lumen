from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.upstream_parts import direct_requests, image_controls
from app.upstream_parts.generated_payload import materialize_generated_payload
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


def png():
    with io.BytesIO() as buf:
        Image.new("RGB", (16, 16), "blue").save(buf, format="PNG")
        return buf.getvalue()


def native_bytes(fmt):
    if fmt == "svg":
        return b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>'
    with io.BytesIO() as buf:
        frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=200)
        for frame in frames:
            frame.close()
        return buf.getvalue()


def request(fmt, runtime, **changes):
    values = dict(
        action="edit", prompt="animate or redraw", size="512x512", images=[png(), png()],
        mask=None, n=1, quality="medium", output_format=fmt, output_compression=None,
        background="auto", moderation="auto", model=None,
        image_model="gpt-image-2.6-flare", progress_callback=None,
        provider_override=None, user_id=None, upstream_runtime=runtime,
    )
    values.update(changes)
    return ImageExecutionRequest(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["svg", "gif"])
@pytest.mark.parametrize("model", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
async def test_native_edit_preserves_references_and_real_result(monkeypatch, fmt, model):
    runtime = build_image_upstream_runtime()
    calls = []
    raw = native_bytes(fmt)
    async def multipart(**kwargs):
        calls.append(kwargs)
        return 200, {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
    monkeypatch.setattr(runtime.services.transport, "curl_post_multipart", multipart)
    monkeypatch.setattr(direct_requests.image_artifacts, "require_svg_renderer", lambda: None)
    item = request(fmt, runtime, image_model=model,
                   frame_count=2 if fmt == "gif" else None,
                   frame_duration_ms=300 if fmt == "gif" else None)
    assert image_controls.validate_image_controls(item) is item
    result = await direct_requests._direct_edit_image_once(
        item, base_url_override="https://example.invalid/v1", api_key_override="test"
    )
    assert materialize_generated_payload(result[0][0]) == raw
    call = calls[0]
    assert call["url"].endswith("/images/edits")
    assert [value[1] for _, value in call["files"]] == item.images
    assert [name for name, _ in call["files"]] == ["image[]", "image[]"]
    assert call["data"]["output_format"] == fmt
    assert call["data"]["model"] == model
    assert call["data"]["n"] == "1"
    assert not {"stream", "partial_images", "output_compression", "prompt_rewriter", "preview"} & call["data"].keys()
    if fmt == "gif":
        assert call["data"]["frame_count"] == "2"
        assert call["data"]["frame_duration_ms"] == "300"
    else:
        assert "frame_count" not in call["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["svg", "gif"])
async def test_native_edit_upstream_rejection_is_not_replaced_or_retried(monkeypatch, fmt):
    runtime = build_image_upstream_runtime()
    calls = []
    async def multipart(**kwargs):
        calls.append(kwargs)
        return 400, {"error": {"message": "preview access or format unavailable", "type": "invalid_request_error", "code": "unsupported_output_format"}}
    monkeypatch.setattr(runtime.services.transport, "curl_post_multipart", multipart)
    monkeypatch.setattr(direct_requests.image_artifacts, "require_svg_renderer", lambda: None)
    with pytest.raises(Exception, match="preview access or format unavailable"):
        await direct_requests._direct_edit_image_once(
            request(fmt, runtime), base_url_override="https://example.invalid/v1", api_key_override="test"
        )
    assert len(calls) == 1
    assert calls[0]["data"]["output_format"] == fmt


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["svg", "gif"])
async def test_native_edit_rejects_silent_png_substitution(monkeypatch, fmt):
    runtime = build_image_upstream_runtime()
    calls = []
    async def multipart(**kwargs):
        calls.append(kwargs)
        return 200, {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
    monkeypatch.setattr(runtime.services.transport, "curl_post_multipart", multipart)
    monkeypatch.setattr(direct_requests.image_artifacts, "require_svg_renderer", lambda: None)
    with pytest.raises(Exception) as error:
        await direct_requests._direct_edit_image_once(
            request(fmt, runtime), base_url_override="https://example.invalid/v1", api_key_override="test"
        )
    assert error.value.error_code == runtime.services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    assert len(calls) == 1


@pytest.mark.parametrize("fmt", ["svg", "gif"])
@pytest.mark.parametrize("overlay", ["mask", "markup"])
def test_native_edit_rejects_overlay_without_discarding_it(fmt, overlay):
    item = request(fmt, None, **{overlay: png()})
    with pytest.raises(ValueError, match="mask or markup"):
        image_controls.validate_image_controls(item)
    with pytest.raises(ValueError, match="mask or markup"):
        direct_requests._validate_edit_format(item)


def test_native_edit_budget_before_renderer_or_network():
    item = request("gif", None, size="4096x4096", frame_count=9)
    with pytest.raises(ValueError, match="safety"):
        direct_requests._validate_edit_format(item)
