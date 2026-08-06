"""Image-job terminal protocol validation and artifact delivery."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, NoReturn

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_client import ImageJobClient
from ..upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobResultState,
)
from .image_execution import (
    ImageRequestContext,
    ensure_image_request_context,
)
from .image_job_recovery import (
    emit_image_job_execution,
    image_job_recovery_error,
)
from .transport import ImageProgressCallback


def _result_metadata(
    *,
    job: dict[str, Any],
    first: dict[str, Any],
    payload: dict[str, Any],
    job_id: str,
    image_url: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "image_job_url": image_url,
        "job_id": job_id,
        "endpoint_used": job.get("endpoint_used") or payload.get("endpoint"),
    }
    for key in ("expires_at", "bytes", "width", "height", "format", "sha256"):
        value = first.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


async def _raise_protocol_error(
    *,
    message: str,
    execution: ImageJobExecutionHandle,
    progress_callback: ImageProgressCallback | None,
    status_code: int,
    runtime: ImageUpstreamRuntime | None,
) -> NoReturn:
    invalid = replace(
        execution,
        result_state=ImageJobResultState.UNCERTAIN,
        cost_knowledge=ImageJobCostKnowledge.UNKNOWN,
        sidecar_status="protocol_error",
    )
    await emit_image_job_execution(progress_callback, invalid, runtime=runtime)
    raise image_job_recovery_error(
        message,
        invalid,
        phase="terminal",
        status_code=status_code,
        runtime=runtime,
    )


async def _handle_non_success(
    *,
    job: dict[str, Any],
    status: Any,
    status_code: int,
    execution: ImageJobExecutionHandle,
    progress_callback: ImageProgressCallback | None,
    runtime: ImageUpstreamRuntime | None,
) -> None:
    services = resolve_image_upstream_services(runtime)
    if status == "failed":
        if bool(job.get("outcome_uncertain")):
            await _raise_protocol_error(
                message=(
                    "image job protocol error: failed cannot carry "
                    "outcome_uncertain=true"
                ),
                execution=execution,
                progress_callback=progress_callback,
                status_code=status_code,
                runtime=runtime,
            )
        failed = replace(
            execution,
            result_state=ImageJobResultState.FAILED,
            cost_knowledge=ImageJobCostKnowledge.NONE,
            sidecar_status="failed",
        )
        await emit_image_job_execution(progress_callback, failed, runtime=runtime)
        error = services.image_jobs.image_job_error(job, status_code=status_code)
        if isinstance(error, services.infrastructure.UpstreamError):
            error.payload = {
                **error.payload,
                "sidecar_execution_accepted": True,
                "sidecar_execution": failed.to_dict(),
            }
        raise error
    if status == "uncertain":
        if not bool(job.get("outcome_uncertain")):
            await _raise_protocol_error(
                message=(
                    "image job protocol error: uncertain requires "
                    "outcome_uncertain=true"
                ),
                execution=execution,
                progress_callback=progress_callback,
                status_code=status_code,
                runtime=runtime,
            )
        uncertain = replace(
            execution,
            result_state=ImageJobResultState.UNCERTAIN,
            cost_knowledge=ImageJobCostKnowledge.UNKNOWN,
            sidecar_status="uncertain",
        )
        await emit_image_job_execution(progress_callback, uncertain, runtime=runtime)
        raise image_job_recovery_error(
            "image job finished with an unresolved upstream result; "
            "the upstream request may already have been billed",
            uncertain,
            phase="terminal",
            status_code=status_code,
            runtime=runtime,
        )
    if status == "artifact_corrupt":
        corrupt = replace(
            execution,
            result_state=ImageJobResultState.UNCERTAIN,
            cost_knowledge=ImageJobCostKnowledge.INCURRED,
            sidecar_status="artifact_corrupt",
        )
        await emit_image_job_execution(progress_callback, corrupt, runtime=runtime)
        raise image_job_recovery_error(
            "image job artifact is corrupt; upstream cost is known incurred",
            corrupt,
            phase="delivery",
            status_code=status_code,
            runtime=runtime,
        )
    if status != "succeeded":
        unknown = replace(
            execution,
            sidecar_status=str(status or "unknown"),
        )
        await emit_image_job_execution(progress_callback, unknown, runtime=runtime)
        raise image_job_recovery_error(
            f"image job returned unknown status: {status!r}",
            unknown,
            phase="poll",
            status_code=status_code,
            runtime=runtime,
        )


async def finish_image_job(
    *,
    client: ImageJobClient,
    job: dict[str, Any],
    status_code: int,
    payload: dict[str, Any],
    base_url: str,
    proxy_url: str | None,
    progress_callback: ImageProgressCallback | None,
    execution: ImageJobExecutionHandle | None = None,
    job_id: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str | None]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = resolve_image_upstream_services(runtime)
    if execution is None:
        resolved_job_id = str(job_id or job.get("job_id") or "unknown")
        execution = ImageJobExecutionHandle(
            job_id=resolved_job_id,
            provider_id="unknown",
            endpoint=str(payload.get("endpoint") or "unknown"),
            base_url=base_url,
            idempotency_key=f"legacy:{resolved_job_id}",
        )
    status = job.get("status")
    await _handle_non_success(
        job=job,
        status=status,
        status_code=status_code,
        execution=execution,
        progress_callback=progress_callback,
        runtime=runtime,
    )
    images = job.get("images")
    first = images[0] if isinstance(images, list) and images else None
    image_url = first.get("url") if isinstance(first, dict) else None
    if not isinstance(first, dict) or not isinstance(image_url, str) or not image_url:
        succeeded = replace(
            execution,
            result_state=ImageJobResultState.SUCCEEDED,
            cost_knowledge=ImageJobCostKnowledge.INCURRED,
            sidecar_status="succeeded",
        )
        await emit_image_job_execution(progress_callback, succeeded, runtime=runtime)
        raise image_job_recovery_error(
            "image job succeeded without images[0].url",
            succeeded,
            phase="delivery",
            status_code=status_code,
            runtime=runtime,
        )
    artifact = {
        key: value
        for key, value in first.items()
        if key
        in {"url", "expires_at", "bytes", "width", "height", "format", "sha256"}
        and value is not None
    }
    succeeded = replace(
        execution,
        result_state=ImageJobResultState.SUCCEEDED,
        cost_knowledge=ImageJobCostKnowledge.INCURRED,
        sidecar_status="succeeded",
        result_artifact=artifact,
    )
    await emit_image_job_execution(progress_callback, succeeded, runtime=runtime)
    await services.transport.emit_image_progress(
        progress_callback,
        "image_job_image",
        **_result_metadata(
            job=job,
            first=first,
            payload=payload,
            job_id=execution.job_id,
            image_url=image_url,
        ),
    )
    try:
        raw = await services.image_jobs.download_image_job_result(
            client=client,
            image_url=image_url,
            proxy_url=proxy_url,
            allowed_base_url=base_url,
            request_context=request_context,
        )
        if not raw:
            raise ValueError("image job delivery returned an empty body")
    except Exception as exc:  # noqa: BLE001
        raise image_job_recovery_error(
            f"image job delivery failed: {exc}",
            succeeded,
            phase="delivery",
            status_code=getattr(exc, "status_code", None),
            cause=exc,
            runtime=runtime,
        ) from exc
    return services.infrastructure.base64.b64encode(raw).decode("ascii"), None
