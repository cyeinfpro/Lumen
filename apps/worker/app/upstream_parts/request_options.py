"""Image request option normalization for the upstream runtime."""

from __future__ import annotations

import re
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)
from .. import upstream_image_requests

_LOG_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LOG_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_LOG_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")


def runtime_services(runtime: ImageUpstreamRuntime | None):
    return resolve_image_upstream_services(runtime)


def redact_upstream_log_text(value: str) -> str:
    text = _LOG_EMAIL_RE.sub("[email]", value)
    text = _LOG_BEARER_RE.sub("Bearer [redacted]", text)
    text = _LOG_API_KEY_RE.sub("[api_key]", text)
    return text[:300]


def summarize_upstream_error_detail(
    detail: dict[str, Any] | None,
) -> dict[str, Any] | str:
    if not isinstance(detail, dict):
        return "none"
    summary: dict[str, Any] = {}
    for key in ("code", "type", "param", "status"):
        value = detail.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
    message = detail.get("message")
    if isinstance(message, str) and message:
        summary["message"] = redact_upstream_log_text(message)
    if summary:
        return summary
    return {"keys": sorted(str(key) for key in detail.keys())[:10]}


def image_request_policy(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> upstream_image_requests.ImageRequestPolicy:
    """Snapshot current service policy so test fakes remain call-time visible."""
    services = runtime_services(runtime)
    core = services.core
    return services.infrastructure.upstream_image_requests.ImageRequestPolicy(
        upstream_model=services.infrastructure.UPSTREAM_MODEL,
        default_responses_model=core.DEFAULT_IMAGE_RESPONSES_MODEL,
        default_image_instructions=core.DEFAULT_IMAGE_INSTRUCTIONS,
        image_qualities=core.IMAGE_QUALITIES,
        image_output_formats=core.IMAGE_OUTPUT_FORMATS,
        image_backgrounds=core.IMAGE_BACKGROUNDS,
        image_moderations=core.IMAGE_MODERATIONS,
        default_image_quality="high",
        default_image_output_format=core.DEFAULT_IMAGE_OUTPUT_FORMAT,
        default_image_output_compression=core.DEFAULT_IMAGE_OUTPUT_COMPRESSION,
        default_image_background=core.DEFAULT_IMAGE_BACKGROUND,
        default_image_moderation=core.DEFAULT_IMAGE_MODERATION,
        transparent_matte_prompt_note=core.TRANSPARENT_MATTE_PROMPT_NOTE,
        partial_images_max_pixels=core.PARTIAL_IMAGES_MAX_PIXELS,
        image_job_retention_days=core.IMAGE_JOB_RETENTION_DAYS,
    )


def normalize_image_quality(
    value: str | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = runtime_services(runtime)
    return services.requests.normalize_image_quality(
        value,
        policy=image_request_policy(runtime=runtime),
    )


def normalize_image_output_format(
    value: str | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = runtime_services(runtime)
    return services.requests.normalize_image_output_format(
        value,
        policy=image_request_policy(runtime=runtime),
    )


def normalize_image_output_compression(
    value: int | None,
    *,
    output_format: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> int | None:
    services = runtime_services(runtime)
    return services.requests.normalize_image_output_compression(
        value,
        output_format=output_format,
        policy=image_request_policy(runtime=runtime),
    )


def normalize_image_background(
    value: str | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = runtime_services(runtime)
    return services.requests.normalize_image_background(
        value,
        policy=image_request_policy(runtime=runtime),
    )


def normalize_image_moderation(
    value: str | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = runtime_services(runtime)
    return services.requests.normalize_image_moderation(
        value,
        policy=image_request_policy(runtime=runtime),
    )


def add_image_output_options(
    body: dict[str, Any],
    *,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = runtime_services(runtime)
    services.requests.add_image_output_options(
        body,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        hooks=upstream_image_requests.ImageOutputOptionsHooks(
            normalize_image_background=services.core.normalize_image_background,
            normalize_image_output_format=services.core.normalize_image_output_format,
            normalize_image_output_compression=(
                services.core.normalize_image_output_compression
            ),
            normalize_image_moderation=services.core.normalize_image_moderation,
        ),
    )


def is_transparent_image_request(
    background: str | None,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = runtime_services(runtime)
    return services.requests.is_transparent_image_request(
        background,
        normalize_image_background=services.core.normalize_image_background,
    )


def append_transparent_matte_prompt(
    prompt: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = runtime_services(runtime)
    return services.requests.append_transparent_matte_prompt(
        prompt,
        policy=services.core.image_request_policy(),
    )


def transparent_matte_upstream_options(
    *,
    prompt: str,
    output_format: str | None,
    background: str | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str | None, str | None]:
    services = runtime_services(runtime)
    return services.requests.transparent_matte_upstream_options(
        prompt=prompt,
        output_format=output_format,
        background=background,
        hooks=upstream_image_requests.TransparentMatteHooks(
            is_transparent_image_request=(services.core.is_transparent_image_request),
            append_transparent_matte_prompt=(
                services.core.append_transparent_matte_prompt
            ),
        ),
    )
