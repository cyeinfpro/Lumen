"""Progress callback handling shared by upstream transports."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)


ImageProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def maybe_record_usage_from_event(
    event: dict[str, Any],
    *,
    services: UpstreamServices | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    """Record terminal usage and warn about unknown response output types."""
    services = services or resolve_image_upstream_services(runtime)
    usage = event.get("usage")
    if not isinstance(usage, dict):
        response = event.get("response")
        if isinstance(response, dict):
            usage = response.get("usage")
    if isinstance(usage, dict):
        services.core.record_usage(usage)
    if services.core.is_responses_success_terminal(event.get("type")):
        response = event.get("response")
        if isinstance(response, dict):
            outputs = response.get("output")
            if isinstance(outputs, list):
                for item in outputs:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if (
                        isinstance(item_type, str)
                        and item_type not in services.core.KNOWN_OUTPUT_ITEM_TYPES
                    ):
                        services.infrastructure.logger.warning(
                            "upstream output item with unknown type=%r; skipping",
                            item_type,
                        )


async def emit_image_progress(
    progress_callback: ImageProgressCallback | None,
    event_type: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
    **payload: Any,
) -> None:
    if progress_callback is None:
        return
    event = {"type": event_type, **payload}
    services = resolve_image_upstream_services(runtime)
    try:
        result = progress_callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        if event_type == "response_ready":
            raise services.infrastructure.UpstreamError(
                "failed to persist the successful upstream response receipt; "
                "the request was not retried",
                status_code=200,
                error_code=(
                    services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
                ),
                payload={
                    "upstream_result_unknown": True,
                    "response_received": True,
                    "receipt_persist_failed": True,
                },
            ) from exc
        if event_type == "image_job_execution":
            execution = event.get("execution")
            error_payload: dict[str, Any] = {
                "path": "image-jobs",
                "phase": "receipt",
                "upstream_result_unknown": True,
                "receipt_persist_failed": True,
                "recovery_only": False,
            }
            if isinstance(execution, dict):
                error_payload.update(
                    {
                        "sidecar_execution_accepted": True,
                        "sidecar_execution": dict(execution),
                    }
                )
            raise services.infrastructure.UpstreamError(
                "failed to persist the accepted sidecar execution receipt; "
                "polling was stopped",
                status_code=202,
                error_code=(services.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value),
                payload=error_payload,
            ) from exc
        if event_type == "dispatch_ready":
            raise
        services.infrastructure.logger.warning(
            "image progress callback failed",
            exc_info=True,
        )
