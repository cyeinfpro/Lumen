from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest


IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
if str(IMAGE_JOB_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGE_JOB_DIR))

from image_job.config import ImageJobSettings  # noqa: E402
from image_job.adapters.http_upstream import (  # noqa: E402
    RedirectSafeAsyncClient,
)
from image_job.url_security import (  # noqa: E402
    UpstreamRedirectGuard,
    UpstreamUrlPolicyError,
)


VALID_RUNTIME_ENV = {
    "IMAGE_JOB_SIDECAR_TOKEN": "s" * 32,
    "IMAGE_JOB_CREDENTIAL_ACTIVE_KEY_ID": "test-v1",
    "IMAGE_JOB_CREDENTIAL_MASTER_SECRET": "test-master-secret-" + "x" * 32,
}


def _settings(upstream_base_url: str) -> ImageJobSettings:
    return ImageJobSettings.from_env(
        {
            **VALID_RUNTIME_ENV,
            "IMAGE_JOB_UPSTREAM_BASE_URL": upstream_base_url,
        }
    )


def _addrinfo(ip: str, port: int) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes = b"") -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self):
        if self.body:
            yield self.body

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "upstream_base_url",
    [
        "https://api.example.com",
        "https://8.8.8.8/v1",
        "http://127.0.0.1:8081",
        "http://10.20.30.40:8081/v1",
        "http://169.254.10.20",
        "http://[::1]:8081",
        "http://localhost:8081",
        "http://worker.localhost:8081",
    ],
)
def test_upstream_base_url_accepts_https_and_explicit_local_http(
    upstream_base_url: str,
) -> None:
    settings = _settings(upstream_base_url)

    settings.validate()

    assert settings.upstream_base_url == upstream_base_url


@pytest.mark.parametrize(
    "upstream_base_url",
    [
        "http://api.example.com",
        "http://8.8.8.8",
        "http://service.internal",
        "http://localhost.example.com",
        "http://0.0.0.0",
        "http://192.0.2.10",
        "http://255.255.255.255",
        "http://[::]",
        "http://[2001:db8::1]",
    ],
)
def test_upstream_base_url_rejects_public_or_named_http_targets(
    upstream_base_url: str,
) -> None:
    with pytest.raises(RuntimeError, match="must use HTTPS"):
        _settings(upstream_base_url)


@pytest.mark.parametrize(
    ("upstream_base_url", "message"),
    [
        ("https://user@example.com", "username or password"),
        ("https://user:password@example.com", "username or password"),
        ("https://example.com?mode=fast", "query or fragment"),
        ("https://example.com#section", "query or fragment"),
        ("https://example.com?", "query or fragment"),
        ("https://example.com#", "query or fragment"),
    ],
)
def test_upstream_base_url_rejects_unsafe_components(
    upstream_base_url: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _settings(upstream_base_url)


@pytest.mark.parametrize(
    "upstream_base_url",
    [
        "ftp://example.com",
        "https:///v1/images",
        "https://",
        "example.com",
    ],
)
def test_upstream_base_url_rejects_invalid_scheme_or_missing_host(
    upstream_base_url: str,
) -> None:
    with pytest.raises(RuntimeError, match="valid http or https URL with a host"):
        _settings(upstream_base_url)


def test_upstream_base_url_validation_does_not_resolve_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_dns(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("startup URL validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)

    _settings("https://upstream.example.test").validate()


@pytest.mark.asyncio
async def test_redirect_guard_rejects_public_to_private_dns_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        host: str,
        port: int,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        addresses = {
            "api.example.test": "93.184.216.34",
            "redirect.example.test": "169.254.169.254",
        }
        return [_addrinfo(addresses[host], port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guard = UpstreamRedirectGuard("https://api.example.test")
    initial = await guard.resolve("https://api.example.test/v1/images")

    with pytest.raises(UpstreamUrlPolicyError, match="private address"):
        await guard.resolve(
            "https://redirect.example.test/internal",
            previous_url=initial.url,
        )


@pytest.mark.asyncio
async def test_redirect_guard_rejects_same_origin_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = iter(("93.184.216.34", "127.0.0.1"))

    def fake_getaddrinfo(
        _host: str,
        port: int,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        return [_addrinfo(next(resolutions), port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guard = UpstreamRedirectGuard("https://api.example.test")
    initial = await guard.resolve("https://api.example.test/v1/images")

    with pytest.raises(UpstreamUrlPolicyError, match="DNS resolution changed"):
        await guard.resolve(
            "https://api.example.test/v1/images/redirected",
            previous_url=initial.url,
        )


@pytest.mark.asyncio
async def test_redirect_guard_allows_same_origin_explicit_private_upstream() -> None:
    guard = UpstreamRedirectGuard("http://127.0.0.1:8081")
    initial = await guard.resolve("http://127.0.0.1:8081/v1/images")
    redirected = await guard.resolve(
        "http://127.0.0.1:8081/v1/images/redirected",
        previous_url=initial.url,
    )

    assert initial.resolved_ips == ("127.0.0.1",)
    assert redirected.resolved_ips == ("127.0.0.1",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect_url", "message"),
    [
        ("https://user:password@example.com/result", "username or password"),
        ("http://93.184.216.34/result", "downgrade HTTPS"),
        ("ftp://example.com/result", "valid http"),
    ],
)
async def test_redirect_guard_rejects_unsafe_redirect_components(
    redirect_url: str,
    message: str,
) -> None:
    guard = UpstreamRedirectGuard("https://93.184.216.34")
    initial = await guard.resolve("https://93.184.216.34/v1/images")

    with pytest.raises(UpstreamUrlPolicyError, match=message):
        await guard.resolve(redirect_url, previous_url=initial.url)


@pytest.mark.asyncio
async def test_redirect_safe_client_follows_relative_redirect_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
    )
    calls: list[httpx.Request] = []
    response_streams: list[_ResponseStream] = []
    responses = iter(
        (
            (307, {"location": "/v1/images/redirected"}, b""),
            (200, {"content-type": "application/json"}, b'{"data": []}'),
        )
    )

    async def fake_open_pinned_response(
        request: httpx.Request,
        _target: object,
    ) -> httpx.Response:
        calls.append(request)
        status_code, headers, body = next(responses)
        response_stream = _ResponseStream(body)
        response_streams.append(response_stream)
        return httpx.Response(
            status_code,
            headers=headers,
            stream=response_stream,
            request=request,
        )

    monkeypatch.setattr(
        client,
        "_open_pinned_response",
        fake_open_pinned_response,
    )
    request = client.build_request(
        "POST",
        "https://93.184.216.34/v1/images",
        headers={"Authorization": "Bearer secret"},
        json={"prompt": "test"},
    )

    response = await client.send(request, stream=True)
    try:
        assert response.status_code == 200
        assert len(response.history) == 1
        assert [str(call.url) for call in calls] == [
            "https://93.184.216.34/v1/images",
            "https://93.184.216.34/v1/images/redirected",
        ]
        assert [call.method for call in calls] == ["POST", "POST"]
        assert calls[1].headers["Authorization"] == "Bearer secret"
        assert response_streams[0].closed is True
    finally:
        await response.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_safe_client_strips_authorization_cross_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
    )
    calls: list[httpx.Request] = []
    responses = iter(
        (
            (307, {"location": "https://8.8.8.8/redirected"}),
            (200, {"content-type": "application/json"}),
        )
    )

    async def fake_open_pinned_response(
        request: httpx.Request,
        _target: object,
    ) -> httpx.Response:
        calls.append(request)
        status_code, headers = next(responses)
        return httpx.Response(
            status_code,
            headers=headers,
            stream=_ResponseStream(),
            request=request,
        )

    monkeypatch.setattr(
        client,
        "_open_pinned_response",
        fake_open_pinned_response,
    )
    request = client.build_request(
        "POST",
        "https://93.184.216.34/v1/images",
        headers={"Authorization": "Bearer secret"},
        json={"prompt": "test"},
    )

    response = await client.send(request, stream=True)
    try:
        assert len(calls) == 2
        assert "Authorization" not in calls[1].headers
        assert calls[1].headers["Host"] == "8.8.8.8"
    finally:
        await response.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_safe_client_fails_closed_before_private_second_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
    )
    calls = 0

    async def fake_open_pinned_response(
        request: httpx.Request,
        _target: object,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            stream=_ResponseStream(),
            request=request,
        )

    monkeypatch.setattr(
        client,
        "_open_pinned_response",
        fake_open_pinned_response,
    )
    request = client.build_request(
        "POST",
        "https://93.184.216.34/v1/images",
        json={"prompt": "test"},
    )

    with pytest.raises(UpstreamUrlPolicyError, match="downgrade HTTPS"):
        await client.send(request, stream=True)

    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_safe_client_enforces_redirect_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
        max_redirects=1,
    )
    calls = 0

    async def fake_open_pinned_response(
        request: httpx.Request,
        _target: object,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"location": f"/redirect/{calls}"},
            stream=_ResponseStream(),
            request=request,
        )

    monkeypatch.setattr(
        client,
        "_open_pinned_response",
        fake_open_pinned_response,
    )
    request = client.build_request(
        "POST",
        "https://93.184.216.34/v1/images",
        json={"prompt": "test"},
    )

    with pytest.raises(httpx.TooManyRedirects):
        await client.send(request, stream=True)

    assert calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_safe_client_rejects_missing_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
    )

    async def fake_open_pinned_response(
        request: httpx.Request,
        _target: object,
    ) -> httpx.Response:
        return httpx.Response(
            307,
            stream=_ResponseStream(),
            request=request,
        )

    monkeypatch.setattr(
        client,
        "_open_pinned_response",
        fake_open_pinned_response,
    )
    request = client.build_request(
        "POST",
        "https://93.184.216.34/v1/images",
        json={"prompt": "test"},
    )

    with pytest.raises(UpstreamUrlPolicyError, match="missing Location"):
        await client.send(request, stream=True)

    await client.aclose()


def test_redirect_safe_client_disables_httpx_auto_redirects() -> None:
    client = RedirectSafeAsyncClient(
        upstream_base_url="https://93.184.216.34",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=2),
    )

    assert client.follow_redirects is False
    asyncio.run(client.aclose())
