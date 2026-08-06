"""Asynchronous image-job payload, polling, and download runtime."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Awaitable, Callable

import httpx
from lumen_core.providers import ProviderProxyDefinition

from ..provider_runtime.contracts import ImageJobEndpoint
from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_auth import (
    UPSTREAM_AUTH_HEADER,
)
from ..upstream_clients.image_job_client import ImageJobClient, ImageJobClientError
from ..upstream_clients.image_job_models import (
    ImageJobExecutionHandle,
    ImageJobHandle,
)
from .delivery_evidence import (
    image_job_submit_receipt_reason,
    map_image_job_submit_evidence_error,
)
from .image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
    ensure_image_request_context,
)
from .image_job_failover import (
    image_job_submit_unknown_error as _submit_unknown_error,
    submit_failure_result_unknown,
)
from .image_job_recovery import (
    emit_image_job_execution as _emit_image_job_execution,
    execution_after_cancel as _execution_after_cancel,
    image_job_recovery_error as _image_job_recovery_error,
    resume_image_job as _resume_image_job,
    resume_image_jobs as _resume_image_jobs,
)
from .image_job_submission import (
    image_job_dispatch_attempt_hook as _image_job_dispatch_attempt_hook,
    image_job_sidecar_token as _image_job_sidecar_token,
)
from .image_job_terminal import finish_image_job as _finish_image_job
from .transport import ImageProgressCallback

_UPSTREAM_AUTH_HEADER = UPSTREAM_AUTH_HEADER


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def image_job_idempotency_key(
    *,
    context: ImageRequestContext,
    provider_id: str,
    endpoint: str,
) -> str:
    scope = context.quota_scope
    if scope is None:
        generation_id = context.trace_id
        execution_epoch = context.retry_attempt
    else:
        generation_id = scope.task_id
        execution_epoch = scope.attempt_epoch
    raw = (
        f"lumen:{generation_id}:{execution_epoch}:"
        f"{provider_id}:{endpoint}"
    )
    return f"lumen:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


async def validate_effective_image_job_configuration(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    """Fail startup when the effective strict image channel is unusable."""
    services = _runtime_services(runtime)
    if (
        await services.core.resolve_image_channel()
        != services.core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY
    ):
        return
    services.image_jobs.image_job_sidecar_token()
    await services.direct.resolve_image_job_base_url()


def _image_job_body_base(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any]:
    services = _runtime_services(runtime)
    return services.requests.image_job_body_base(
        prompt=prompt,
        size=size,
        n=1,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        policy=services.core.image_request_policy(),
        hooks=services.infrastructure.upstream_image_requests.ImageJobBodyHooks(
            transparent_matte_upstream_options=services.core.transparent_matte_upstream_options,
            normalize_image_quality=services.core.normalize_image_quality,
            add_image_output_options=services.core.add_image_output_options,
        ),
    )


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
    image_edit_input_transport: str | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any]:
    services = _runtime_services(runtime)
    return services.requests.image_job_payload(
        request_type=request_type,
        endpoint=endpoint,
        body=body,
        image_edit_input_transport=image_edit_input_transport,
        policy=services.core.image_request_policy(),
    )


def _build_responses_image_body(
    request: ImageExecutionRequest,
    *,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON body posted to ``/v1/responses`` for image generation.

    image_urls 优先（先 push 到 sidecar 拿短 URL），images（bytes）作 fallback；
    与直接流路径共用同一请求形状，prompt-cache 前缀保持一致。
    """
    services = _runtime_services(request.upstream_runtime)
    return services.requests.build_responses_image_body(
        action=request.action,
        prompt=request.prompt,
        size=request.size,
        images=request.images,
        quality=request.quality,
        output_format=request.output_format,
        output_compression=request.output_compression,
        background=request.background,
        moderation=request.moderation,
        model=request.model,
        image_urls=image_urls,
        retry_attempt=request.request_context.retry_attempt,
        policy=services.core.image_request_policy(),
        hooks=services.infrastructure.upstream_image_requests.ResponsesImageBodyHooks(
            normalize_image_quality=services.core.normalize_image_quality,
            transparent_matte_upstream_options=services.core.transparent_matte_upstream_options,
            add_image_output_options=services.core.add_image_output_options,
            parse_size_pixels=services.requests.parse_size_pixels,
            normalize_reference_image=services.references.normalize_reference_image,
            stable_sort_tools=services.core.stable_sort_tools,
            apply_retry_cache_busters=services.core.apply_retry_cache_busters,
            validate_responses_body=services.core.validate_responses_body,
        ),
    )


def _image_job_error(
    job: dict[str, Any],
    *,
    status_code: int = 200,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    upstream_status = job.get("upstream_status")
    try:
        status = int(upstream_status) if upstream_status is not None else status_code
    except (TypeError, ValueError):
        status = status_code
    upstream_body = job.get("upstream_body")
    # Sidecar tags every failed job with an error_class (transport/http/missing
    # image/...); the failover layer uses it to switch endpoint or provider.
    error_class = job.get("error_class")
    if isinstance(upstream_body, dict):
        exc = services.core.parse_error(upstream_body, status)
        exc.payload = {
            **exc.payload,
            "job_id": job.get("job_id"),
            "path": "image-jobs",
            "method": "GET",
            "image_job_error_class": error_class,
            "image_job_endpoint_used": job.get("endpoint_used"),
        }
        return exc
    err = job.get("error")
    message = err if isinstance(err, str) and err else "image job failed"
    return services.infrastructure.UpstreamError(
        message,
        status_code=status,
        error_code=services.infrastructure.EC.UPSTREAM_ERROR.value,
        payload={
            "job_id": job.get("job_id"),
            "path": "image-jobs",
            "method": "GET",
            "upstream_body": upstream_body,
            "image_job_error_class": error_class,
            "image_job_endpoint_used": job.get("endpoint_used"),
        },
    )


async def _download_image_job_result(
    *,
    client: ImageJobClient | httpx.AsyncClient,
    image_url: str,
    proxy_url: str | None,
    allowed_base_url: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> bytes:
    _ = client, proxy_url
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    return await services.direct.download_result_url_bytes(
        image_url,
        path="image-jobs/result",
        log_endpoint="image_jobs_download",
        description="image job result download",
        allowed_base_url=allowed_base_url,
        request_context=context,
    )


def _build_image_job_client(
    base_url: str,
    *,
    service_token: str | None = None,
    read_timeout_s: float | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> ImageJobClient:
    services = _runtime_services(runtime)
    settings = services.infrastructure.settings
    return ImageJobClient(
        ImageJobEndpoint(
            base_url=services.requests.validate_image_job_base_url(base_url),
            service_token=service_token or _image_job_sidecar_token(runtime=runtime),
            allow_private_http=True,
        ),
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=(
                settings.upstream_read_timeout_s
                if read_timeout_s is None
                else read_timeout_s
            ),
            write=settings.upstream_write_timeout_s,
            pool=settings.upstream_connect_timeout_s,
        ),
        post_with_retry=services.core.post_with_retry,
    )


def _map_image_job_client_error(
    exc: ImageJobClientError,
    *,
    method: str,
    url: str,
    job_id: str | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> Exception:
    services = _runtime_services(runtime)
    receipt_reason = image_job_submit_receipt_reason(exc)
    if receipt_reason is not None:
        return map_image_job_submit_evidence_error(
            exc,
            reason=receipt_reason,
            method=method,
            url=url,
            job_id=job_id,
            runtime=runtime,
        )
    # The client marker is authoritative for malformed responses; the classifier
    # also keeps legacy/custom clients fail-closed for any submit 5xx.
    if exc.result_unknown or submit_failure_result_unknown(exc):
        # 提交结果未知 = 可能已扣费：落进 IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES，
        # 禁止 failover（换 endpoint 换幂等键 = 第二笔上游成本），按 hold 结算。
        # 必须在 payload 分支之前判断：5xx 带响应体 = sidecar 已应答（请求已
        # 送达并可能被转发计费），同样是结果未知，不能降级成普通可重试错误。
        return _submit_unknown_error(exc, method=method, url=url, runtime=runtime)
    if exc.status_code is not None and exc.status_code >= 400 and exc.payload:
        mapped = services.core.parse_error(exc.payload, exc.status_code)
        return services.core.with_error_context(
            mapped,
            path="image-jobs",
            method=method,
            url=url,
        )
    error_code = (
        services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value
        if exc.transient
        else services.infrastructure.EC.BAD_RESPONSE.value
    )
    payload: dict[str, Any] = {
        "path": "image-jobs",
        "method": method,
        "url": url,
        "operation": exc.operation,
    }
    if job_id:
        payload["job_id"] = job_id
    if exc.transient:
        payload["upstream_result_unknown"] = True
    return services.infrastructure.UpstreamError(
        str(exc),
        status_code=exc.status_code or 0,
        error_code=error_code,
        payload=payload,
    )


async def _submit_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    provider_id: str,
    endpoint: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[ImageJobClient, None, ImageJobExecutionHandle]:
    _ = proxy
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    client = services.image_jobs.build_image_job_client(base_url)
    submit_url = services.requests.image_jobs_url(base_url)
    trace_id = context.trace_id
    idempotency_key = image_job_idempotency_key(
        context=context,
        provider_id=provider_id,
        endpoint=endpoint,
    )
    started = services.infrastructure.time.monotonic()
    prepare_attempt = _image_job_dispatch_attempt_hook(
        progress_callback=progress_callback,
        before_attempt=before_attempt,
        runtime=runtime,
    )

    try:
        handle = await client.submit(
            payload,
            upstream_api_key=api_key,
            trace_id=trace_id,
            before_attempt=prepare_attempt,
            idempotency_key=idempotency_key,
        )
    except ImageJobClientError as exc:
        duration_ms = (services.infrastructure.time.monotonic() - started) * 1000.0
        services.core.log_upstream_call(
            endpoint="image_jobs_submit",
            status=exc.status_code or 0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        await client.close()
        raise _map_image_job_client_error(
            exc,
            method="POST",
            url=submit_url,
            runtime=runtime,
        ) from exc
    except BaseException:
        await client.close()
        raise

    duration_ms = (services.infrastructure.time.monotonic() - started) * 1000.0
    services.core.log_upstream_call(
        endpoint="image_jobs_submit",
        status=handle.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=None,
    )
    return (
        client,
        None,
        ImageJobExecutionHandle(
            job_id=handle.job_id,
            provider_id=provider_id,
            endpoint=endpoint,
            base_url=base_url,
            idempotency_key=idempotency_key,
        ),
    )


async def _poll_image_job_once(
    *,
    client: ImageJobClient,
    status_url: str,
    api_key: str,
    job_id: str,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[dict[str, Any], int] | None:
    _ = status_url
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    poll_trace_id = context.trace_id
    poll_started = services.infrastructure.time.monotonic()
    try:
        status = await client.poll(
            ImageJobHandle(job_id=job_id, upstream_api_key=api_key),
            trace_id=poll_trace_id,
        )
    except ImageJobClientError as exc:
        raise _map_image_job_client_error(
            exc,
            method="GET",
            url=status_url,
            job_id=job_id,
            runtime=runtime,
        ) from exc
    poll_duration_ms = (
        services.infrastructure.time.monotonic() - poll_started
    ) * 1000.0
    services.core.log_upstream_call(
        endpoint="image_jobs_poll",
        status=status.status_code if status is not None else 0,
        duration_ms=poll_duration_ms,
        trace_id=poll_trace_id,
        response_headers=None,
    )
    if status is None:
        return None
    return status.payload, status.status_code


async def _wait_image_job(
    *,
    client: ImageJobClient,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy_url: str | None,
    execution: ImageJobExecutionHandle,
    progress_callback: ImageProgressCallback | None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str | None]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    deadline = (
        services.infrastructure.time.monotonic() + services.core.IMAGE_JOB_TIMEOUT_S
    )
    status_url = services.requests.image_job_status_url(base_url, execution.job_id)
    while services.infrastructure.time.monotonic() < deadline:
        await services.infrastructure.asyncio.sleep(
            services.core.IMAGE_JOB_POLL_INTERVAL_S
        )
        try:
            polled = await _poll_image_job_once(
                client=client,
                status_url=status_url,
                api_key=api_key,
                job_id=execution.job_id,
                request_context=context,
                runtime=runtime,
            )
        except Exception as exc:  # noqa: BLE001
            raise _image_job_recovery_error(
                f"image job poll failed: {exc}",
                execution,
                phase="poll",
                status_code=getattr(exc, "status_code", None),
                cause=exc,
                runtime=runtime,
            ) from exc
        if polled is None:
            continue
        job, status_code = polled
        if job.get("status") in {"queued", "running"}:
            continue
        return await _finish_image_job(
            client=client,
            job=job,
            status_code=status_code,
            payload=payload,
            base_url=base_url,
            proxy_url=proxy_url,
            execution=execution,
            progress_callback=progress_callback,
            request_context=context,
            runtime=runtime,
        )
    raise _image_job_recovery_error(
        "image job timeout",
        execution,
        phase="poll",
        status_code=None,
        runtime=runtime,
    )


async def _submit_and_wait_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    provider_id: str = "unknown",
    endpoint: str | None = None,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str | None]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    endpoint = str(endpoint or payload.get("endpoint") or "unknown")
    client, proxy_url, execution = await _submit_image_job(
        payload=payload,
        base_url=base_url,
        api_key=api_key,
        provider_id=provider_id,
        endpoint=endpoint,
        proxy=proxy,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
        request_context=context,
        runtime=runtime,
    )
    handle = ImageJobHandle(
        job_id=execution.job_id,
        upstream_api_key=api_key,
    )
    try:
        try:
            await _emit_image_job_execution(
                progress_callback,
                execution,
                runtime=runtime,
            )
        except Exception as exc:  # noqa: BLE001
            failure_payload = getattr(exc, "payload", None)
            if (
                isinstance(failure_payload, dict)
                and failure_payload.get("receipt_persist_failed") is True
            ):
                raise
            raise _image_job_recovery_error(
                f"image job accepted but execution persistence failed: {exc}",
                execution,
                phase="accept",
                cause=exc,
                runtime=runtime,
            ) from exc
        await services.transport.emit_image_progress(
            progress_callback,
            "fallback_started",
            source="image_jobs",
            job_id=execution.job_id,
        )
        return await _wait_image_job(
            client=client,
            payload=payload,
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy_url,
            execution=execution,
            progress_callback=progress_callback,
            request_context=context,
            runtime=runtime,
        )
    except (
        asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        cancel_result = await client.cancel(
            handle,
            trace_id=context.trace_id,
        )
        cancelled_execution = _execution_after_cancel(execution, cancel_result)
        await _emit_image_job_execution(
            progress_callback,
            cancelled_execution,
            runtime=runtime,
        )
        raise
    finally:
        await client.close()


async def _image_job_generate_once(
    request: ImageExecutionRequest,
    *,
    api_key_override: str,
    provider_id: str,
    endpoint: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    body = services.image_jobs.image_job_body_base(
        prompt=request.prompt,
        size=request.size,
        n=request.n,
        quality=request.quality,
        output_format=request.output_format,
        output_compression=request.output_compression,
        background=request.background,
        moderation=request.moderation,
    )
    return await services.image_jobs.submit_and_wait_image_job(
        payload=services.image_jobs.image_job_payload(
            request_type="generations",
            endpoint="/v1/images/generations",
            body=body,
        ),
        base_url=base_url_override
        or await services.direct.resolve_image_job_base_url(),
        api_key=api_key_override,
        provider_id=provider_id,
        endpoint=endpoint,
        proxy=proxy_override,
        progress_callback=request.progress_callback,
        before_attempt=before_attempt,
        request_context=request.request_context,
    )


async def _image_job_reference_image_entries(
    images: list[bytes],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[dict[str, str]]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    image_urls = await services.references.resolve_reference_image_urls(
        images,
        base_url=base_url,
        api_key=_image_job_sidecar_token(runtime=runtime),
        user_id=user_id,
        request_context=context,
    )
    return [{"image_url": url} for url in image_urls]


async def _image_job_edit_once(
    request: ImageExecutionRequest,
    *,
    api_key_override: str,
    provider_id: str,
    endpoint: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    image_edit_input_transport: str = "url",
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    if not request.images:
        raise services.infrastructure.UpstreamError(
            "edit action requires at least one reference image",
            error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
            status_code=400,
        )
    sidecar_base_url: str | None = base_url_override
    if sidecar_base_url is None:
        try:
            sidecar_base_url = await services.direct.resolve_image_job_base_url()
        except Exception as exc:  # noqa: BLE001
            services.infrastructure.logger.debug(
                "reference push base_url resolve fallback err=%s", exc
            )
    body = services.image_jobs.image_job_body_base(
        prompt=request.prompt,
        size=request.size,
        n=request.n,
        quality=request.quality,
        output_format=request.output_format,
        output_compression=request.output_compression,
        background=request.background,
        moderation=request.moderation,
    )
    body["images"] = await services.image_jobs.image_job_reference_image_entries(
        request.images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=request.user_id,
        request_context=request.request_context,
    )
    # inpaint mask 透传给 image-job sidecar：mask 仍用 data URL 即可。images[] 先走
    # refs cache / sidecar URL，mask 则保持单次任务内最短路径，避免额外 cache 写放大。
    if request.mask is not None:
        mask_b64 = services.infrastructure.base64.b64encode(request.mask).decode(
            "ascii"
        )
        body["mask"] = {"image_url": f"data:image/png;base64,{mask_b64}"}
    submit_base_url = (
        base_url_override
        or sidecar_base_url
        or await services.direct.resolve_image_job_base_url()
    )
    return await services.image_jobs.submit_and_wait_image_job(
        payload=services.image_jobs.image_job_payload(
            request_type="edits",
            endpoint="/v1/images/edits",
            body=body,
            image_edit_input_transport=image_edit_input_transport,
        ),
        base_url=submit_base_url,
        api_key=api_key_override,
        provider_id=provider_id,
        endpoint=endpoint,
        proxy=proxy_override,
        progress_callback=request.progress_callback,
        before_attempt=before_attempt,
        request_context=request.request_context,
    )


async def _image_job_responses_once(
    request: ImageExecutionRequest,
    *,
    api_key_override: str,
    provider_id: str,
    endpoint: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Submit an image job that points the sidecar at ``/v1/responses``.

    The sidecar will block-wait the SSE stream and extract the final image. We
    pass exactly the same body the direct ``_responses_image_stream`` route
    would build, so prompt-cache prefixes match between the two paths.
    """
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    context = request.request_context
    sidecar_base_url = (
        base_url_override or await services.direct.resolve_image_job_base_url()
    )
    # 先 push reference 到 image-job sidecar 拿短 URL；失败时 image_urls=[] 让 build 走 base64 fallback。
    # /v1/refs 只接收 Lumen→sidecar 服务 token；供应商 Bearer 不参与引用上传。
    image_urls = await services.references.resolve_reference_image_urls(
        request.images,
        base_url=sidecar_base_url,
        api_key=_image_job_sidecar_token(runtime=runtime),
        user_id=request.user_id,
        request_context=context,
    )
    body = services.image_jobs.build_responses_image_body(
        request.with_n(1),
        image_urls=image_urls or None,
    )
    return await services.image_jobs.submit_and_wait_image_job(
        payload=services.image_jobs.image_job_payload(
            request_type="responses",
            endpoint="/v1/responses",
            body=body,
        ),
        base_url=sidecar_base_url,
        api_key=api_key_override,
        provider_id=provider_id,
        endpoint=endpoint,
        proxy=proxy_override,
        progress_callback=request.progress_callback,
        before_attempt=before_attempt,
        request_context=context,
    )


__all__ = [
    "validate_effective_image_job_configuration",
    "_image_job_body_base",
    "_image_job_payload",
    "_build_responses_image_body",
    "_build_image_job_client",
    "_download_image_job_result",
    "_image_job_error",
    "_image_job_sidecar_token",
    "_finish_image_job",
    "_resume_image_job",
    "_resume_image_jobs",
    "_submit_and_wait_image_job",
    "_wait_image_job",
    "_image_job_generate_once",
    "_image_job_reference_image_entries",
    "_image_job_edit_once",
    "_image_job_responses_once",
]
