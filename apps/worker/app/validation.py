"""Shared validation helpers for worker-facing provider configuration."""

from __future__ import annotations

from urllib.parse import urlsplit


class ProviderBaseUrlValidationError(ValueError):
    status_code = 400
    error_code = "invalid_provider_base_url"

    def __init__(
        self, message: str, *, payload: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.payload = payload or {}


async def validate_provider_base_url(raw_base: str) -> str:
    base = (raw_base or "").strip()
    if not base or any(ord(ch) < 32 for ch in base):
        raise ProviderBaseUrlValidationError("invalid provider base URL")

    parsed = urlsplit(base)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProviderBaseUrlValidationError(
            "provider base URL must be an http or https URL with a hostname",
            payload={"base_url": base},
        )
    if parsed.username or parsed.password:
        raise ProviderBaseUrlValidationError(
            "provider base URL must not include credentials",
            payload={"base_url": base},
        )
    return base.rstrip("/")


__all__ = [
    "ProviderBaseUrlValidationError",
    "validate_provider_base_url",
]
