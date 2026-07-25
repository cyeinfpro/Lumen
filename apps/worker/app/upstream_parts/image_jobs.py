"""Asynchronous image-job payload, polling, and download runtime."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
from lumen_core.providers import ProviderProxyDefinition

from ..provider_runtime.contracts import ImageJobEndpoint
from ..provider_runtime.upstream_services import upstream_services
from ..upstream_clients.image_job_auth import (
    UPSTREAM_AUTH_HEADER,
    image_job_headers,
)
from ..upstream_clients.image_job_client import ImageJobClient, ImageJobClientError
from ..upstream_clients.image_job_models import ImageJobHandle
from .transport import ImageProgressCallback

_UPSTREAM_AUTH_HEADER = UPSTREAM_AUTH_HEADER


async def validate_effective_image_job_configuration() -> None:
    """Fail startup when the effective strict image channel is unusable."""
    services = upstream_services()
    if (
        await services.core.resolve_image_channel()
        != services.core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY
    ):
        return
    services.image_jobs.image_job_sidecar_token()
    await services.direct.resolve_image_job_base_url()


def _image_job_sidecar_token() -> str:
    raw_token = str(
        getattr(
            upstream_services().infrastructure.settings, "image_job_sidecar_token", ""
        )
        or ""
    )
    try:
        return upstream_services().infrastructure.validate_image_job_sidecar_token(
            raw_token
        )
    except ValueError as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"image job configuration unavailable: {exc}",
            status_code=503,
            error_code=upstream_services().infrastructure.EC.SERVICE_UNAVAILABLE.value,
            payload={
                "path": "image-jobs",
                "configuration": "sidecar_auth",
                "reason": "configuration_unavailable",
            },
        ) from None


def _image_job_headers(
    *,
    api_key: str,
    trace_id: str,
) -> dict[str, str]:
    return image_job_headers(
        service_token=_image_job_sidecar_token(),
        upstream_api_key=api_key,
        trace_id=trace_id,
    )


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
) -> dict[str, Any]:
    return upstream_services().requests.image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        policy=upstream_services().core.image_request_policy(),
        hooks=upstream_services().infrastructure.upstream_image_requests.ImageJobBodyHooks(
            transparent_matte_upstream_options=upstream_services().core.transparent_matte_upstream_options,
            normalize_image_quality=upstream_services().core.normalize_image_quality,
            add_image_output_options=upstream_services().core.add_image_output_options,
        ),
    )


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
    image_edit_input_transport: str | None = None,
) -> dict[str, Any]:
    return upstream_services().requests.image_job_payload(
        request_type=request_type,
        endpoint=endpoint,
        body=body,
        image_edit_input_transport=image_edit_input_transport,
        policy=upstream_services().core.image_request_policy(),
    )


def _build_responses_image_body(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON body posted to ``/v1/responses`` for image generation.

    image_urls vs images：
    - image_urls 优先（http URL 或 data URL，已是上游 image_url 字段值）：调用方先把 reference
      push 到 image-job sidecar 拿短 URL，body 缩到几百字节。这是新优化路径。
    - images（bytes）作为 fallback：旧路径，base64 内联到 body（4-7MB），用于无 sidecar 测试环境。
    - 两者都不传 + action=edit：edit 没参考图，上游会按文生图处理（语义降级）。

    Extracted from ``_responses_image_stream`` so the image-job sidecar path can
    reuse the exact same request shape — keeping prompt-cache prefixes aligned
    between the direct-stream route and the async sidecar route.
    """
    return upstream_services().requests.build_responses_image_body(
        action=action,
        prompt=prompt,
        size=size,
        images=images,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
        image_urls=image_urls,
        retry_attempt=upstream_services().core.image_retry_attempt_ctx.get(),
        policy=upstream_services().core.image_request_policy(),
        hooks=upstream_services().infrastructure.upstream_image_requests.ResponsesImageBodyHooks(
            normalize_image_quality=upstream_services().core.normalize_image_quality,
            transparent_matte_upstream_options=upstream_services().core.transparent_matte_upstream_options,
            add_image_output_options=upstream_services().core.add_image_output_options,
            parse_size_pixels=upstream_services().requests.parse_size_pixels,
            normalize_reference_image=upstream_services().references.normalize_reference_image,
            stable_sort_tools=upstream_services().core.stable_sort_tools,
            apply_retry_cache_busters=upstream_services().core.apply_retry_cache_busters,
            validate_responses_body=upstream_services().core.validate_responses_body,
        ),
    )


def _image_job_error(
    job: dict[str, Any], *, status_code: int = 200
) -> upstream_services().infrastructure.UpstreamError:
    upstream_status = job.get("upstream_status")
    try:
        status = int(upstream_status) if upstream_status is not None else status_code
    except (TypeError, ValueError):
        status = status_code
    upstream_body = job.get("upstream_body")
    # Sidecar tags every failed job with an error_class describing whether the
    # failure was a transport problem, an upstream HTTP error, a missing image,
    # etc. Lumen's failover layer reads this to decide whether to switch the
    # endpoint kind on the same provider or jump straight to the next provider.
    error_class = job.get("error_class")
    if isinstance(upstream_body, dict):
        exc = upstream_services().core.parse_error(upstream_body, status)
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
    return upstream_services().infrastructure.UpstreamError(
        message,
        status_code=status,
        error_code=upstream_services().infrastructure.EC.UPSTREAM_ERROR.value,
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
) -> bytes:
    _ = client, proxy_url
    return await upstream_services().direct.download_result_url_bytes(
        image_url,
        path="image-jobs/result",
        log_endpoint="image_jobs_download",
        description="image job result download",
        allowed_base_url=allowed_base_url,
    )


def _image_job_submit_headers(
    payload: dict[str, Any],
    *,
    api_key: str,
    trace_id: str,
) -> dict[str, str]:
    headers = _image_job_headers(api_key=api_key, trace_id=trace_id)
    payload_idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if payload_idempotency_key:
        digest = (
            upstream_services()
            .infrastructure.hashlib.sha256(payload_idempotency_key.encode("utf-8"))
            .hexdigest()
        )
        headers.setdefault("Idempotency-Key", f"lumen-image-job-{digest[:32]}")
    else:
        upstream_services().core.attach_image_idempotency_key(
            headers,
            trace_id=trace_id,
            endpoint="image-jobs",
            body=payload,
        )
    return headers


def _build_image_job_client(
    base_url: str,
    *,
    service_token: str | None = None,
    read_timeout_s: float | None = None,
) -> ImageJobClient:
    settings = upstream_services().infrastructure.settings
    return ImageJobClient(
        ImageJobEndpoint(
            base_url=upstream_services().requests.validate_image_job_base_url(base_url),
            service_token=service_token or _image_job_sidecar_token(),
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
        post_with_retry=upstream_services().core.post_with_retry,
    )


def _map_image_job_client_error(
    exc: ImageJobClientError,
    *,
    method: str,
    url: str,
    job_id: str | None = None,
) -> Exception:
    if exc.status_code is not None and exc.status_code >= 400 and exc.payload:
        mapped = upstream_services().core.parse_error(exc.payload, exc.status_code)
        return upstream_services().core.with_error_context(
            mapped,
            path="image-jobs",
            method=method,
            url=url,
        )
    error_code = (
        upstream_services().infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value
        if exc.transient
        else upstream_services().infrastructure.EC.BAD_RESPONSE.value
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
    return upstream_services().infrastructure.UpstreamError(
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
    proxy: ProviderProxyDefinition | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[ImageJobClient, None, str]:
    _ = proxy
    client = upstream_services().image_jobs.build_image_job_client(base_url)
    submit_url = upstream_services().requests.image_jobs_url(base_url)
    trace_id = upstream_services().core.generate_trace_id()
    headers = _image_job_submit_headers(
        payload,
        api_key=api_key,
        trace_id=trace_id,
    )
    started = upstream_services().infrastructure.time.monotonic()
    try:
        handle = await client.submit(
            payload,
            upstream_api_key=api_key,
            trace_id=trace_id,
            before_attempt=before_attempt,
            idempotency_key=headers.get("Idempotency-Key"),
        )
    except ImageJobClientError as exc:
        duration_ms = (
            upstream_services().infrastructure.time.monotonic() - started
        ) * 1000.0
        upstream_services().core.log_upstream_call(
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
        ) from exc

    duration_ms = (
        upstream_services().infrastructure.time.monotonic() - started
    ) * 1000.0
    upstream_services().core.log_upstream_call(
        endpoint="image_jobs_submit",
        status=handle.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=None,
    )
    return client, None, handle.job_id


async def _poll_image_job_once(
    *,
    client: ImageJobClient,
    status_url: str,
    api_key: str,
    job_id: str,
) -> tuple[dict[str, Any], int] | None:
    _ = status_url
    poll_trace_id = upstream_services().core.generate_trace_id()
    poll_started = upstream_services().infrastructure.time.monotonic()
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
        ) from exc
    poll_duration_ms = (
        upstream_services().infrastructure.time.monotonic() - poll_started
    ) * 1000.0
    upstream_services().core.log_upstream_call(
        endpoint="image_jobs_poll",
        status=status.status_code if status is not None else 0,
        duration_ms=poll_duration_ms,
        trace_id=poll_trace_id,
        response_headers=None,
    )
    if status is None:
        return None
    return status.payload, status.status_code


def _image_job_result_metadata(
    *,
    job: dict[str, Any],
    first: dict[str, Any],
    payload: dict[str, Any],
    job_id: str,
    image_url: str,
) -> dict[str, Any]:
    image_meta: dict[str, Any] = {
        "image_job_url": image_url,
        "job_id": job_id,
        "endpoint_used": job.get("endpoint_used") or payload.get("endpoint"),
    }
    for key in ("expires_at", "bytes", "width", "height", "format"):
        value = first.get(key)
        if value is not None:
            image_meta[key] = value
    return image_meta


async def _finish_image_job(
    *,
    client: ImageJobClient,
    job: dict[str, Any],
    status_code: int,
    payload: dict[str, Any],
    base_url: str,
    proxy_url: str | None,
    job_id: str,
    progress_callback: ImageProgressCallback | None,
) -> tuple[str, str | None]:
    status = job.get("status")
    if status == "failed":
        raise upstream_services().image_jobs.image_job_error(
            job, status_code=status_code
        )
    if status != "succeeded":
        raise upstream_services().infrastructure.UpstreamError(
            f"image job returned unknown status: {status!r}",
            status_code=status_code,
            error_code=upstream_services().infrastructure.EC.BAD_RESPONSE.value,
            payload=job,
        )
    images = job.get("images")
    first = images[0] if isinstance(images, list) and images else None
    image_url = first.get("url") if isinstance(first, dict) else None
    if not isinstance(first, dict) or not isinstance(image_url, str) or not image_url:
        raise upstream_services().infrastructure.UpstreamError(
            "image job succeeded without images[0].url",
            status_code=status_code,
            error_code=upstream_services().infrastructure.EC.NO_IMAGE_RETURNED.value,
            payload=job,
        )
    await upstream_services().transport.emit_image_progress(
        progress_callback,
        "image_job_image",
        **_image_job_result_metadata(
            job=job,
            first=first,
            payload=payload,
            job_id=job_id,
            image_url=image_url,
        ),
    )
    raw = await upstream_services().image_jobs.download_image_job_result(
        client=client,
        image_url=image_url,
        proxy_url=proxy_url,
        allowed_base_url=base_url,
    )
    return upstream_services().infrastructure.base64.b64encode(raw).decode(
        "ascii"
    ), None


async def _wait_image_job(
    *,
    client: ImageJobClient,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy_url: str | None,
    job_id: str,
    progress_callback: ImageProgressCallback | None,
) -> tuple[str, str | None]:
    deadline = (
        upstream_services().infrastructure.time.monotonic()
        + upstream_services().core.IMAGE_JOB_TIMEOUT_S
    )
    status_url = upstream_services().requests.image_job_status_url(base_url, job_id)
    while upstream_services().infrastructure.time.monotonic() < deadline:
        await upstream_services().infrastructure.asyncio.sleep(
            upstream_services().core.IMAGE_JOB_POLL_INTERVAL_S
        )
        polled = await _poll_image_job_once(
            client=client,
            status_url=status_url,
            api_key=api_key,
            job_id=job_id,
        )
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
            job_id=job_id,
            progress_callback=progress_callback,
        )
    raise upstream_services().infrastructure.UpstreamError(
        "image job timeout",
        status_code=None,
        error_code=upstream_services().infrastructure.EC.UPSTREAM_TIMEOUT.value,
        payload={"path": "image-jobs", "method": "GET", "job_id": job_id},
    )


async def _submit_and_wait_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None,
    progress_callback: ImageProgressCallback | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    client, proxy_url, job_id = await _submit_image_job(
        payload=payload,
        base_url=base_url,
        api_key=api_key,
        proxy=proxy,
        before_attempt=before_attempt,
    )
    handle = ImageJobHandle(job_id=job_id, upstream_api_key=api_key)
    try:
        await upstream_services().transport.emit_image_progress(
            progress_callback,
            "fallback_started",
            source="image_jobs",
            job_id=job_id,
        )
        return await _wait_image_job(
            client=client,
            payload=payload,
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy_url,
            job_id=job_id,
            progress_callback=progress_callback,
        )
    except (
        asyncio.CancelledError,
        upstream_services().infrastructure.UpstreamCancelled,
    ):
        await client.cancel(
            handle,
            trace_id=upstream_services().core.generate_trace_id(),
        )
        raise
    finally:
        await client.close()


async def _image_job_generate_once(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    progress_callback: ImageProgressCallback | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    body = upstream_services().image_jobs.image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    return await upstream_services().image_jobs.submit_and_wait_image_job(
        payload=upstream_services().image_jobs.image_job_payload(
            request_type="generations",
            endpoint="/v1/images/generations",
            body=body,
        ),
        base_url=base_url_override
        or await upstream_services().direct.resolve_image_job_base_url(),
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


async def _image_job_reference_image_entries(
    images: list[bytes],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, str]]:
    image_urls = await upstream_services().references.resolve_reference_image_urls(
        images,
        base_url=base_url,
        api_key=_image_job_sidecar_token(),
        user_id=user_id,
    )
    return [{"image_url": url} for url in image_urls]


async def _image_job_edit_once(
    *,
    prompt: str,
    size: str,
    images: list[bytes],
    mask: bytes | None = None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    image_edit_input_transport: str = "url",
    progress_callback: ImageProgressCallback | None = None,
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    sidecar_base_url: str | None = base_url_override
    if sidecar_base_url is None:
        try:
            sidecar_base_url = (
                await upstream_services().direct.resolve_image_job_base_url()
            )
        except Exception as exc:  # noqa: BLE001
            upstream_services().infrastructure.logger.debug(
                "reference push base_url resolve fallback err=%s", exc
            )
    body = upstream_services().image_jobs.image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    body[
        "images"
    ] = await upstream_services().image_jobs.image_job_reference_image_entries(
        images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=user_id,
    )
    # inpaint mask 透传给 image-job sidecar：mask 仍用 data URL 即可。images[] 先走
    # refs cache / sidecar URL，mask 则保持单次任务内最短路径，避免额外 cache 写放大。
    if mask is not None:
        mask_b64 = (
            upstream_services().infrastructure.base64.b64encode(mask).decode("ascii")
        )
        body["mask"] = {"image_url": f"data:image/png;base64,{mask_b64}"}
    submit_base_url = (
        base_url_override
        or sidecar_base_url
        or await upstream_services().direct.resolve_image_job_base_url()
    )
    return await upstream_services().image_jobs.submit_and_wait_image_job(
        payload=upstream_services().image_jobs.image_job_payload(
            request_type="edits",
            endpoint="/v1/images/edits",
            body=body,
            image_edit_input_transport=image_edit_input_transport,
        ),
        base_url=submit_base_url,
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


async def _image_job_responses_once(
    *,
    action: str,
    prompt: str,
    size: str,
    images: list[bytes] | None,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    model: str | None,
    api_key_override: str,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    progress_callback: ImageProgressCallback | None = None,
    user_id: str | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Submit an image job that points the sidecar at ``/v1/responses``.

    The sidecar will block-wait the SSE stream and extract the final image. We
    pass exactly the same body the direct ``_responses_image_stream`` route
    would build, so prompt-cache prefixes match between the two paths.
    """
    _ = n  # /v1/responses + image_generation tool returns a single image.
    sidecar_base_url = (
        base_url_override
        or await upstream_services().direct.resolve_image_job_base_url()
    )
    # 先 push reference 到 image-job sidecar 拿短 URL；失败时 image_urls=[] 让 build 走 base64 fallback。
    # /v1/refs 只接收 Lumen→sidecar 服务 token；供应商 Bearer 不参与引用上传。
    image_urls = await upstream_services().references.resolve_reference_image_urls(
        images,
        base_url=sidecar_base_url,
        api_key=_image_job_sidecar_token(),
        user_id=user_id,
    )
    body = upstream_services().image_jobs.build_responses_image_body(
        action=action,
        prompt=prompt,
        size=size,
        images=images,
        image_urls=image_urls or None,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
    )
    return await upstream_services().image_jobs.submit_and_wait_image_job(
        payload=upstream_services().image_jobs.image_job_payload(
            request_type="responses",
            endpoint="/v1/responses",
            body=body,
        ),
        base_url=sidecar_base_url,
        api_key=api_key_override,
        proxy=proxy_override,
        progress_callback=progress_callback,
        before_attempt=before_attempt,
    )


__all__ = [
    "_image_job_body_base",
    "_image_job_payload",
    "_build_responses_image_body",
    "_build_image_job_client",
    "_download_image_job_result",
    "_image_job_error",
    "_image_job_sidecar_token",
    "_submit_and_wait_image_job",
    "_image_job_generate_once",
    "_image_job_reference_image_entries",
    "_image_job_edit_once",
    "_image_job_responses_once",
]
