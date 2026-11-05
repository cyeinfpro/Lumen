"""Network-free regression for the required, independent SVG author model."""
import base64
import json
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.upstream_parts import direct_requests
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.upstream_parts.upstream_impl import build_image_upstream_runtime

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"/>'
# Explicit fake provider model, not an assertion of a real author model ID.
AUTHOR = {"model": "test-svg-author", "reasoning": {"effort": "high"}}


def request(runtime, **changes):
    values = dict(action="generate", prompt="contract test", size="3808x1632",
                  images=None, mask=None, n=1, quality="high", output_format="svg",
                  output_compression=None, background="auto", moderation="auto",
                  model=None, image_model="gpt-image-2.6-flare", complexity="low",
                  progress_callback=None, provider_override=None, user_id=None,
                  upstream_runtime=runtime)
    values.update(changes)
    # Lets the pre-fix code reach the real transport instead of failing at its
    # missing dataclass field. After the fix use the real request contract.
    svg = values.pop("svg", None)
    if "svg" in {field.name for field in fields(ImageExecutionRequest)}:
        return ImageExecutionRequest(**values, svg=svg)
    old = ImageExecutionRequest(**values)
    return SimpleNamespace(**{field.name: getattr(old, field.name)
                              for field in fields(old)}, svg=svg)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
@pytest.mark.parametrize("svg", [AUTHOR, {"model": "test-svg-author"}, None])
async def test_svg_author_reaches_real_transport(monkeypatch, model, svg):
    runtime = build_image_upstream_runtime()
    services = runtime.services
    bodies = []
    async def post(**kwargs):
        # Round-trip actual serialized payload; upstream rejects missing author.
        body = json.loads(json.dumps(kwargs["json_body"]))
        bodies.append(body)
        if svg is not None and body.get("svg", {}).get("model") != svg["model"]:
            return httpx.Response(400, json={"error": {
                "code": "missing_required_parameter",
                "message": "SVG generation requires 'svg.model'",
            }})
        return httpx.Response(200, json={"data": [
            {"b64_json": base64.b64encode(SVG).decode()}]})
    monkeypatch.setattr(services.lifecycle, "get_images_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(services.core, "post_with_retry", post)
    monkeypatch.setattr(direct_requests.image_artifacts, "require_svg_renderer", lambda: None)
    await services.direct.direct_generate_image_once(
        request(runtime, image_model=model, svg=svg),
        base_url_override="https://example.invalid/v1", api_key_override="test-unused",
        streaming_override=True,
    )
    assert len(bodies) == 1
    body = bodies[0]
    assert body["model"] == model
    assert body["size"] == "3808x1632"
    assert body["complexity"] == "low"
    assert body.get("svg") == svg
    assert ("svg" in body) == (svg is not None)
    assert not {"stream", "partial_images", "output_compression", "prompt_rewriter", "reasoning"} & body.keys()

@pytest.mark.parametrize("svg", [{}, {"model": " "}, {"model": "author", "reasoning": {"effort": "auto"}}])
def test_invalid_worker_author_rejected_before_dispatch(svg):
    from app.upstream_parts.image_controls import validate_image_controls
    with pytest.raises(ValueError):
        validate_image_controls(request(None, svg=svg))


@pytest.mark.asyncio
async def test_explicit_author_edit_and_sidecar_fail_before_network(monkeypatch):
    from app.upstream_parts import image_jobs
    def forbidden(*args, **kwargs):
        pytest.fail("must reject before resolving network services")
    monkeypatch.setattr(direct_requests, "_runtime_services", forbidden)
    monkeypatch.setattr(image_jobs, "_runtime_services", forbidden)
    with pytest.raises(ValueError, match="Native SVG editing is unsupported"):
        await direct_requests._direct_edit_image_once(
            request(None, action="edit", images=[b"source"], svg=AUTHOR),
            base_url_override="https://example.invalid", api_key_override="test-unused")
    with pytest.raises(ValueError, match="not edits"):
        await image_jobs._image_job_edit_once(
            request(None, action="edit", images=[b"source"], svg=AUTHOR),
            api_key_override="test-unused", provider_id="test", endpoint="edits")
    with pytest.raises(ValueError, match="direct Images API"):
        await image_jobs._image_job_generate_once(
            request(None, svg=AUTHOR), api_key_override="test-unused",
            provider_id="test", endpoint="generations")


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "gif"])
def test_non_svg_never_serializes_author(fmt):
    from app.upstream_parts.image_controls import validate_image_controls
    with pytest.raises(ValueError, match="SVG output"):
        validate_image_controls(request(None, output_format=fmt, svg=AUTHOR))
    body = {"model": "gpt-image-2.6-flare"}
    direct_requests._apply_generation_controls(
        body, request(None, output_format=fmt, size="512x512"), False)
    assert "svg" not in body


def test_persisted_request_worker_adapter_and_idempotency():
    from app.tasks.generation_parts.request_options import image_request_options
    from app.tasks.generation_parts.composition_support import build_image_execution_request
    from app.tasks.generation_parts.services import GenerationProviderRequest, GenerationProviderContext
    from app.upstream_image_requests import _image_idempotency_key
    persisted = {"model": "gpt-image-2.6-sunburst", "output_format": "svg",
                 "render_quality": "high", "complexity": "low", "svg": AUTHOR}
    options = image_request_options(persisted, size="3808x1632")
    provider = GenerationProviderRequest(
        prompt="contract", size="3808x1632", n=1, quality=options["render_quality"],
        output_format=options["output_format"], output_compression=None,
        background="auto", moderation="auto", model=options["responses_model"],
        image_model=options["image_model"], complexity=options["complexity"],
        svg=options["svg"], progress_callback=None, provider_override=None, user_id=None,
        context=GenerationProviderContext("trace", 1, "task", 1))
    execution = build_image_execution_request(provider, action="generate", upstream_runtime=None)
    assert execution.svg == AUTHOR
    assert execution.image_model == "gpt-image-2.6-sunburst"
    body = {"model": execution.image_model}
    direct_requests._apply_generation_controls(body, execution, True)
    def key(svg):
        return _image_idempotency_key(trace_id="trace", endpoint="images/generations",
                                      body={**body, "svg": svg})
    assert key(AUTHOR) != key({"model": "other-test-author"})
    assert key(AUTHOR) != key({"model": "test-svg-author", "reasoning": {"effort": "low"}})
    assert persisted["svg"] == AUTHOR
