"""Strict Images API request and error-envelope regression coverage."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.upstream_parts.generated_payload import InlineImageBytes
from app.upstream_parts.image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
)
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


def _request(runtime: Any, model: str = "gpt-image-2") -> ImageExecutionRequest:
    return ImageExecutionRequest(
        action="generate",
        prompt="A bicycle by the sea",
        size="1024x1024",
        images=None,
        mask=None,
        n=1,
        quality="high",
        output_format="webp",
        output_compression=82,
        background="transparent",
        moderation="auto",
        model=None,
        progress_callback=None,
        provider_override=None,
        user_id=None,
        request_context=ImageRequestContext.create(upstream_runtime=runtime),
        upstream_runtime=runtime,
        image_model=model,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-2",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.6-flare",
        "gpt-image-2.6-sunburst",
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_strict_gpt_image_upstream_accepts_request_without_legacy_format(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    streaming: bool,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    bodies: list[dict[str, Any]] = []

    def accept(body: dict[str, Any]) -> None:
        bodies.append(dict(body))
        if "response_format" in body:
            raise services.infrastructure.UpstreamError(
                "Unknown parameter: 'response_format'.",
                status_code=400,
                error_code="unknown_parameter",
            )
        assert body["model"] == model
        assert body["output_format"] == "webp"
        assert body["background"] == "transparent"
        assert body["output_compression"] == 82

    async def client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def post(**kwargs: Any) -> httpx.Response:
        accept(kwargs["json_body"])
        await kwargs["before_attempt"](1)
        return httpx.Response(200, json={"data": [{"b64_json": "ZmluYWw="}]})

    async def stream(**kwargs: Any) -> Any:
        accept(kwargs["json_body"])
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {"content-type": "text/event-stream"})
        yield {"type": "image_generation.completed", "b64_json": "ZmluYWw="}

    monkeypatch.setattr(services.lifecycle, "get_images_client", client)
    monkeypatch.setattr(services.core, "post_with_retry", post)
    monkeypatch.setattr(services.transport, "iter_sse_curl", stream)

    result = await services.direct.direct_generate_image_once(
        _request(runtime, model),
        base_url_override="https://example.invalid/v1",
        api_key_override="sk-test",
        streaming_override=streaming,
    )

    assert result == [(InlineImageBytes(b"final"), None)]
    assert len(bodies) == 1
    assert "response_format" not in bodies[0]
    if streaming:
        assert bodies[0]["stream"] is True
        assert bodies[0]["partial_images"] == 0
    else:
        assert "stream" not in bodies[0]
        assert "partial_images" not in bodies[0]


def _parameter_error(shape: str) -> dict[str, Any]:
    detail = {
        "code": "unknown_parameter",
        "message": "Unknown parameter: 'response_format'.",
        "param": "response_format",
    }
    if shape == "envelope":
        return {"error": detail}
    if shape == "nested":
        return {"type": "error", "error": detail}
    if shape == "flat":
        return {"type": "error", **detail}
    return {"type": shape, "response": {"error": detail}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "envelope",
        "nested",
        "flat",
        "response.failed",
        "response.incomplete",
    ],
)
async def test_direct_stream_preserves_parameter_error_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    calls: list[int] = []

    async def client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def stream(**kwargs: Any) -> Any:
        calls.append(1)
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {"x-request-id": "req-error"})
        yield _parameter_error(shape)

    monkeypatch.setattr(services.lifecycle, "get_images_client", client)
    monkeypatch.setattr(services.transport, "iter_sse_curl", stream)

    with pytest.raises(services.infrastructure.UpstreamError) as captured:
        await services.direct.direct_generate_image_once(
            _request(runtime),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
            streaming_override=True,
        )

    error = captured.value
    assert error.error_code == "unknown_parameter"
    assert str(error) == "Unknown parameter: 'response_format'."
    assert error.payload["upstream_error"]["param"] == "response_format"
    assert error.payload.get("upstream_result_unknown") is not True
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["envelope", "flat"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_direct_json_parameter_error_is_not_reported_as_missing_image(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    streaming: bool,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    calls: list[int] = []
    payload = _parameter_error(shape)

    async def client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def post(**kwargs: Any) -> httpx.Response:
        calls.append(1)
        await kwargs["before_attempt"](1)
        return httpx.Response(200, json=payload)

    async def stream(**kwargs: Any) -> Any:
        calls.append(1)
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {"content-type": "application/json"})
        yield {"type": services.core.JSON_PAYLOAD_SENTINEL_TYPE, "payload": payload}

    monkeypatch.setattr(services.lifecycle, "get_images_client", client)
    monkeypatch.setattr(services.core, "post_with_retry", post)
    monkeypatch.setattr(services.transport, "iter_sse_curl", stream)

    with pytest.raises(services.infrastructure.UpstreamError) as captured:
        await services.direct.direct_generate_image_once(
            _request(runtime),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
            streaming_override=streaming,
        )

    error = captured.value
    assert error.error_code == "unknown_parameter"
    assert "response_format" in str(error)
    assert error.payload["upstream_error"]["param"] == "response_format"
    assert error.payload["response_received"] is True
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_code",
    [
        ({"data": []}, "no_image_returned"),
        ({"data": [{"b64_json": "%%%"}]}, "bad_response"),
    ],
)
async def test_missing_or_corrupt_image_is_not_an_explicit_parameter_rejection(
    payload: dict[str, Any],
    expected_code: str,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services

    with pytest.raises(services.infrastructure.UpstreamError) as captured:
        await services.core.extract_image_results(
            payload,
            200,
            request_context=ImageRequestContext.create(upstream_runtime=runtime),
        )

    assert captured.value.error_code == expected_code
    assert "upstream_error" not in captured.value.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason, expected_code",
    [
        ("max_output_tokens", "max_output_tokens"),
        ("", "response_incomplete"),
    ],
)
async def test_direct_incomplete_event_keeps_reason(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected_code: str,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    calls: list[int] = []

    async def client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def stream(**kwargs: Any) -> Any:
        calls.append(1)
        await kwargs["on_dispatch_ready"]()
        await kwargs["on_response_head"](200, {})
        yield {
            "type": "response.incomplete",
            "response": {"incomplete_details": {"reason": reason}},
        }

    monkeypatch.setattr(services.lifecycle, "get_images_client", client)
    monkeypatch.setattr(services.transport, "iter_sse_curl", stream)

    with pytest.raises(services.infrastructure.UpstreamError) as captured:
        await services.direct.direct_generate_image_once(
            _request(runtime),
            base_url_override="https://example.invalid/v1",
            api_key_override="sk-test",
            streaming_override=True,
        )

    assert captured.value.status_code == 200
    assert captured.value.error_code == expected_code
    assert captured.value.payload["upstream_error"]["reason"] == reason
    assert calls == [1]
