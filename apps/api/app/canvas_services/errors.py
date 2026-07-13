"""Canvas-specific HTTP errors."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def canvas_http(
    code: str,
    message: str,
    status_code: int = 400,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(status_code=status_code, detail={"error": error})


def not_found() -> HTTPException:
    return canvas_http("not_found", "canvas not found", 404)


def idempotency_conflict(message: str = "idempotency key conflict") -> HTTPException:
    return canvas_http("idempotency_conflict", message, 409)
