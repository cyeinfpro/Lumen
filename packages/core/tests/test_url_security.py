from __future__ import annotations

import socket
from typing import Any

import httpcore
import httpx
import pytest

import lumen_core.url_security as url_security
from lumen_core.url_security import (
    PublicHttpBodyTooLarge,
    PublicHttpTarget,
    assert_public_http_target,
    download_public_http_url,
    is_forbidden_ip,
    is_private_host,
    pinned_async_http_transport,
    resolve_public_http_target,
)


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_private_host_rejects_ipv4_mapped_ipv6_loopback() -> None:
    assert is_forbidden_ip("::ffff:127.0.0.1")
    assert is_forbidden_ip("::ffff:169.254.169.254")
    assert is_private_host("::ffff:10.0.0.1")


def test_private_host_rejects_legacy_ipv4_spellings() -> None:
    assert is_private_host("127.1")
    assert is_private_host("0177.0.0.1")
    assert is_private_host("0x7f.0.0.1")
    assert is_private_host("2130706433")
    assert is_private_host("010.010.010.010")
    assert not is_private_host("93.184.216.34")


@pytest.mark.asyncio
async def test_assert_public_http_target_rejects_gaierror_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="cannot be resolved"):
        await assert_public_http_target("https://missing.example/v1")


@pytest.mark.asyncio
async def test_assert_public_http_target_allows_gaierror_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        raise socket.gaierror("temporary resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert (
        await assert_public_http_target(
            "https://temporarily-missing.example/v1",
            allow_unresolved=True,
        )
        == "https://temporarily-missing.example/v1"
    )


@pytest.mark.asyncio
async def test_resolve_public_http_target_returns_public_resolved_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        _host: str, port: int, *_args: Any, **_kwargs: Any
    ) -> list[tuple[Any, ...]]:
        return [_addrinfo("93.184.216.34", port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    target = await resolve_public_http_target("https://upstream.example/v1/")

    assert target.url == "https://upstream.example/v1"
    assert target.resolved_ips == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_assert_public_http_target_rejects_private_rebound_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        _host: str, port: int, *_args: Any, **_kwargs: Any
    ) -> list[tuple[Any, ...]]:
        return [_addrinfo("169.254.169.254", port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private address"):
        await assert_public_http_target("https://rebind.example/v1")


@pytest.mark.asyncio
async def test_assert_public_http_target_rejects_ipv4_mapped_rebound_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        _host: str, port: int, *_args: Any, **_kwargs: Any
    ) -> list[tuple[Any, ...]]:
        return [_addrinfo("::ffff:127.0.0.1", port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private address"):
        await assert_public_http_target("https://mapped-rebind.example/v1")


@pytest.mark.asyncio
async def test_pinned_transport_uses_validated_ip_with_original_host_and_sni() -> None:
    target = PublicHttpTarget(
        "https://cdn.example/images/result.png",
        ("93.184.216.34",),
    )
    response_bytes = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
    )

    class ScriptedStream:
        def __init__(self) -> None:
            self.reads = 0
            self.writes: list[bytes] = []
            self.sni_hosts: list[str] = []

        async def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
            _ = timeout
            self.reads += 1
            return response_bytes if self.reads == 1 else b""

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            _ = timeout
            self.writes.append(buffer)

        async def aclose(self) -> None:
            return None

        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> "ScriptedStream":
            _ = ssl_context, timeout
            self.sni_hosts.append(server_hostname or "")
            return self

        def get_extra_info(self, _info: str) -> Any:
            return None

    stream = ScriptedStream()

    class ScriptedBackend:
        def __init__(self) -> None:
            self.connected_hosts: list[str] = []

        async def connect_tcp(self, host: str, *_args: Any, **_kwargs: Any) -> Any:
            self.connected_hosts.append(host)
            return stream

        async def connect_unix_socket(
            self, *_args: Any, **_kwargs: Any
        ) -> Any:  # pragma: no cover - defensive interface parity
            raise AssertionError("unexpected unix socket")

        async def sleep(self, _seconds: float) -> None:
            return None

    transport = pinned_async_http_transport(target)
    network_backend = transport._pool._network_backend  # noqa: SLF001
    scripted_backend = ScriptedBackend()
    network_backend._backend = scripted_backend  # noqa: SLF001

    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get(target.url)

    assert response.content == b"OK"
    assert scripted_backend.connected_hosts == ["93.184.216.34"]
    assert stream.sni_hosts == ["cdn.example"]
    request_bytes = b"".join(stream.writes).lower()
    assert b"host: cdn.example\r\n" in request_bytes


@pytest.mark.asyncio
async def test_pinned_transport_caps_candidates_and_shares_connect_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = PublicHttpTarget(
        "https://cdn.example/result.png",
        tuple(f"203.0.113.{index}" for index in range(1, 7)),
    )
    transport = pinned_async_http_transport(target)
    network_backend = transport._pool._network_backend  # noqa: SLF001

    class FailingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float | None]] = []

        async def connect_tcp(
            self,
            host: str,
            _port: int,
            timeout: float | None = None,
            **_kwargs: Any,
        ) -> Any:
            self.calls.append((host, timeout))
            raise httpcore.ConnectError(f"cannot connect to {host}")

    failing_backend = FailingBackend()
    network_backend._backend = failing_backend  # noqa: SLF001
    monotonic_values = iter((100.0, 100.0, 100.2, 100.5, 100.9))
    monkeypatch.setattr(url_security, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(httpcore.ConnectError, match="203.0.113.4"):
        await network_backend.connect_tcp(
            "cdn.example",
            443,
            timeout=1.0,
        )

    assert [host for host, _timeout in failing_backend.calls] == [
        "203.0.113.1",
        "203.0.113.2",
        "203.0.113.3",
        "203.0.113.4",
    ]
    assert [timeout for _host, timeout in failing_backend.calls] == pytest.approx(
        [1.0, 0.8, 0.5, 0.1]
    )


@pytest.mark.asyncio
async def test_pinned_transport_preserves_last_error_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = PublicHttpTarget(
        "https://cdn.example/result.png",
        ("203.0.113.10", "203.0.113.11"),
    )
    transport = pinned_async_http_transport(target)
    network_backend = transport._pool._network_backend  # noqa: SLF001
    last_error = httpcore.ConnectTimeout("first candidate timed out")

    class TimeoutBackend:
        def __init__(self) -> None:
            self.calls = 0

        async def connect_tcp(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise last_error

    timeout_backend = TimeoutBackend()
    network_backend._backend = timeout_backend  # noqa: SLF001
    monotonic_values = iter((200.0, 200.0, 201.1))
    monkeypatch.setattr(url_security, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(httpcore.ConnectTimeout) as excinfo:
        await network_backend.connect_tcp(
            "cdn.example",
            443,
            timeout=1.0,
        )

    assert excinfo.value is last_error
    assert timeout_backend.calls == 1


@pytest.mark.asyncio
async def test_pinned_transport_raises_connect_error_without_candidate_error() -> None:
    class TruthyEmptyCandidates:
        def __bool__(self) -> bool:
            return True

        def __iter__(self):
            return iter(())

    target = PublicHttpTarget(
        "https://cdn.example/result.png",
        ("203.0.113.10",),
    )
    transport = pinned_async_http_transport(target)
    network_backend = transport._pool._network_backend  # noqa: SLF001
    network_backend._resolved_ips = TruthyEmptyCandidates()  # noqa: SLF001

    with pytest.raises(httpcore.ConnectError, match="without a transport error"):
        await network_backend.connect_tcp(
            "cdn.example",
            443,
            timeout=1.0,
        )


@pytest.mark.asyncio
async def test_download_public_http_url_revalidates_each_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_urls: list[str] = []
    requested_hosts: list[str] = []
    body_stream = _ChunkStream([b"abc", b"def"])

    async def fake_resolve(url: str, **kwargs: Any) -> PublicHttpTarget:
        assert kwargs["strip_trailing_slash"] is False
        resolved_urls.append(url)
        return PublicHttpTarget(url, ("93.184.216.34",))

    def fake_transport(target: PublicHttpTarget, **_kwargs: Any) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.headers["host"])
            assert request.headers["accept-encoding"] == "identity"
            if target.url.endswith("/start/"):
                return httpx.Response(302, headers={"location": "../final.png"})
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=body_stream,
            )

        return httpx.MockTransport(handler)

    monkeypatch.setattr(url_security, "resolve_public_http_target", fake_resolve)
    monkeypatch.setattr(url_security, "pinned_async_http_transport", fake_transport)

    result = await download_public_http_url(
        "https://cdn.example/start/",
        max_bytes=16,
    )

    assert resolved_urls == [
        "https://cdn.example/start/",
        "https://cdn.example/final.png",
    ]
    assert requested_hosts == ["cdn.example", "cdn.example"]
    assert result.url == "https://cdn.example/final.png"
    assert result.body == b"abcdef"
    assert result.redirects == 1
    assert body_stream.closed is True


@pytest.mark.asyncio
async def test_download_public_http_url_rejects_private_redirect_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_calls = 0

    async def fake_resolve(url: str, **_kwargs: Any) -> PublicHttpTarget:
        if url.startswith("http://127.0.0.1"):
            raise ValueError("base_url host is not allowed")
        return PublicHttpTarget(url, ("93.184.216.34",))

    def fake_transport(
        _target: PublicHttpTarget, **_kwargs: Any
    ) -> httpx.MockTransport:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
            )
        )

    monkeypatch.setattr(url_security, "resolve_public_http_target", fake_resolve)
    monkeypatch.setattr(url_security, "pinned_async_http_transport", fake_transport)

    with pytest.raises(ValueError, match="not allowed"):
        await download_public_http_url(
            "https://cdn.example/start",
            max_bytes=1024,
        )

    assert transport_calls == 1


@pytest.mark.asyncio
async def test_download_public_http_url_aborts_stream_over_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_stream = _ChunkStream([b"A" * 8, b"B" * 8, b"C" * 8])

    async def fake_resolve(url: str, **_kwargs: Any) -> PublicHttpTarget:
        return PublicHttpTarget(url, ("93.184.216.34",))

    monkeypatch.setattr(url_security, "resolve_public_http_target", fake_resolve)
    monkeypatch.setattr(
        url_security,
        "pinned_async_http_transport",
        lambda *_args, **_kwargs: httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=body_stream)
        ),
    )

    with pytest.raises(PublicHttpBodyTooLarge) as excinfo:
        await download_public_http_url(
            "https://cdn.example/large.png",
            max_bytes=16,
        )

    assert excinfo.value.max_bytes == 16
    assert excinfo.value.received_bytes == 24
    assert body_stream.yielded == 3
    assert body_stream.closed is True


@pytest.mark.asyncio
async def test_download_public_http_url_truncates_error_body_at_small_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_stream = _ChunkStream([b"A" * 8, b"B" * 8, b"C", b"D" * 8])

    async def fake_resolve(url: str, **_kwargs: Any) -> PublicHttpTarget:
        return PublicHttpTarget(url, ("93.184.216.34",))

    monkeypatch.setattr(url_security, "resolve_public_http_target", fake_resolve)
    monkeypatch.setattr(
        url_security,
        "pinned_async_http_transport",
        lambda *_args, **_kwargs: httpx.MockTransport(
            lambda _request: httpx.Response(502, stream=body_stream)
        ),
    )

    result = await download_public_http_url(
        "https://cdn.example/error",
        max_bytes=1024,
        max_error_bytes=16,
    )

    assert result.status_code == 502
    assert result.body == b"A" * 8 + b"B" * 8
    assert result.body_truncated is True
    assert body_stream.yielded == 3
    assert body_stream.closed is True
