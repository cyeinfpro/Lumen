"""Asynchronous image-job payload, polling, and download runtime."""

from __future__ import annotations

import importlib
from typing import Any, Awaitable, Callable

import httpx
from lumen_core.providers import ProviderProxyDefinition

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


class _FacadeProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_facade(), name)


_runtime = _FacadeProxy()


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
    return _runtime.upstream_image_requests._image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        policy=_runtime._image_request_policy(),
        hooks=_runtime.upstream_image_requests.ImageJobBodyHooks(
            transparent_matte_upstream_options=_runtime._transparent_matte_upstream_options,
            normalize_image_quality=_runtime._normalize_image_quality,
            add_image_output_options=_runtime._add_image_output_options,
        ),
    )


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
    image_edit_input_transport: str | None = None,
) -> dict[str, Any]:
    return _runtime.upstream_image_requests._image_job_payload(
        request_type=request_type,
        endpoint=endpoint,
        body=body,
        image_edit_input_transport=image_edit_input_transport,
        policy=_runtime._image_request_policy(),
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
    return _runtime.upstream_image_requests._build_responses_image_body(
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
        retry_attempt=_runtime._image_retry_attempt_ctx.get(),
        policy=_runtime._image_request_policy(),
        hooks=_runtime.upstream_image_requests.ResponsesImageBodyHooks(
            normalize_image_quality=_runtime._normalize_image_quality,
            transparent_matte_upstream_options=_runtime._transparent_matte_upstream_options,
            add_image_output_options=_runtime._add_image_output_options,
            parse_size_pixels=_runtime._parse_size_pixels,
            normalize_reference_image=_runtime._normalize_reference_image,
            stable_sort_tools=_runtime._stable_sort_tools,
            apply_retry_cache_busters=_runtime._apply_retry_cache_busters,
            validate_responses_body=_runtime._validate_responses_body,
        ),
    )


def _image_job_error(
    job: dict[str, Any], *, status_code: int = 200
) -> _runtime.UpstreamError:
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
        exc = _runtime._parse_error(upstream_body, status)
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
    return _runtime.UpstreamError(
        message,
        status_code=status,
        error_code=_runtime.EC.UPSTREAM_ERROR.value,
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
    client: httpx.AsyncClient,
    image_url: str,
    proxy_url: str | None,
    allowed_base_url: str | None = None,
) -> bytes:
    _ = client, proxy_url
    return await _runtime._download_result_url_bytes(
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
    headers = _runtime._auth_headers(api_key, trace_id=trace_id)
    payload_idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if payload_idempotency_key:
        digest = _runtime.hashlib.sha256(
            payload_idempotency_key.encode("utf-8")
        ).hexdigest()
        headers.setdefault("Idempotency-Key", f"lumen-image-job-{digest[:32]}")
    else:
        _runtime._attach_image_idempotency_key(
            headers,
            trace_id=trace_id,
            endpoint="image-jobs",
            body=payload,
        )
    return headers


async def _submit_image_job(
    *,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[httpx.AsyncClient, str | None, str]:
    proxy_url = await _runtime.resolve_provider_proxy_url(proxy)
    # image-job traffic targets the configured sidecar/internal origin. BYOK
    # pins are only passed to direct supplier requests, never inherited here.
    client = await (
        _runtime._get_images_client(proxy_url)
        if proxy_url
        else _runtime._get_images_client()
    )
    submit_url = _runtime._image_jobs_url(base_url)
    trace_id = _runtime._generate_trace_id()
    headers = _image_job_submit_headers(
        payload,
        api_key=api_key,
        trace_id=trace_id,
    )
    started = _runtime.time.monotonic()
    try:
        resp = await _runtime._post_with_retry(
            client=client,
            url=submit_url,
            headers=headers,
            json_body=payload,
            max_attempts=3,
            before_attempt=before_attempt,
        )
    except _runtime._RETRY_HTTPX_EXC as exc:
        duration_ms = (_runtime.time.monotonic() - started) * 1000.0
        _runtime._log_upstream_call(
            endpoint="image_jobs_submit",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"image job submit failed: {exc}",
            status_code=0,
            error_code=_runtime.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        ) from exc

    duration_ms = (_runtime.time.monotonic() - started) * 1000.0
    _runtime._log_upstream_call(
        endpoint="image_jobs_submit",
        status=resp.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(resp, "headers", None),
    )
    try:
        submit_payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise _runtime.UpstreamError(
            "image job submit returned invalid JSON",
            status_code=resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        ) from exc
    if resp.status_code >= 400:
        raise _runtime._with_error_context(
            _runtime._parse_error(
                submit_payload if isinstance(submit_payload, dict) else {},
                resp.status_code,
            ),
            path="image-jobs",
            method="POST",
            url=submit_url,
        )
    if not isinstance(submit_payload, dict):
        raise _runtime.UpstreamError(
            "image job submit returned non-object",
            status_code=resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload={"path": "image-jobs", "method": "POST", "url": submit_url},
        )
    job_id = submit_payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise _runtime.UpstreamError(
            "image job submit returned no job_id",
            status_code=resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload=submit_payload,
        )
    return client, proxy_url, job_id


async def _poll_image_job_once(
    *,
    client: httpx.AsyncClient,
    status_url: str,
    api_key: str,
    job_id: str,
) -> tuple[dict[str, Any], int] | None:
    poll_trace_id = _runtime._generate_trace_id()
    poll_started = _runtime.time.monotonic()
    try:
        poll_resp = await client.get(
            status_url,
            headers=_runtime._auth_headers(api_key, trace_id=poll_trace_id),
        )
    except _runtime._RETRY_HTTPX_EXC as exc:
        _runtime.logger.warning(
            "image job poll transient err job=%s err=%r", job_id, exc
        )
        return None
    poll_duration_ms = (_runtime.time.monotonic() - poll_started) * 1000.0
    _runtime._log_upstream_call(
        endpoint="image_jobs_poll",
        status=poll_resp.status_code,
        duration_ms=poll_duration_ms,
        trace_id=poll_trace_id,
        response_headers=getattr(poll_resp, "headers", None),
    )
    if poll_resp.status_code in _runtime._RETRY_STATUS:
        return None
    try:
        job = poll_resp.json()
    except Exception as exc:  # noqa: BLE001
        if poll_resp.status_code >= 500:
            return None
        raise _runtime.UpstreamError(
            "image job poll returned invalid JSON",
            status_code=poll_resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload={"job_id": job_id, "path": "image-jobs", "method": "GET"},
        ) from exc
    if poll_resp.status_code >= 400:
        raise _runtime._with_error_context(
            _runtime._parse_error(
                job if isinstance(job, dict) else {}, poll_resp.status_code
            ),
            path="image-jobs",
            method="GET",
            url=status_url,
        )
    if not isinstance(job, dict):
        raise _runtime.UpstreamError(
            "image job poll returned non-object",
            status_code=poll_resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload={"job_id": job_id, "path": "image-jobs", "method": "GET"},
        )
    return job, poll_resp.status_code


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
    client: httpx.AsyncClient,
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
        raise _runtime._image_job_error(job, status_code=status_code)
    if status != "succeeded":
        raise _runtime.UpstreamError(
            f"image job returned unknown status: {status!r}",
            status_code=status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload=job,
        )
    images = job.get("images")
    first = images[0] if isinstance(images, list) and images else None
    image_url = first.get("url") if isinstance(first, dict) else None
    if not isinstance(first, dict) or not isinstance(image_url, str) or not image_url:
        raise _runtime.UpstreamError(
            "image job succeeded without images[0].url",
            status_code=status_code,
            error_code=_runtime.EC.NO_IMAGE_RETURNED.value,
            payload=job,
        )
    await _runtime._emit_image_progress(
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
    raw = await _runtime._download_image_job_result(
        client=client,
        image_url=image_url,
        proxy_url=proxy_url,
        allowed_base_url=base_url,
    )
    return _runtime.base64.b64encode(raw).decode("ascii"), None


async def _wait_image_job(
    *,
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxy_url: str | None,
    job_id: str,
    progress_callback: ImageProgressCallback | None,
) -> tuple[str, str | None]:
    deadline = _runtime.time.monotonic() + _runtime._IMAGE_JOB_TIMEOUT_S
    status_url = _runtime._image_job_status_url(base_url, job_id)
    while _runtime.time.monotonic() < deadline:
        await _runtime.asyncio.sleep(_runtime._IMAGE_JOB_POLL_INTERVAL_S)
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
    raise _runtime.UpstreamError(
        "image job timeout",
        status_code=None,
        error_code=_runtime.EC.UPSTREAM_TIMEOUT.value,
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
    await _runtime._emit_image_progress(
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
    body = _runtime._image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    return await _runtime._submit_and_wait_image_job(
        payload=_runtime._image_job_payload(
            request_type="generations",
            endpoint="/v1/images/generations",
            body=body,
        ),
        base_url=base_url_override or await _runtime._resolve_image_job_base_url(),
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
    image_urls = await _runtime._resolve_reference_image_urls(
        images,
        base_url=base_url,
        api_key=api_key,
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
            sidecar_base_url = await _runtime._resolve_image_job_base_url()
        except Exception as exc:  # noqa: BLE001
            _runtime.logger.debug(
                "reference push base_url resolve fallback err=%s", exc
            )
    body = _runtime._image_job_body_base(
        prompt=prompt,
        size=size,
        n=n,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    body["images"] = await _runtime._image_job_reference_image_entries(
        images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=user_id,
    )
    # inpaint mask 透传给 image-job sidecar：mask 仍用 data URL 即可。images[] 先走
    # refs cache / sidecar URL，mask 则保持单次任务内最短路径，避免额外 cache 写放大。
    if mask is not None:
        mask_b64 = _runtime.base64.b64encode(mask).decode("ascii")
        body["mask"] = {"image_url": f"data:image/png;base64,{mask_b64}"}
    submit_base_url = (
        base_url_override
        or sidecar_base_url
        or await _runtime._resolve_image_job_base_url()
    )
    return await _runtime._submit_and_wait_image_job(
        payload=_runtime._image_job_payload(
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
    sidecar_base_url = base_url_override or await _runtime._resolve_image_job_base_url()
    # 先 push reference 到 image-job sidecar 拿短 URL；失败时 image_urls=[] 让 build 走 base64 fallback。
    # api_key 用同一个（image-job sidecar /v1/refs 和 /v1/image-jobs 共用 Bearer）。
    image_urls = await _runtime._resolve_reference_image_urls(
        images,
        base_url=sidecar_base_url,
        api_key=api_key_override,
        user_id=user_id,
    )
    body = _runtime._build_responses_image_body(
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
    return await _runtime._submit_and_wait_image_job(
        payload=_runtime._image_job_payload(
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
    "_image_job_error",
    "_download_image_job_result",
    "_submit_and_wait_image_job",
    "_image_job_generate_once",
    "_image_job_reference_image_entries",
    "_image_job_edit_once",
    "_image_job_responses_once",
]
