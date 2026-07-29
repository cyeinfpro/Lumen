"""Sidecar submission identity and idempotency headers."""

from __future__ import annotations

from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)


def image_job_submit_headers(
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    trace_id: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, str]:
    services = resolve_image_upstream_services(runtime)
    headers = dict(headers)
    payload_idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if payload_idempotency_key:
        digest = (
            services.infrastructure.hashlib.sha256(
                payload_idempotency_key.encode("utf-8")
            ).hexdigest()
        )
        headers.setdefault("Idempotency-Key", f"lumen-image-job-{digest[:32]}")
    else:
        services.core.attach_image_idempotency_key(
            headers,
            trace_id=trace_id,
            endpoint="image-jobs",
            body=payload,
        )
    return headers


__all__ = ["image_job_submit_headers"]
