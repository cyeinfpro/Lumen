"""Network target validation helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Any
from urllib.parse import urlsplit


_PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}
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


class _PinnedAsyncNetworkBackend:
    def __init__(self, *, expected_host: str, resolved_ips: Iterable[str]) -> None:
        import httpcore

        self._backend = httpcore.AnyIOBackend()
        self._expected_host = canonical_host(expected_host)
        self._resolved_ips = tuple(sorted({str(ip) for ip in resolved_ips}))

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
        return await self._backend.connect_tcp(
            self._resolved_ips[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

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
        network_backend=_PinnedAsyncNetworkBackend(
            expected_host=host,
            resolved_ips=target.resolved_ips,
        ),
        socket_options=pool._socket_options,  # noqa: SLF001
    )
    return transport


def canonical_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


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
    if any(part > 0xFF for part in numbers[:-1]):
        return None
    last_bits = 8 * (5 - len(numbers))
    if numbers[-1] >= 1 << last_bits:
        return None
    value = numbers[-1]
    shift = last_bits
    for part in reversed(numbers[:-1]):
        value |= part << shift
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
) -> PublicHttpTarget:
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
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
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
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

    ips = {str(info[4][0]) for info in infos if info and info[4]}
    if not ips:
        if allow_unresolved:
            return PublicHttpTarget(value, ())
        raise ValueError("base_url host cannot be resolved")
    if not allow_private and any(is_forbidden_ip(ip) for ip in ips):
        raise ValueError("base_url resolves to a private address")
    return PublicHttpTarget(value, tuple(sorted(ips)))


__all__ = [
    "assert_public_http_target",
    "canonical_host",
    "is_forbidden_ip",
    "is_private_host",
    "pinned_async_http_transport",
    "PublicHttpTarget",
    "resolve_public_http_target",
]
