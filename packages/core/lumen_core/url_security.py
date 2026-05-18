"""Network target validation helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
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


def canonical_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or any(ip in network for network in _FORBIDDEN_HOST_NETWORKS)
    )


def is_private_host(host: str) -> bool:
    clean = canonical_host(host)
    if not clean or clean in _PRIVATE_HOSTS:
        return True
    if clean.endswith(".localhost") or clean.endswith(".local"):
        return True
    if clean.isdigit():
        return True
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
    except (socket.gaierror, TimeoutError) as exc:
        if allow_unresolved:
            return value
        raise ValueError("base_url host cannot be resolved") from exc

    ips = {str(info[4][0]) for info in infos if info and info[4]}
    if not ips:
        if allow_unresolved:
            return value
        raise ValueError("base_url host cannot be resolved")
    if not allow_private and any(is_forbidden_ip(ip) for ip in ips):
        raise ValueError("base_url resolves to a private address")
    return value


__all__ = [
    "assert_public_http_target",
    "canonical_host",
    "is_forbidden_ip",
    "is_private_host",
]
