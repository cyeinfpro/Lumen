from __future__ import annotations

import socket
from typing import Any

import pytest

from lumen_core.url_security import (
    assert_public_http_target,
    is_forbidden_ip,
    is_private_host,
    resolve_public_http_target,
)


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


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

    assert await assert_public_http_target(
        "https://temporarily-missing.example/v1",
        allow_unresolved=True,
    ) == "https://temporarily-missing.example/v1"


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
