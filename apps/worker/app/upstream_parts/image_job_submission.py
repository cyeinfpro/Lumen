"""Sidecar submission identity and idempotency headers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_auth import image_job_headers as build_auth_headers
from .transport import ImageProgressCallback


def image_job_sidecar_token(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = resolve_image_upstream_services(runtime)
    raw_token = str(
        getattr(services.infrastructure.settings, "image_job_sidecar_token", "") or ""
    )
    try:
        return services.infrastructure.validate_image_job_sidecar_token(raw_token)
    except ValueError as exc:
        raise services.infrastructure.UpstreamError(
            f"image job configuration unavailable: {exc}",
            status_code=503,
            error_code=services.infrastructure.EC.SERVICE_UNAVAILABLE.value,
            payload={
                "path": "image-jobs",
                "configuration": "sidecar_auth",
                "reason": "configuration_unavailable",
            },
        ) from None


def image_job_headers(
    *,
    api_key: str,
    trace_id: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, str]:
    return build_auth_headers(
        service_token=image_job_sidecar_token(runtime=runtime),
        upstream_api_key=api_key,
        trace_id=trace_id,
    )


def image_job_dispatch_attempt_hook(
    *,
    progress_callback: ImageProgressCallback | None,
    before_attempt: Callable[[int], Awaitable[None]] | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> Callable[[int], Awaitable[None]]:
    services = resolve_image_upstream_services(runtime)
    dispatch_ready_emitted = False

    async def prepare_attempt(attempt: int) -> None:
        nonlocal dispatch_ready_emitted
        if before_attempt is not None:
            await before_attempt(attempt)
        if not dispatch_ready_emitted:
            await services.transport.emit_image_progress(
                progress_callback,
                "dispatch_ready",
            )
            dispatch_ready_emitted = True

    return prepare_attempt


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
        digest = services.infrastructure.hashlib.sha256(
            payload_idempotency_key.encode("utf-8")
        ).hexdigest()
        headers.setdefault("Idempotency-Key", f"lumen-image-job-{digest[:32]}")
    else:
        services.core.attach_image_idempotency_key(
            headers,
            trace_id=trace_id,
            endpoint="image-jobs",
            body=payload,
        )
    return headers


__all__ = [
    "image_job_dispatch_attempt_hook",
    "image_job_headers",
    "image_job_sidecar_token",
    "image_job_submit_headers",
]
