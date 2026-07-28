"""Request-scoped BYOK DNS pin context.

The context is deliberately independent from credential/database resolution.
Transport code only needs this small contract, not the full BYOK runtime.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from lumen_core.url_security import PublicHttpTarget


def _http_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), port


def validate_byok_http_target(
    target: PublicHttpTarget | None,
    url: str,
) -> PublicHttpTarget | None:
    """Return a usable pin only when it matches the outbound request origin."""
    if target is None or not target.resolved_ips:
        return None
    if _http_origin(target.url) != _http_origin(url):
        raise ValueError("validated BYOK target origin does not match request URL")
    return target


__all__ = [
    "_http_origin",
    "validate_byok_http_target",
]
