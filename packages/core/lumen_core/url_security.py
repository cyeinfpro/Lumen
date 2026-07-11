"""Network target validation helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from time import monotonic
from typing import Any, cast
from urllib.parse import urljoin, urlsplit


_PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_ERROR_BODY_MAX_BYTES = 64 * 1024
_MAX_PINNED_CONNECT_CANDIDATES = 4
_FORBIDDEN_HOST_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


@dataclass(frozen=True)
class PublicHttpTarget:
    """A public URL plus the addresses validated at resolution time.

    Callers that fetch untrusted URLs must not treat this as a DNS pin by
    itself. To close DNS-rebinding/TOCTOU windows, bind the outbound request to
    one of ``resolved_ips`` while preserving the original host/SNI semantics, or
    perform an equivalent transport-level pin.
    """

    url: str
    resolved_ips: tuple[str, ...]


@dataclass(frozen=True)
class PublicHttpDownload:
    """A bounded response fetched through a DNS-pinned connection."""

    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    body_truncated: bool = False
    redirects: int = 0


class PublicHttpBodyTooLarge(ValueError):
    """Raised when a successful response exceeds its hard byte cap."""

    def __init__(
        self,
        *,
        url: str,
        max_bytes: int,
        received_bytes: int,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"response body exceeded {max_bytes} bytes")
        self.url = url
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes
        self.status_code = status_code


class PublicHttpRedirectError(ValueError):
    """Raised for malformed redirects or redirect budget exhaustion."""

    def __init__(self, message: str, *, url: str, status_code: int) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class _PinnedAsyncNetworkBackend:
    def __init__(self, *, expected_host: str, resolved_ips: Iterable[str]) -> None:
        import httpcore

        self._backend = httpcore.AnyIOBackend()
        self._expected_host = canonical_host(expected_host)
        self._resolved_ips = tuple(dict.fromkeys(str(ip) for ip in resolved_ips))[
            :_MAX_PINNED_CONNECT_CANDIDATES
        ]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        import httpcore

        clean_host = canonical_host(host)
        if clean_host != self._expected_host:
            raise httpcore.ConnectError("pinned target host mismatch")
        if not self._resolved_ips:
            raise httpcore.ConnectError("pinned target has no resolved public address")
        deadline = None if timeout is None else monotonic() + max(0.0, timeout)
        last_error: BaseException | None = None
        for resolved_ip in self._resolved_ips:
            remaining_timeout = (
                None if deadline is None else max(0.0, deadline - monotonic())
            )
            if remaining_timeout == 0.0 and last_error is not None:
                raise last_error
            try:
                return await self._backend.connect_tcp(
                    resolved_ip,
                    port,
                    timeout=remaining_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is None:
            raise httpcore.ConnectError(
                "pinned target connection failed without a transport error"
            )
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        return await self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def pinned_async_http_transport(target: PublicHttpTarget, **kwargs: Any) -> Any:
    """Return an httpx transport that connects only to target.resolved_ips.

    The HTTP request URL, Host header, SNI, and certificate verification remain
    bound to the original hostname. Only the TCP dial target is pinned.
    """

    import httpcore
    import httpx

    parsed = urlsplit(target.url)
    host = canonical_host(parsed.hostname or "")
    transport = httpx.AsyncHTTPTransport(trust_env=False, **kwargs)
    pool = transport._pool  # noqa: SLF001
    transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001
        ssl_context=pool._ssl_context,  # noqa: SLF001
        max_connections=pool._max_connections,  # noqa: SLF001
        max_keepalive_connections=pool._max_keepalive_connections,  # noqa: SLF001
        keepalive_expiry=pool._keepalive_expiry,  # noqa: SLF001
        http1=pool._http1,  # noqa: SLF001
        http2=pool._http2,  # noqa: SLF001
        retries=pool._retries,  # noqa: SLF001
        local_address=pool._local_address,  # noqa: SLF001
        uds=pool._uds,  # noqa: SLF001
        network_backend=cast(
            Any,
            _PinnedAsyncNetworkBackend(
                expected_host=host,
                resolved_ips=target.resolved_ips,
            ),
        ),
        socket_options=pool._socket_options,  # noqa: SLF001
    )
    return transport


def canonical_host(host: str) -> str:
    clean = host.strip().strip("[]").rstrip(".").lower()
    try:
        return clean.encode("idna").decode("ascii")
    except UnicodeError:
        return clean


def _is_forbidden_ip_address(ip: IPv4Address | IPv6Address) -> bool:
    embedded_ipv4: list[IPv4Address] = []
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped is not None:
            embedded_ipv4.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded_ipv4.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded_ipv4.append(ip.teredo[1])

    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or any(ip in network for network in _FORBIDDEN_HOST_NETWORKS)
        or any(_is_forbidden_ip_address(item) for item in embedded_ipv4)
    )


def is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.strip().strip("[]").split("%", 1)[0])
    return _is_forbidden_ip_address(ip)


def _parse_legacy_ipv4_part(part: str) -> int | None:
    if not part:
        return None
    try:
        if part.lower().startswith("0x"):
            return int(part[2:], 16)
        if len(part) > 1 and part.startswith("0"):
            return int(part[1:] or "0", 8)
        return int(part, 10)
    except ValueError:
        return None


def _parse_legacy_ipv4_host(host: str) -> IPv4Address | None:
    """Parse URL hosts that libc may treat as non-canonical IPv4 literals."""

    parts = host.split(".")
    if len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        number = _parse_legacy_ipv4_part(part)
        if number is None:
            return None
        numbers.append(number)
    if len(numbers) == 1:
        if numbers[0] > 0xFFFFFFFF:
            return None
        return IPv4Address(numbers[0])
    if any(number_part > 0xFF for number_part in numbers[:-1]):
        return None
    last_bits = 8 * (5 - len(numbers))
    if numbers[-1] >= 1 << last_bits:
        return None
    value = numbers[-1]
    shift = last_bits
    for number_part in reversed(numbers[:-1]):
        value |= number_part << shift
        shift += 8
    return IPv4Address(value)


def is_private_host(host: str) -> bool:
    clean = canonical_host(host)
    if not clean or clean in _PRIVATE_HOSTS:
        return True
    if clean.endswith(".localhost") or clean.endswith(".local"):
        return True
    if clean.isdigit():
        return True
    legacy_ipv4 = _parse_legacy_ipv4_host(clean)
    if legacy_ipv4 is not None:
        if clean != str(legacy_ipv4):
            return True
        return is_forbidden_ip(str(legacy_ipv4))
    try:
        return is_forbidden_ip(clean)
    except ValueError:
        return False


async def assert_public_http_target(
    base_url: str,
    *,
    allow_http: bool = False,
    allow_private: bool = False,
    allow_unresolved: bool = False,
    dns_timeout_s: float = 2.0,
) -> str:
    target = await resolve_public_http_target(
        base_url,
        allow_http=allow_http,
        allow_private=allow_private,
        allow_unresolved=allow_unresolved,
        dns_timeout_s=dns_timeout_s,
    )
    return target.url


async def resolve_public_http_target(
    base_url: str,
    *,
    allow_http: bool = False,
    allow_private: bool = False,
    allow_unresolved: bool = False,
    dns_timeout_s: float = 2.0,
    strip_trailing_slash: bool = True,
) -> PublicHttpTarget:
    raw_value = base_url.strip()
    value = raw_value.rstrip("/") if strip_trailing_slash else raw_value
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("base_url must be an http(s) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain username or password")
    if parsed.scheme != "https" and not allow_http:
        raise ValueError("base_url must use https")

    host = canonical_host(parsed.hostname or "")
    if not allow_private and is_private_host(host):
        raise ValueError("base_url host is not allowed")

    loop = asyncio.get_running_loop()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("base_url port is invalid") from exc
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=dns_timeout_s,
        )
    except socket.gaierror as exc:
        if allow_unresolved:
            return PublicHttpTarget(value, ())
        raise ValueError("base_url host cannot be resolved") from exc
    except TimeoutError as exc:
        if allow_unresolved:
            return PublicHttpTarget(value, ())
        raise ValueError("base_url host cannot be resolved") from exc

    ips = tuple(dict.fromkeys(str(info[4][0]) for info in infos if info and info[4]))
    if not ips:
        if allow_unresolved:
            return PublicHttpTarget(value, ())
        raise ValueError("base_url host cannot be resolved")
    try:
        has_forbidden_ip = any(is_forbidden_ip(ip) for ip in ips)
    except ValueError as exc:
        raise ValueError("base_url host resolved to an invalid address") from exc
    if not allow_private and has_forbidden_ip:
        raise ValueError("base_url resolves to a private address")
    return PublicHttpTarget(value, ips)


def _http_origin(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("trusted origin must be an http(s) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("trusted origin must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("trusted origin must not contain username or password")
    host = canonical_host(parsed.hostname or "")
    if not host:
        raise ValueError("trusted origin must include a host")
    return parsed.scheme, host, port


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def _read_bounded_body(
    response: Any,
    *,
    max_bytes: int,
    truncate: bool,
) -> tuple[bytes, bool, int]:
    body = bytearray()
    async for chunk in response.aiter_raw():
        if not chunk:
            continue
        next_size = len(body) + len(chunk)
        if next_size > max_bytes:
            if truncate:
                remaining = max_bytes - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
                return bytes(body), True, next_size
            return bytes(body), True, next_size
        body.extend(chunk)
    return bytes(body), False, len(body)


async def download_public_http_url(
    url: str,
    *,
    max_bytes: int,
    max_error_bytes: int = _DEFAULT_ERROR_BODY_MAX_BYTES,
    max_redirects: int = 5,
    allow_http: bool = True,
    allowed_private_origins: Sequence[str] = (),
    dns_timeout_s: float = 2.0,
    timeout: Any = None,
    headers: Mapping[str, str] | None = None,
) -> PublicHttpDownload:
    """Download an HTTP(S) URL through a bounded, DNS-pinned stream.

    Each redirect target is resolved and validated independently. The TCP dial
    uses only the validated address while the request URL remains unchanged, so
    HTTP Host, TLS SNI, and certificate hostname verification use the original
    hostname. Trusted private origins are an explicit escape hatch for
    configured same-origin services; redirects to other private origins remain
    blocked.
    """

    import httpx

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if max_error_bytes < 0:
        raise ValueError("max_error_bytes must be non-negative")
    if max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")

    trusted_origins = {_http_origin(origin) for origin in allowed_private_origins}
    request_headers = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() not in {"accept-encoding", "host"}
    }
    request_headers["Accept-Encoding"] = "identity"
    request_timeout = timeout or httpx.Timeout(30.0, connect=5.0)
    current_url = url.strip()
    redirects = 0

    while True:
        allow_private = bool(trusted_origins) and (
            _http_origin(current_url) in trusted_origins
        )
        target = await resolve_public_http_target(
            current_url,
            allow_http=allow_http,
            allow_private=allow_private,
            allow_unresolved=False,
            dns_timeout_s=dns_timeout_s,
            strip_trailing_slash=False,
        )
        transport = pinned_async_http_transport(target)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=request_timeout,
        ) as client:
            async with client.stream(
                "GET",
                target.url,
                headers=request_headers,
            ) as response:
                status_code = response.status_code
                response_headers = dict(response.headers)
                if status_code in _REDIRECT_STATUSES:
                    location = (response.headers.get("location") or "").strip()
                    if not location:
                        raise PublicHttpRedirectError(
                            "redirect response is missing Location",
                            url=target.url,
                            status_code=status_code,
                        )
                    if redirects >= max_redirects:
                        raise PublicHttpRedirectError(
                            "too many redirects",
                            url=target.url,
                            status_code=status_code,
                        )
                    current_url = urljoin(target.url, location)
                    redirects += 1
                    continue

                is_success = 200 <= status_code < 300
                body_limit = (
                    max_bytes if is_success else min(max_bytes, max_error_bytes)
                )
                declared_size = _content_length(response.headers)
                if declared_size is not None and declared_size > body_limit:
                    if is_success:
                        raise PublicHttpBodyTooLarge(
                            url=target.url,
                            max_bytes=max_bytes,
                            received_bytes=declared_size,
                            status_code=status_code,
                        )
                    return PublicHttpDownload(
                        url=target.url,
                        status_code=status_code,
                        headers=response_headers,
                        body=b"",
                        body_truncated=True,
                        redirects=redirects,
                    )

                body, truncated, received_bytes = await _read_bounded_body(
                    response,
                    max_bytes=body_limit,
                    truncate=not is_success,
                )
                if is_success and truncated:
                    raise PublicHttpBodyTooLarge(
                        url=target.url,
                        max_bytes=max_bytes,
                        received_bytes=received_bytes,
                        status_code=status_code,
                    )
                return PublicHttpDownload(
                    url=target.url,
                    status_code=status_code,
                    headers=response_headers,
                    body=body,
                    body_truncated=truncated,
                    redirects=redirects,
                )


__all__ = [
    "assert_public_http_target",
    "canonical_host",
    "download_public_http_url",
    "is_forbidden_ip",
    "is_private_host",
    "pinned_async_http_transport",
    "PublicHttpBodyTooLarge",
    "PublicHttpDownload",
    "PublicHttpRedirectError",
    "PublicHttpTarget",
    "resolve_public_http_target",
]
