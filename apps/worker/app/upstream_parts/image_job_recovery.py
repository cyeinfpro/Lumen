"""Sidecar execution state transitions and recovery-only dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
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
    ImageJobHandle,
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
    status = str(cancel_result.status or "unknown").strip().lower()
    cost_knowledge = (
        ImageJobCostKnowledge.INCURRED
        if status in {"succeeded", "incurred"}
        else ImageJobCostKnowledge.UNKNOWN
        if cancel_result.outcome_uncertain
        else ImageJobCostKnowledge.NONE
        if status in {"failed", "cancelled"}
        else ImageJobCostKnowledge.UNKNOWN
    )
    return replace(
        execution,
        result_state=(
            ImageJobResultState.SUCCEEDED
            if status == "succeeded"
            else ImageJobResultState.FAILED
            if status == "failed"
            else ImageJobResultState.CANCELLED
            if status == "cancelled"
            else ImageJobResultState.UNCERTAIN
        ),
        cost_knowledge=cost_knowledge,
        sidecar_status=status,
        cancel_outcome=cancel_result.outcome,
    )


async def _cancel_recovered_image_job(
    request: ImageExecutionRequest,
    execution: ImageJobExecutionHandle,
    client: Any,
    provider: Any | None,
) -> ImageJobExecutionHandle:
    if execution.recovery_outcome.value == "deliver":
        cancel_result = ImageJobCancelResult(
            job_id=execution.job_id,
            outcome=ImageJobCancelOutcome.ALREADY_TERMINAL,
            status="succeeded",
            status_code=200,
            outcome_uncertain=False,
        )
        return execution_after_cancel(execution, cancel_result)
    try:
        resolved_provider = provider or await _resolve_image_job_execution_provider(
            request,
            execution,
        )
        cancel_result = await client.cancel(
            ImageJobHandle(
                job_id=execution.job_id,
                upstream_api_key=str(resolved_provider.api_key),
            ),
            trace_id=request.request_context.trace_id,
        )
    except Exception:  # noqa: BLE001
        services = _runtime_services(request.upstream_runtime)
        services.infrastructure.logger.warning(
            "sidecar recovery cancel outcome unknown job_id=%s endpoint=%s",
            execution.job_id,
            execution.endpoint,
            exc_info=True,
        )
        cancel_result = ImageJobCancelResult(
            job_id=execution.job_id,
            outcome=ImageJobCancelOutcome.UNCERTAIN,
            status="unknown",
            status_code=None,
            outcome_uncertain=True,
        )
    return execution_after_cancel(execution, cancel_result)


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
    if not isinstance(execution, ImageJobExecutionHandle):
        raise ValueError("sidecar execution handle is required for recovery")
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    await emit_image_job_execution(
        request.progress_callback,
        execution,
        runtime=runtime,
    )
    client = services.image_jobs.build_image_job_client(execution.base_url)
    provider: Any | None = None
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
    except (
        asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        cancelled_execution = await _cancel_recovered_image_job(
            request,
            execution,
            client,
            provider,
        )
        await emit_image_job_execution(
            request.progress_callback,
            cancelled_execution,
            runtime=runtime,
        )
        raise
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


def _recovery_executions(
    request: ImageExecutionRequest,
) -> tuple[ImageJobExecutionHandle, ...]:
    raw_execution = request.request_context.sidecar_execution
    if isinstance(raw_execution, ImageJobExecutionHandle):
        return (raw_execution,)
    if not isinstance(raw_execution, (list, tuple)):
        return ()
    by_endpoint = {
        execution.endpoint: execution
        for execution in raw_execution
        if isinstance(execution, ImageJobExecutionHandle)
    }
    endpoint_order = {"generations": 0, "responses": 1}
    return tuple(
        sorted(
            by_endpoint.values(),
            key=lambda execution: (
                endpoint_order.get(execution.endpoint, 2),
                execution.endpoint,
            ),
        )
    )


def _request_for_execution(
    request: ImageExecutionRequest,
    execution: ImageJobExecutionHandle,
    *,
    progress_callback: ImageProgressCallback | None,
) -> ImageExecutionRequest:
    return replace(
        request,
        progress_callback=progress_callback,
        request_context=replace(
            request.request_context,
            sidecar_execution=execution,
        ),
    )


async def _resume_image_job_lane(
    request: ImageExecutionRequest,
) -> list[tuple[str, str | None]]:
    return [await resume_image_job(request)]


async def resume_image_jobs(
    request: ImageExecutionRequest,
) -> AsyncIterator[tuple[str, str | None]]:
    executions = _recovery_executions(request)
    if not executions:
        raise ValueError("sidecar execution handle is required for recovery")
    if len(executions) == 1:
        yield await resume_image_job(
            _request_for_execution(
                request,
                executions[0],
                progress_callback=request.progress_callback,
            )
        )
        return

    from . import image_race

    race_name = "image_jobs recovery dual_race"
    progress_pairs = [
        image_race._image_job_lane_progress(  # noqa: SLF001
            request,
            lane_name=f"image_jobs:{execution.endpoint}",
            race_name=race_name,
            metadata_only=index > 0,
        )
        for index, execution in enumerate(executions)
    ]
    lane_progress = [progress for progress, _observation in progress_pairs]
    lane_observations = [observation for _progress, observation in progress_pairs]
    lane_requests = [
        _request_for_execution(
            request,
            execution,
            progress_callback=lane_progress[index],
        )
        for index, execution in enumerate(executions)
    ]
    tasks = [
        asyncio.create_task(
            _resume_image_job_lane(lane_request),
            name=(f"{request.action}-image-jobs-recovery-{executions[index].endpoint}"),
        )
        for index, lane_request in enumerate(lane_requests)
    ]
    lane_names = {
        task: f"image_jobs:{executions[index].endpoint}"
        for index, task in enumerate(tasks)
    }
    observations_by_task = {
        task: lane_observations[index] for index, task in enumerate(tasks)
    }
    async with aclosing(
        image_race._iter_dual_race_results(  # noqa: SLF001
            request,
            tasks,
            lane_names,
            grace_seconds=image_race._dual_race_grace_seconds(  # noqa: SLF001
                request,
                image_jobs=True,
            ),
            race_name=race_name,
            abort_result_unknown=False,
            lane_observations=observations_by_task,
        )
    ) as results:
        async for item in results:
            yield item


__all__ = [
    "emit_image_job_execution",
    "execution_after_cancel",
    "image_job_recovery_error",
    "_resolve_image_job_execution_provider",
    "resume_image_job",
    "resume_image_jobs",
]
