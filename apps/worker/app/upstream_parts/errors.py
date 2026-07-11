"""Exception types exposed through the ``app.upstream`` compatibility facade."""

from __future__ import annotations

from typing import Any


class UpstreamError(Exception):
    """Wrap upstream failures with retry-relevant status and error metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload or {}


class UpstreamCancelled(BaseException):
    """Terminate every upstream race lane and fallback retry immediately."""


__all__ = ["UpstreamCancelled", "UpstreamError"]
