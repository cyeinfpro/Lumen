"""Pure request payload and idempotency helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Set
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class PayloadPolicy:
    allowed_fixed_endpoints: tuple[str, ...]
    allowed_prefix_endpoints: tuple[str, ...]
    image_output_formats: Set[str]
    default_image_output_format: str
    default_image_output_compression: int
    responses_strip_partial_images: bool
    max_endpoint_chars: int
    max_request_type_chars: int
    default_retention_days: int
    max_retention_days: int


def auth_hash(auth_header: str) -> str:
    credential = _bearer_credential(auth_header)
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def stable_json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json_dump(payload).encode("utf-8")).hexdigest()


def request_idempotency_key(
    request: Request,
    raw_payload: Any,
    *,
    max_bytes: int,
) -> str | None:
    raw = (request.headers.get("Idempotency-Key") or "").strip()
    if not raw and isinstance(raw_payload, dict):
        candidate = raw_payload.get("idempotency_key")
        raw = candidate.strip() if isinstance(candidate, str) else ""
    if not raw:
        return None
    if len(raw.encode("utf-8")) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"idempotency key exceeds {max_bytes} bytes",
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upstream_idempotency_key(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return f"image-job-{digest}"


def normalize_image_edit_input_transport(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() == "file":
        return "file"
    return "url"


def body_preview(data: bytes, limit: int = 20000) -> Any:
    try:
        parsed = json.loads(
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
        parsed = None
    if parsed is not None:
        return parsed
    text = data.decode("utf-8", "replace")
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def _bearer_credential(auth_header: str) -> str:
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        raise ValueError("authorization must use the Bearer scheme")
    credential = parts[1].strip()
    if not credential or any(char.isspace() for char in credential):
        raise ValueError("Bearer credential is missing or malformed")
    return credential


def require_auth(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    try:
        credential = _bearer_credential(auth)
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization: Bearer token",
        ) from None
    return f"Bearer {credential}"


def normalize_endpoint(value: Any, policy: PayloadPolicy) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="endpoint must be an absolute API path",
        )
    if len(value) > policy.max_endpoint_chars:
        raise HTTPException(
            status_code=400,
            detail=f"endpoint exceeds {policy.max_endpoint_chars} characters",
        )
    if "://" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid endpoint")
    if value in policy.allowed_fixed_endpoints:
        return value
    if any(value.startswith(prefix) for prefix in policy.allowed_prefix_endpoints):
        return value
    raise HTTPException(status_code=400, detail="unsupported image endpoint")


def infer_request_type(endpoint: str) -> str:
    if endpoint == "/v1/images/generations":
        return "generations"
    if endpoint == "/v1/images/edits":
        return "edits"
    if endpoint == "/v1/responses":
        return "responses"
    if endpoint.startswith("/v1beta/models/"):
        return "gemini"
    return "image"


def normalize_image_output_options(
    target: dict[str, Any],
    policy: PayloadPolicy,
) -> None:
    background = target.get("background")
    if background == "transparent":
        target["output_format"] = "png"
        target.pop("output_compression", None)
        return

    output_format = target.get("output_format")
    if output_format not in policy.image_output_formats:
        output_format = policy.default_image_output_format
        target["output_format"] = output_format

    if output_format in {"jpeg", "webp"} and target.get("output_compression") is None:
        target["output_compression"] = policy.default_image_output_compression
    elif output_format == "png":
        target.pop("output_compression", None)

    if target.get("background") not in {"auto", "opaque", "transparent"}:
        target["background"] = "auto"
    if target.get("moderation") not in {"auto", "low"}:
        target["moderation"] = "low"


def normalize_payload_body(
    endpoint: str,
    body: dict[str, Any],
    policy: PayloadPolicy,
) -> dict[str, Any]:
    normalized = copy.deepcopy(body)
    if endpoint in {"/v1/images/generations", "/v1/images/edits"}:
        normalize_image_output_options(normalized, policy)
    elif endpoint == "/v1/responses":
        tools = normalized.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and tool.get("type") == "image_generation":
                    normalize_image_output_options(tool, policy)
                    if policy.responses_strip_partial_images:
                        tool.pop("partial_images", None)
    return normalized


def validate_payload(payload: Any, policy: PayloadPolicy) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    endpoint = normalize_endpoint(payload.get("endpoint"), policy)
    body = payload.get("body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    body = normalize_payload_body(endpoint, body, policy)
    request_type = payload.get("request_type") or infer_request_type(endpoint)
    if not isinstance(request_type, str) or not request_type:
        raise HTTPException(
            status_code=400,
            detail="request_type must be a string",
        )
    if len(request_type) > policy.max_request_type_chars:
        raise HTTPException(
            status_code=400,
            detail=(f"request_type exceeds {policy.max_request_type_chars} characters"),
        )
    try:
        retention_days = int(
            payload.get("retention_days", policy.default_retention_days)
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="retention_days must be an integer",
        ) from None
    if retention_days < 1 or retention_days > policy.max_retention_days:
        raise HTTPException(
            status_code=400,
            detail=(
                f"retention_days must be between 1 and {policy.max_retention_days}"
            ),
        )
    validated = {
        "request_type": request_type,
        "endpoint": endpoint,
        "body": body,
        "retention_days": retention_days,
    }
    if endpoint == "/v1/images/edits":
        validated["image_edit_input_transport"] = normalize_image_edit_input_transport(
            payload.get("image_edit_input_transport")
        )
    return validated
