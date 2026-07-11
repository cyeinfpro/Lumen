"""Bounded request-body and JSON parsing helpers for the image-job sidecar."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


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
