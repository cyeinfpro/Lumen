from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Iterable, cast
from urllib.parse import urlsplit

import httpx


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


class ImageDownloadResolutionError(ValueError):
    """Transient DNS resolution failure for an otherwise valid public URL."""


@dataclass(frozen=True)
class PublicImageDownloadTarget:
    url: str
    resolved_ips: tuple[str, ...]


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
    return _is_forbidden_ip_address(
        ipaddress.ip_address(value.strip().strip("[]").split("%", 1)[0])
    )


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


async def resolve_public_image_download_target(
    url: str,
) -> PublicImageDownloadTarget:
    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("image URL must be http(s)") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("image URL must be http(s)")
    if parsed.username or parsed.password:
        raise ValueError("image URL must not contain username or password")
    host = canonical_host(parsed.hostname or "")
    if is_private_host(host):
        raise ValueError("image URL host is not allowed")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("image URL port is invalid") from exc
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=2.0,
        )
    except (socket.gaierror, TimeoutError) as exc:
        raise ImageDownloadResolutionError("image URL host cannot be resolved") from exc

    ips = tuple(dict.fromkeys(str(info[4][0]) for info in infos if info and info[4]))
    if not ips:
        raise ImageDownloadResolutionError("image URL host cannot be resolved")
    try:
        has_forbidden_ip = any(is_forbidden_ip(ip) for ip in ips)
    except ValueError as exc:
        raise ValueError("image URL host resolved to an invalid address") from exc
    if has_forbidden_ip:
        raise ValueError("image URL resolves to a private address")
    return PublicImageDownloadTarget(value, ips)


class _PinnedAsyncNetworkBackend:
    def __init__(self, *, expected_host: str, resolved_ips: Iterable[str]) -> None:
        import httpcore

        self._backend = httpcore.AnyIOBackend()
        self._expected_host = canonical_host(expected_host)
        self._resolved_ips = tuple(dict.fromkeys(str(ip) for ip in resolved_ips))

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> Any:
        import httpcore

        if canonical_host(host) != self._expected_host:
            raise httpcore.ConnectError("pinned target host mismatch")
        if not self._resolved_ips:
            raise httpcore.ConnectError("pinned target has no resolved public address")
        last_error: BaseException | None = None
        for resolved_ip in self._resolved_ips:
            try:
                return await self._backend.connect_tcp(
                    resolved_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
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


def pinned_async_http_transport(
    target: PublicImageDownloadTarget,
    **kwargs: Any,
) -> httpx.AsyncHTTPTransport:
    """Pin TCP to a validated address while keeping URL Host and TLS SNI."""

    import httpcore

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
