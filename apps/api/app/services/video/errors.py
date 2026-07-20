"""Shared video service errors."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def video_http_error(
    code: str,
    message: str,
    status_code: int = 400,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=status_code,
        detail={"error": error},
    )
