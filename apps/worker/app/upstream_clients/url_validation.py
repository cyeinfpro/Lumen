"""Network boundary validation for the image-job control plane."""

from __future__ import annotations

import ipaddress
from collections.abc import Collection
from urllib.parse import urlsplit

_PLACEHOLDER_HOSTS = frozenset({"example.com", "example.net", "example.org"})
_PRIVATE_DNS_SUFFIXES = (".internal", ".local", ".localhost")


def _is_private_control_host(
    host: str,
    *,
    allowed_http_hosts: Collection[str],
) -> bool:
    if host in {item.lower().rstrip(".") for item in allowed_http_hosts}:
        return True
    if host == "localhost" or host.endswith(_PRIVATE_DNS_SUFFIXES):
        return True
    if "." not in host and ":" not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def validate_image_job_control_url(
    raw_base: str,
    *,
    allow_private_http: bool,
    allowed_http_hosts: Collection[str] = (),
) -> str:
    base = (raw_base or "").strip().rstrip("/")
    parts = urlsplit(base)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not host
        or any(char.isspace() for char in host)
    ):
        raise ValueError(
            "IMAGE_JOB_BASE_URL must be an http or https URL with a hostname"
        )
    if parts.username or parts.password:
        raise ValueError("IMAGE_JOB_BASE_URL must not include credentials")
    if parts.query or parts.fragment:
        raise ValueError("IMAGE_JOB_BASE_URL must not include query or fragment")
    if any(
        host == placeholder or host.endswith(f".{placeholder}")
        for placeholder in _PLACEHOLDER_HOSTS
    ):
        raise ValueError("IMAGE_JOB_BASE_URL must not use a placeholder hostname")
    if scheme == "http" and (
        not allow_private_http
        or not _is_private_control_host(
            host,
            allowed_http_hosts=allowed_http_hosts,
        )
    ):
        raise ValueError("IMAGE_JOB_BASE_URL must use HTTPS for non-private hosts")
    return base


__all__ = ["validate_image_job_control_url"]
