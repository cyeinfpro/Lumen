"""Sanitized response metadata for durable upstream delivery receipts."""

from __future__ import annotations

from typing import Any

from lumen_core.upstream_billing import (
    UPSTREAM_RESPONSE_HTTP_ATTEMPTS,
    UPSTREAM_RESPONSE_REQUEST_ID,
    UPSTREAM_RESPONSE_STATUS_CODE,
    UPSTREAM_RESPONSE_TRACE_ID,
)


_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-amzn-requestid",
    "x-amz-request-id",
)
_TRACE_ID_HEADERS = (
    "traceparent",
    "x-trace-id",
    "trace-id",
    "x-b3-traceid",
    "x-amzn-trace-id",
)


def _response_identifier(
    response_headers: Any,
    header_names: tuple[str, ...],
) -> str | None:
    if response_headers is None:
        return None
    for header_name in header_names:
        try:
            value = response_headers.get(header_name)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(value, str):
            continue
        sanitized = "".join(
            character for character in value.strip() if 32 <= ord(character) <= 126
        )
        if sanitized:
            return sanitized[:256]
    return None


def direct_image_response_metadata(
    response: Any,
    *,
    http_attempts: int,
) -> dict[str, Any]:
    return direct_image_response_metadata_from_headers(
        status_code=int(response.status_code),
        response_headers=getattr(response, "headers", None),
        http_attempts=http_attempts,
    )


def direct_image_response_metadata_from_headers(
    *,
    status_code: int,
    response_headers: Any,
    http_attempts: int,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        UPSTREAM_RESPONSE_STATUS_CODE: int(status_code),
        UPSTREAM_RESPONSE_HTTP_ATTEMPTS: max(1, int(http_attempts)),
    }
    request_id = _response_identifier(response_headers, _REQUEST_ID_HEADERS)
    if request_id is not None:
        metadata[UPSTREAM_RESPONSE_REQUEST_ID] = request_id
    response_trace_id = _response_identifier(response_headers, _TRACE_ID_HEADERS)
    if response_trace_id is not None:
        metadata[UPSTREAM_RESPONSE_TRACE_ID] = response_trace_id
    return metadata


__all__ = [
    "direct_image_response_metadata",
    "direct_image_response_metadata_from_headers",
]
