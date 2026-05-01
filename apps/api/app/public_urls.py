"""Helpers for public URLs copied out of the app."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import validate_public_base_url

from .config import settings


logger = logging.getLogger(__name__)

PUBLIC_BASE_URL_SETTING_KEY = "site.public_base_url"


def _normalize_public_base_url(raw: object | None) -> str | None:
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    try:
        return validate_public_base_url(raw)
    except ValueError:
        logger.warning("invalid_public_base_url_ignored", extra={"value": raw})
        return None


def _has_public_hostname(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _origin_from_absolute_url(raw: str | None) -> str | None:
    value = _first_header_value(raw)
    if not value or value.lower() == "null":
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return _normalize_public_base_url(origin)


def _origin_from_host(host: str | None, scheme: str | None) -> str | None:
    clean_host = _first_header_value(host)
    clean_scheme = _first_header_value(scheme) or "http"
    if not clean_host:
        return None
    if any(ch in clean_host for ch in "/?#@\\"):
        return None
    if clean_scheme.lower() not in {"http", "https"}:
        clean_scheme = "http"
    return _normalize_public_base_url(f"{clean_scheme.lower()}://{clean_host}")


def _origin_from_forwarded_header(raw: str | None) -> str | None:
    first = _first_header_value(raw)
    if not first:
        return None
    values: dict[str, str] = {}
    for part in first.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        values[key.strip().lower()] = value.strip().strip('"')
    return _origin_from_host(values.get("host"), values.get("proto"))


def request_public_origin(request: Request) -> str | None:
    """Best-effort origin from browser/proxy headers.

    Origin/Referer reflect the page the admin actually used. They are preferred
    because a Next.js rewrite may connect to the API with an internal Host.
    """
    headers = request.headers
    candidates = (
        _origin_from_absolute_url(headers.get("origin")),
        _origin_from_absolute_url(headers.get("referer")),
        _origin_from_forwarded_header(headers.get("forwarded")),
        _origin_from_host(
            headers.get("x-forwarded-host"),
            headers.get("x-forwarded-proto"),
        ),
        _origin_from_host(
            headers.get("host"),
            headers.get("x-forwarded-proto") or request.url.scheme,
        ),
    )
    for candidate in candidates:
        if candidate:
            return candidate
    return None


async def _configured_public_base_url(db: AsyncSession) -> str | None:
    row = (
        await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == PUBLIC_BASE_URL_SETTING_KEY
            )
        )
    ).scalar_one_or_none()
    return _normalize_public_base_url(row)


async def resolve_public_base_url(request: Request, db: AsyncSession) -> str:
    """Resolve the web root used for copied invitation/share URLs.

    Priority:
    1. DB-backed site.public_base_url override.
    2. Public PUBLIC_BASE_URL env / Settings fallback.
    3. Current browser/proxy origin.
    4. Non-public PUBLIC_BASE_URL env / Settings fallback.
    """
    configured = await _configured_public_base_url(db)
    if configured:
        return configured

    fallback = _normalize_public_base_url(settings.public_base_url)
    if fallback and _has_public_hostname(fallback):
        return fallback

    request_origin = request_public_origin(request)
    if request_origin:
        return request_origin

    return fallback or settings.public_base_url.rstrip("/")


__all__ = [
    "PUBLIC_BASE_URL_SETTING_KEY",
    "request_public_origin",
    "resolve_public_base_url",
]
