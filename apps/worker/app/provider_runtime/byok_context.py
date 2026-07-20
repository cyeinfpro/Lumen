"""Request-scoped BYOK DNS pin context.

The context is deliberately independent from credential/database resolution.
Transport code only needs this small contract, not the full BYOK runtime.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from urllib.parse import urlsplit

from lumen_core.url_security import PublicHttpTarget


_BYOK_HTTP_TARGET_CONTEXT: ContextVar[PublicHttpTarget | None] = ContextVar(
    "byok_http_target",
    default=None,
)


def _http_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), port


def current_byok_http_target(url: str | None = None) -> PublicHttpTarget | None:
    target = _BYOK_HTTP_TARGET_CONTEXT.get()
    if target is None or not target.resolved_ips:
        return None
    if url is not None and _http_origin(target.url) != _http_origin(url):
        return None
    return target


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


def bind_byok_http_target(
    target: PublicHttpTarget | None,
) -> Token[PublicHttpTarget | None]:
    return _BYOK_HTTP_TARGET_CONTEXT.set(target)


def reset_byok_http_target(token: Token[PublicHttpTarget | None]) -> None:
    _BYOK_HTTP_TARGET_CONTEXT.reset(token)


__all__ = [
    "_BYOK_HTTP_TARGET_CONTEXT",
    "_http_origin",
    "bind_byok_http_target",
    "current_byok_http_target",
    "reset_byok_http_target",
    "validate_byok_http_target",
]
