"""Preserve provider parameter errors across JSON and SSE transports."""

from __future__ import annotations

import json

import httpx
import pytest

from image_job.contracts import ERROR_CLASS_UPSTREAM_4XX, JobFailure

from .support import load_harness


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_format", ["json", "image_sse", "responses_sse"])
@pytest.mark.parametrize("shape", ["envelope", "nested_event", "flat_event"])
@pytest.mark.parametrize("code", ["unknown_parameter", "unsupported_parameter"])
@pytest.mark.parametrize("idempotency_guaranteed", [False, True])
async def test_parameter_error_is_not_lost_or_retried(
    wire_format: str,
    shape: str,
    code: str,
    idempotency_guaranteed: bool,
) -> None:
    app = load_harness()
    app.RETRY_NETWORK_MAX = 2
    app.RETRY_RESPONSES_STREAM_MAX = 2
    app.RETRY_UPSTREAM_5XX_MAX = 2
    app.RETRY_BACKOFF_S = 0
    app.UPSTREAM_IDEMPOTENCY_GUARANTEED = idempotency_guaranteed
    error = {
        "code": code,
        "message": "Unknown parameter: 'response_format'.",
        "param": "response_format",
    }
    if shape == "envelope":
        event = {"error": error}
    elif shape == "nested_event":
        event = {"type": "error", "error": error}
    else:
        event = {"type": "error", **error}
    encoded = json.dumps(event).encode()
    endpoint = (
        "/v1/responses" if wire_format == "responses_sse" else "/v1/images/generations"
    )
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if wire_format == "json":
            return httpx.Response(200, json=event)
        return httpx.Response(
            200,
            content=b"data: " + encoded + b"\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    row = {
        "job_id": "parameter-error",
        "auth_header": "Bearer sk-test",
        "payload_json": app.json_dump(
            {
                "endpoint": endpoint,
                "body": {"model": "gpt-image-2", "prompt": "A test image"},
                "retention_days": 1,
            }
        ),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        app._http_client = client
        with pytest.raises(JobFailure) as captured:
            await app.call_upstream(row)

    failure = captured.value
    assert failure.error_class == ERROR_CLASS_UPSTREAM_4XX
    assert code in failure.error
    assert "response_format" in failure.error
    assert failure.cost_proven_absent is True
    assert failure.outcome_uncertain is False
    assert failure.retryable is False
    assert len(calls) == 1
