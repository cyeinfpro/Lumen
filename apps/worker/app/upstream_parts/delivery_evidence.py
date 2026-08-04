"""Delivery and no-cost evidence helpers for image upstream failures."""

from __future__ import annotations

import httpx
from lumen_core.upstream_billing import (
    UPSTREAM_DISPATCH_PROVEN_NO_COST,
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
)

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)


_DISPATCH_RECEIPT_PAYLOAD_KEYS = (
    "receipt_reason",
    "upstream_receipt_reason",
    "upstream_dispatch_delivery",
)


def transport_error_proves_undelivered(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )


def dispatch_receipt_reason(exc: BaseException) -> str | None:
    reason = getattr(exc, "upstream_receipt_reason", None)
    if reason in {
        UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
        UPSTREAM_DISPATCH_PROVEN_NO_COST,
    }:
        return str(reason)
    if transport_error_proves_undelivered(exc):
        return UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    return None


def merged_dispatch_receipt_reason(errors: list[BaseException]) -> str | None:
    reasons = [dispatch_receipt_reason(exc) for exc in errors]
    if not reasons or any(reason is None for reason in reasons):
        return None
    if all(reason == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED for reason in reasons):
        return UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    return UPSTREAM_DISPATCH_PROVEN_NO_COST


def apply_dispatch_receipt(
    error: BaseException,
    reason: str | None,
) -> None:
    payload = getattr(error, "payload", None)
    if isinstance(payload, dict):
        for key in _DISPATCH_RECEIPT_PAYLOAD_KEYS:
            payload.pop(key, None)
        if reason is not None:
            payload["receipt_reason"] = reason
            payload["upstream_dispatch_delivery"] = reason
    if reason is not None:
        setattr(error, "upstream_receipt_reason", reason)


def image_job_submit_receipt_reason(exc: BaseException) -> str | None:
    if getattr(exc, "operation", None) != "submit":
        return None
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 300 <= status_code < 500:
        return UPSTREAM_DISPATCH_PROVEN_NO_COST
    if not (isinstance(status_code, int) and status_code > 0) and (
        transport_error_proves_undelivered(exc.__cause__)
    ):
        return UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    return None


def map_image_job_submit_evidence_error(
    exc: BaseException,
    *,
    reason: str,
    method: str,
    url: str,
    job_id: str | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = resolve_image_upstream_services(runtime)
    status_code = int(getattr(exc, "status_code", None) or 0)
    raw_payload = getattr(exc, "payload", None)
    if reason == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED:
        mapped = services.infrastructure.UpstreamError(
            str(exc),
            status_code=0,
            error_code=services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={},
        )
    else:
        mapped = services.core.parse_error(
            raw_payload if isinstance(raw_payload, dict) else {},
            status_code,
        )
    mapped = services.core.with_error_context(
        mapped,
        path="image-jobs",
        method=method,
        url=url,
    )
    mapped.payload["operation"] = getattr(exc, "operation", "submit")
    if job_id:
        mapped.payload["job_id"] = job_id
    mapped.payload.pop("upstream_result_unknown", None)
    mapped.payload.pop("response_received", None)
    apply_dispatch_receipt(mapped, reason)
    return mapped


__all__ = [
    "apply_dispatch_receipt",
    "dispatch_receipt_reason",
    "image_job_submit_receipt_reason",
    "map_image_job_submit_evidence_error",
    "merged_dispatch_receipt_reason",
    "transport_error_proves_undelivered",
]
