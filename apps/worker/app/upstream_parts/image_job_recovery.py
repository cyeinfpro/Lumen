"""Sidecar execution state transitions and recovery-only dispatch."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_models import (
    ImageJobCancelOutcome,
    ImageJobCancelResult,
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobResultState,
)
from .image_execution import ImageExecutionRequest
from .transport import ImageProgressCallback


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


async def emit_image_job_execution(
    progress_callback: ImageProgressCallback | None,
    execution: ImageJobExecutionHandle,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    await services.transport.emit_image_progress(
        progress_callback,
        "image_job_execution",
        execution=execution.to_dict(),
    )


def image_job_recovery_error(
    message: str,
    execution: ImageJobExecutionHandle,
    *,
    phase: str,
    status_code: int | None = None,
    cause: BaseException | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    error = services.infrastructure.UpstreamError(
        message,
        status_code=status_code,
        error_code=services.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        payload={
            "path": "image-jobs",
            "method": "GET",
            "job_id": execution.job_id,
            "phase": phase,
            "recovery_only": execution.recovery_outcome.value != "terminal",
            "delivery_only": execution.recovery_outcome.value == "deliver",
            "upstream_result_unknown": True,
            "sidecar_execution": execution.to_dict(),
        },
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def execution_after_cancel(
    execution: ImageJobExecutionHandle,
    cancel_result: ImageJobCancelResult,
) -> ImageJobExecutionHandle:
    if cancel_result.outcome == ImageJobCancelOutcome.CANCELLED_BEFORE_DISPATCH:
        return replace(
            execution,
            result_state=ImageJobResultState.CANCELLED,
            cost_knowledge=ImageJobCostKnowledge.NONE,
            sidecar_status=cancel_result.status,
            cancel_outcome=cancel_result.outcome,
        )
    cost_knowledge = (
        ImageJobCostKnowledge.INCURRED
        if cancel_result.status == "succeeded"
        else ImageJobCostKnowledge.NONE
        if cancel_result.status in {"failed", "cancelled"}
        else ImageJobCostKnowledge.UNKNOWN
    )
    return replace(
        execution,
        result_state=(
            ImageJobResultState.SUCCEEDED
            if cancel_result.status == "succeeded"
            else ImageJobResultState.FAILED
            if cancel_result.status == "failed"
            else ImageJobResultState.UNCERTAIN
        ),
        cost_knowledge=cost_knowledge,
        sidecar_status=cancel_result.status,
        cancel_outcome=cancel_result.outcome,
    )


async def _resolve_image_job_execution_provider(
    request: ImageExecutionRequest,
    execution: ImageJobExecutionHandle,
) -> Any:
    provider = request.provider_override
    if getattr(provider, "name", None) == execution.provider_id:
        return provider
    services = _runtime_services(request.upstream_runtime)
    pool = await services.infrastructure.provider_pool.get_pool()
    peek = getattr(pool, "peek", None)
    try:
        if callable(peek):
            candidates = await peek(
                route="image_jobs",
                endpoint_kind=execution.endpoint,
            )
        else:
            candidates = await services.providers.pool_select_compat(
                pool,
                route="image_jobs",
                ignore_cooldown=True,
                endpoint_kind=execution.endpoint,
                acquire_inflight=False,
            )
    except TypeError:
        candidates = await peek(route="image_jobs") if callable(peek) else []
    for candidate in candidates:
        if getattr(candidate, "name", None) == execution.provider_id:
            return candidate
    raise image_job_recovery_error(
        f"sidecar recovery provider unavailable: {execution.provider_id}",
        execution,
        phase="recovery_provider",
        runtime=request.upstream_runtime,
    )


async def resume_image_job(
    request: ImageExecutionRequest,
) -> tuple[str, str | None]:
    execution = request.request_context.sidecar_execution
    if execution is None:
        raise ValueError("sidecar execution handle is required for recovery")
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    await emit_image_job_execution(
        request.progress_callback,
        execution,
        runtime=runtime,
    )
    client = services.image_jobs.build_image_job_client(execution.base_url)
    try:
        if execution.recovery_outcome.value == "deliver":
            result = await services.image_jobs.finish_image_job(
                client=client,
                job={
                    "job_id": execution.job_id,
                    "status": "succeeded",
                    "endpoint_used": execution.endpoint,
                    "images": [dict(execution.result_artifact or {})],
                },
                status_code=200,
                payload={"endpoint": execution.endpoint},
                base_url=execution.base_url,
                proxy_url=None,
                execution=execution,
                progress_callback=request.progress_callback,
                request_context=request.request_context,
                runtime=runtime,
            )
        elif execution.recovery_outcome.value == "poll":
            provider = await _resolve_image_job_execution_provider(
                request,
                execution,
            )
            result = await services.image_jobs.wait_image_job(
                client=client,
                payload={"endpoint": execution.endpoint},
                base_url=execution.base_url,
                api_key=str(provider.api_key),
                proxy_url=None,
                execution=execution,
                progress_callback=request.progress_callback,
                request_context=request.request_context,
                runtime=runtime,
            )
        else:
            raise image_job_recovery_error(
                "sidecar execution is terminal and cannot be regenerated",
                execution,
                phase="terminal",
                runtime=runtime,
            )
    finally:
        await client.close()
    source = "image_jobs" if request.action == "generate" else "image_jobs_edit"
    await services.transport.emit_image_progress(
        request.progress_callback,
        "provider_used",
        provider=execution.provider_id,
        route="image_jobs",
        source=source,
        endpoint=f"image-jobs:{execution.endpoint}",
        status="recovered",
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "final_image",
        source=source,
        endpoint_used=execution.endpoint,
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "completed",
        source=source,
        endpoint_used=execution.endpoint,
    )
    return result


__all__ = [
    "emit_image_job_execution",
    "execution_after_cancel",
    "image_job_recovery_error",
    "_resolve_image_job_execution_provider",
    "resume_image_job",
]
