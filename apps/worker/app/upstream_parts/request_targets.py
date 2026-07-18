"""Outbound request URL construction and validated target binding."""

from __future__ import annotations

import importlib
from typing import Any
from urllib.parse import quote, urlsplit

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _validated_byok_target_for_request(
    target: Any | None,
    url: str,
) -> Any | None:
    from ..byok_runtime import validate_byok_http_target

    try:
        return validate_byok_http_target(target, url)
    except ValueError as exc:
        facade = _facade()
        raise facade.UpstreamError(
            "validated BYOK target does not match request URL",
            status_code=403,
            error_code=facade.EC.UPSTREAM_INVALID_REQUEST.value,
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
    facade = _facade()
    base = (raw_base or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise facade.UpstreamError(
            "image job base URL must be an http or https URL with a hostname",
            status_code=400,
            error_code=facade.EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    if parts.username or parts.password:
        raise facade.UpstreamError(
            "image job base URL must not include credentials",
            status_code=400,
            error_code=facade.EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    if parts.query or parts.fragment:
        raise facade.UpstreamError(
            "image job base URL must not include query or fragment",
            status_code=400,
            error_code=facade.EC.INVALID_VALUE.value,
            payload={"base_url": raw_base},
        )
    return base


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
