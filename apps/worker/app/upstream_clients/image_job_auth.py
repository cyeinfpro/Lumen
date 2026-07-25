"""Credential separation for image-job control-plane requests."""

from __future__ import annotations

import hashlib

UPSTREAM_AUTH_HEADER = "X-Lumen-Upstream-Authorization"


def image_job_headers(
    *,
    service_token: str,
    upstream_api_key: str,
    trace_id: str,
) -> dict[str, str]:
    return {
        "authorization": f"Bearer {service_token}",
        UPSTREAM_AUTH_HEADER: f"Bearer {upstream_api_key}",
        "x-trace-id": trace_id,
    }


def image_job_submit_headers(
    *,
    service_token: str,
    upstream_api_key: str,
    trace_id: str,
    payload: dict[str, object],
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = image_job_headers(
        service_token=service_token,
        upstream_api_key=upstream_api_key,
        trace_id=trace_id,
    )
    payload_key = str(payload.get("idempotency_key") or "").strip()
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    elif payload_key:
        digest = hashlib.sha256(payload_key.encode("utf-8")).hexdigest()
        headers["Idempotency-Key"] = f"lumen-image-job-{digest[:32]}"
    return headers


def image_job_service_headers(
    *,
    service_token: str,
    trace_id: str,
    content_type: str | None = None,
) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {service_token}",
        "x-trace-id": trace_id,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


__all__ = [
    "UPSTREAM_AUTH_HEADER",
    "image_job_headers",
    "image_job_service_headers",
    "image_job_submit_headers",
]
