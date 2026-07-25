"""Outbound request URL construction and validated target binding."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

from typing import Any
from urllib.parse import quote

from ..upstream_clients.url_validation import validate_image_job_control_url


def _validated_byok_target_for_request(
    target: Any | None,
    url: str,
) -> Any | None:
    from ..provider_runtime.byok_context import validate_byok_http_target

    try:
        return validate_byok_http_target(target, url)
    except ValueError as exc:
        raise upstream_services().infrastructure.UpstreamError(
            "validated BYOK target does not match request URL",
            status_code=403,
            error_code=upstream_services().infrastructure.EC.UPSTREAM_INVALID_REQUEST.value,
            payload={"url": url},
        ) from exc


def _api_base(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _responses_url(base: str) -> str:
    return _api_base(base) + "/responses"


def _image_generations_url(base: str) -> str:
    return _api_base(base) + "/images/generations"


def _image_edits_url(base: str) -> str:
    return _api_base(base) + "/images/edits"


def _image_jobs_url(base: str) -> str:
    return _api_base(base) + "/image-jobs"


def _image_job_status_url(base: str, job_id: str) -> str:
    return f"{_image_jobs_url(base)}/{quote(job_id, safe='')}"


def _validate_image_job_base_url(raw_base: str) -> str:
    try:
        return validate_image_job_control_url(
            raw_base,
            allow_private_http=True,
        )
    except ValueError as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"image job configuration unavailable: {exc}",
            status_code=503,
            error_code=upstream_services().infrastructure.EC.SERVICE_UNAVAILABLE.value,
            payload={
                "path": "image-jobs",
                "configuration": "sidecar_url",
                "reason": "configuration_unavailable",
            },
        ) from None


__all__ = [
    "_api_base",
    "_image_edits_url",
    "_image_generations_url",
    "_image_job_status_url",
    "_image_jobs_url",
    "_responses_url",
    "_validate_image_job_base_url",
    "_validated_byok_target_for_request",
]
