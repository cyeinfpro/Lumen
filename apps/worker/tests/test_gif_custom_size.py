"""GIF size selection survives persisted task resolution and real direct shaping."""
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app.tasks.generation_parts.runner_dispatch_phase import resolve_generation_size
from app.tasks.generation_parts.request_options import image_request_options
from app.tasks.generation_parts.diagnostics import image_effective_params_snapshot
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.upstream_parts.upstream_impl import build_image_upstream_runtime
from app import image_artifacts


def gif_bytes():
    with io.BytesIO() as data:
        frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
        frames[0].save(data, "GIF", save_all=True, append_images=frames[1:], duration=200)
        for frame in frames:
            frame.close()
        return data.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["auto", "768x432"])
@pytest.mark.parametrize("model", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
async def test_gif_size_survives_worker_and_direct_transport(monkeypatch, size, model):
    persisted = {"model": model, "output_format": "gif", "render_quality": "high",
                 "frame_count": 2, "frame_duration_ms": 200}
    state = SimpleNamespace(generation=SimpleNamespace(upstream_request=persisted),
                            size_requested=size, aspect_ratio="16:9")
    resolved = resolve_generation_size(state)
    assert resolved.size == size
    assert (resolved.width, resolved.height) == ((None, None) if size == "auto" else (768, 432))
    options = image_request_options(persisted, size=resolved.size)
    runtime = build_image_upstream_runtime()
    captured = []
    raw = gif_bytes()
    async def post(**kwargs):
        captured.append(kwargs["json_body"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}]})
    monkeypatch.setattr(runtime.services.lifecycle, "get_images_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime.services.core, "post_with_retry", post)
    request = ImageExecutionRequest(
        action="generate", prompt="loop", size=resolved.size, images=None, mask=None,
        n=1, quality=options["render_quality"], output_format="gif", output_compression=None,
        background="auto", moderation="auto", model=None, image_model=options["image_model"],
        frame_count=options["frame_count"], frame_duration_ms=options["frame_duration_ms"],
        progress_callback=None, provider_override=None, user_id=None, upstream_runtime=runtime)
    await runtime.services.direct.direct_generate_image_once(
        request, base_url_override="https://example.invalid/v1", api_key_override="test-unused")
    assert len(captured) == 1
    assert captured[0]["size"] == size
    assert captured[0]["model"] == model
    # Unknown requested dimensions do not replace actual decoded dimensions.
    info = image_artifacts._inspect_generated_image_sync(raw)
    effective = image_effective_params_snapshot(
        options, size=resolved.size, width=info.width, height=info.height, mime="image/gif")
    assert effective["size"] == size
    assert effective["size_actual"] == "32x32"


def test_non_gif_auto_keeps_existing_preset_resolution():
    state = SimpleNamespace(generation=SimpleNamespace(upstream_request={"output_format": "png"}),
                            size_requested="auto", aspect_ratio="16:9")
    result = resolve_generation_size(state)
    assert result.size == "3840x2160"
    assert result.width == 3840 and result.height == 2160
