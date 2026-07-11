from __future__ import annotations

import pytest
from fastapi import Request

from app import public_urls
from app.public_urls import request_public_origin, resolve_public_base_url


class _Scalar:
    def __init__(self, value: str | None):
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class _Db:
    def __init__(self, value: str | None):
        self.value = value

    async def execute(self, _stmt):
        return _Scalar(self.value)


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/invite_links",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )


def test_request_public_origin_prefers_browser_origin() -> None:
    request = _request(
        [
            (b"origin", b"https://lumen.example.com"),
            (b"host", b"127.0.0.1:8000"),
        ]
    )

    assert request_public_origin(request) == "https://lumen.example.com"


def test_request_public_origin_uses_forwarded_host() -> None:
    request = _request(
        [
            (b"x-forwarded-host", b"lumen.example.com"),
            (b"x-forwarded-proto", b"https"),
            (b"host", b"127.0.0.1:8000"),
        ]
    )

    assert request_public_origin(request) == "https://lumen.example.com"


@pytest.mark.asyncio
async def test_resolve_public_base_url_prefers_db_override() -> None:
    request = _request([(b"origin", b"https://wrong.example")])

    assert (
        await resolve_public_base_url(request, _Db("https://lumen.example.com/"))  # type: ignore[arg-type]
        == "https://lumen.example.com"
    )


@pytest.mark.asyncio
async def test_resolve_public_base_url_can_use_allowed_request_origin_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_urls.settings,
        "cors_allow_origins",
        "https://lumen.example.com",
    )
    request = _request(
        [
            (b"origin", b"https://lumen.example.com"),
            (b"host", b"192.0.2.1:3000"),
        ]
    )

    assert (
        await resolve_public_base_url(
            request,
            _Db(None),
            allow_request_origin=True,  # type: ignore[arg-type]
        )
        == "https://lumen.example.com"
    )


@pytest.mark.asyncio
async def test_resolve_public_base_url_prefers_public_env_over_internal_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_urls.settings,
        "public_base_url",
        "https://lumen.example.com",
    )
    request = _request([(b"host", b"192.0.2.1:3000")])

    assert (
        await resolve_public_base_url(request, _Db(None))  # type: ignore[arg-type]
        == "https://lumen.example.com"
    )


@pytest.mark.asyncio
async def test_resolve_public_base_url_does_not_trust_request_origin_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_urls.settings,
        "public_base_url",
        "http://192.0.2.1:3000",
    )
    request = _request([(b"origin", b"https://lumen.example.com")])

    assert (
        await resolve_public_base_url(request, _Db(None))  # type: ignore[arg-type]
        == "http://192.0.2.1:3000"
    )


@pytest.mark.asyncio
async def test_resolve_public_base_url_requires_config_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(public_urls.settings, "app_env", "prod")
    monkeypatch.setattr(
        public_urls.settings, "public_base_url", "http://localhost:3000"
    )
    request = _request([(b"origin", b"https://evil.example")])

    with pytest.raises(RuntimeError):
        await resolve_public_base_url(request, _Db(None))  # type: ignore[arg-type]
