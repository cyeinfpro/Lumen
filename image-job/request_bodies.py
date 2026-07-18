"""Bounded HTTP body, JSON, and SSE helpers for the image-job sidecar."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


_STREAM_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class JsonShapeLimits:
    max_depth: int
    max_array_items: int
    max_object_items: int
    max_total_values: int
    max_key_chars: int
    max_string_chars: int


def parse_content_length(headers: Any) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def parse_json_bytes(data: bytes) -> Any | None:
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        return None


def load_json_bytes(data: bytes, limits: JsonShapeLimits) -> Any:
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    validate_json_shape(value, limits)
    return value


def validate_json_shape(value: Any, limits: JsonShapeLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    values_seen = 0
    while stack:
        current, depth = stack.pop()
        values_seen += 1
        if values_seen > limits.max_total_values:
            raise HTTPException(
                status_code=400,
                detail=f"JSON exceeds {limits.max_total_values} total values",
            )
        if isinstance(current, dict):
            container_depth = depth + 1
            if container_depth > limits.max_depth:
                raise HTTPException(
                    status_code=400,
                    detail=f"JSON exceeds maximum depth {limits.max_depth}",
                )
            if len(current) > limits.max_object_items:
                raise HTTPException(
                    status_code=400,
                    detail=f"JSON object exceeds {limits.max_object_items} keys",
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    raise HTTPException(
                        status_code=400,
                        detail="JSON object keys must be strings",
                    )
                if len(key) > limits.max_key_chars:
                    raise HTTPException(
                        status_code=400,
                        detail=f"JSON key exceeds {limits.max_key_chars} characters",
                    )
                stack.append((item, container_depth))
        elif isinstance(current, list):
            container_depth = depth + 1
            if container_depth > limits.max_depth:
                raise HTTPException(
                    status_code=400,
                    detail=f"JSON exceeds maximum depth {limits.max_depth}",
                )
            if len(current) > limits.max_array_items:
                raise HTTPException(
                    status_code=400,
                    detail=f"JSON array exceeds {limits.max_array_items} items",
                )
            stack.extend((item, container_depth) for item in current)
        elif isinstance(current, str):
            if len(current) > limits.max_string_chars:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"JSON string exceeds {limits.max_string_chars} characters"
                    ),
                )
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise HTTPException(
                    status_code=400,
                    detail="JSON contains a non-finite number",
                )
        elif current is None or isinstance(current, (bool, int)):
            continue
        else:
            raise HTTPException(
                status_code=400,
                detail="JSON contains an unsupported value",
            )


async def read_request_body_bounded(
    request: Any,
    *,
    max_bytes: int,
) -> bytes:
    declared_size = parse_content_length(request.headers)
    if declared_size is not None and declared_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"request exceeds {max_bytes} bytes",
        )
    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"request exceeds {max_bytes} bytes",
            )
        body.extend(chunk)
    return bytes(body)


def stream_read_chunk_size(max_bytes: int) -> int:
    return max(1, min(_STREAM_READ_CHUNK_BYTES, max_bytes + 1))


def response_byte_iterator(
    response: Any,
    *,
    max_bytes: int,
    raw: bool = False,
) -> Any:
    factory = response.aiter_raw if raw else response.aiter_bytes
    try:
        return factory(chunk_size=stream_read_chunk_size(max_bytes))
    except TypeError as exc:
        if "chunk_size" not in str(exc):
            raise
        return factory()


class SseLineDecoder:
    """Incrementally decode SSE lines from bounded byte chunks."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._pending_cr = False

    def _finish_line(self) -> str:
        line = bytes(self._line).decode("utf-8", "replace")
        self._line.clear()
        return line

    def feed(self, chunk: bytes) -> list[str]:
        lines: list[str] = []
        for value in chunk:
            if self._pending_cr:
                if value == 0x0A:
                    lines.append(self._finish_line())
                    self._pending_cr = False
                    continue
                lines.append(self._finish_line())
                self._pending_cr = False

            if value == 0x0D:
                self._pending_cr = True
            elif value == 0x0A:
                lines.append(self._finish_line())
            else:
                self._line.append(value)
        return lines

    def finish(self) -> list[str]:
        lines: list[str] = []
        if self._pending_cr:
            lines.append(self._finish_line())
            self._pending_cr = False
        if self._line:
            lines.append(self._finish_line())
        return lines


async def read_download_body_bounded(
    response: Any,
    *,
    max_bytes: int,
    truncate: bool,
) -> tuple[bytes, bool, int]:
    body = bytearray()
    async for chunk in response_byte_iterator(
        response,
        max_bytes=max_bytes,
        raw=True,
    ):
        if not chunk:
            continue
        next_size = len(body) + len(chunk)
        if next_size > max_bytes:
            if truncate:
                remaining = max_bytes - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
            return bytes(body), True, next_size
        body.extend(chunk)
    return bytes(body), False, len(body)


async def read_response_body_bounded(
    response: Any,
    *,
    max_bytes: int,
    truncate: bool,
) -> tuple[bytes, bool, int]:
    declared_size = parse_content_length(response.headers)
    if declared_size is not None and declared_size > max_bytes:
        return b"", True, declared_size

    body = bytearray()
    async for chunk in response_byte_iterator(
        response,
        max_bytes=max_bytes,
    ):
        if not chunk:
            continue
        next_size = len(body) + len(chunk)
        if next_size > max_bytes:
            if truncate:
                remaining = max_bytes - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
            return bytes(body), True, next_size
        body.extend(chunk)
    return bytes(body), False, len(body)
