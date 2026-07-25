from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.provider_runtime.contracts import ImageJobEndpoint
from app.upstream_clients.image_job_auth import UPSTREAM_AUTH_HEADER
from app.upstream_clients.image_job_client import ImageJobClient, ImageJobClientError
from app.upstream_clients.url_validation import validate_image_job_control_url


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=1.0)


def test_public_http_is_rejected_and_private_http_is_allowed() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_image_job_control_url(
            "http://public.example.test",
            allow_private_http=True,
        )

    assert (
        validate_image_job_control_url(
            "http://127.0.0.1:8090/",
            allow_private_http=True,
        )
        == "http://127.0.0.1:8090"
    )
    assert (
        validate_image_job_control_url(
            "http://image-job:8090/",
            allow_private_http=True,
        )
        == "http://image-job:8090"
    )


@pytest.mark.asyncio
async def test_submit_separates_service_and_provider_credentials() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"job_id": "job-1"})

    client = ImageJobClient(
        ImageJobEndpoint(
            base_url="https://jobs.test",
            service_token="service-token-a",
        ),
        timeout=_timeout(),
        transport=httpx.MockTransport(handler),
    )
    try:
        handle = await client.submit(
            {"request_type": "responses"},
            upstream_api_key="provider-key-a",
            trace_id="trace-1",
        )
    finally:
        await client.close()

    assert handle.job_id == "job-1"
    assert requests[0].headers["authorization"] == "Bearer service-token-a"
    assert requests[0].headers[UPSTREAM_AUTH_HEADER] == "Bearer provider-key-a"


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_client_cannot_take_a_proxy() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://other-origin.test/v1/image-jobs"},
        )

    client = ImageJobClient(
        ImageJobEndpoint(
            base_url="https://jobs.test",
            service_token="service-token",
        ),
        timeout=_timeout(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ImageJobClientError, match="returned invalid JSON"):
            await client.submit(
                {},
                upstream_api_key="provider-key",
                trace_id="trace-redirect",
            )
    finally:
        await client.close()

    assert len(requests) == 1
    with pytest.raises(TypeError):
        ImageJobClient(  # type: ignore[call-arg]
            ImageJobEndpoint(
                base_url="https://jobs.test",
                service_token="service-token",
            ),
            timeout=_timeout(),
            proxy="http://provider-proxy.test",
        )


@pytest.mark.asyncio
async def test_service_and_provider_tokens_rotate_independently() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers["authorization"],
                request.headers[UPSTREAM_AUTH_HEADER],
            )
        )
        return httpx.Response(202, json={"job_id": f"job-{len(seen)}"})

    for service_token, provider_key in (
        ("service-token-a", "provider-key-a"),
        ("service-token-b", "provider-key-a"),
        ("service-token-b", "provider-key-b"),
    ):
        client = ImageJobClient(
            ImageJobEndpoint(
                base_url="https://jobs.test",
                service_token=service_token,
            ),
            timeout=_timeout(),
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.submit(
                {},
                upstream_api_key=provider_key,
                trace_id="trace",
            )
        finally:
            await client.close()

    assert seen == [
        ("Bearer service-token-a", "Bearer provider-key-a"),
        ("Bearer service-token-b", "Bearer provider-key-a"),
        ("Bearer service-token-b", "Bearer provider-key-b"),
    ]


@pytest.mark.asyncio
async def test_client_policy_disables_environment_and_redirect_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = ImageJobClient(
        ImageJobEndpoint(
            base_url="https://jobs.test",
            service_token="service-token",
        ),
        timeout=_timeout(),
    )
    await client.close()

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert "proxy" not in captured
