"""Direct Images API request execution extracted from app.upstream."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, NoReturn

import httpx
from lumen_core.providers import ProviderProxyDefinition
from lumen_core.upstream_billing import (
    IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES,
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
)

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .. import image_artifacts
from .delivery_evidence import transport_error_proves_undelivered
from .direct_generation_responses import (
    DirectGenerationStreamCall,
    complete_direct_generation_response as _complete_direct_generation_response,
    direct_image_response_result_unknown_error as _direct_image_response_result_unknown_error,
    stream_direct_generation_response,
)
from .image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
    ensure_image_request_context,
)
from .generated_payload import (
    GeneratedPayload,
    StagedImageFile,
)


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _context_services(context: ImageRequestContext) -> UpstreamServices:
    return _runtime_services(context.upstream_runtime)


async def _download_result_url_bytes(
    image_url: str,
    *,
    path: str,
    log_endpoint: str,
    description: str,
    allowed_base_url: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> bytes:
    context = ensure_image_request_context(request_context)
    services = _runtime_services(runtime or context.upstream_runtime)
    started = services.infrastructure.time.monotonic()
    trace_id = context.trace_id
    try:
        response = await services.infrastructure.download_public_http_url(
            image_url,
            max_bytes=services.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            max_redirects=5,
            allow_http=True,
            allowed_private_origins=((allowed_base_url,) if allowed_base_url else ()),
            dns_timeout_s=2.0,
            timeout=services.infrastructure.httpx.Timeout(
                connect=services.infrastructure.settings.upstream_connect_timeout_s,
                read=services.infrastructure.settings.upstream_read_timeout_s,
                write=services.infrastructure.settings.upstream_write_timeout_s,
                pool=services.infrastructure.settings.upstream_connect_timeout_s,
            ),
            headers={"User-Agent": "lumen-worker"},
        )
    except services.infrastructure.PublicHttpBodyTooLarge as exc:
        services.core.log_upstream_call(
            endpoint=log_endpoint,
            status=exc.status_code or 0,
            duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise services.infrastructure.UpstreamError(
            f"{description} exceeded max bytes",
            status_code=exc.status_code,
            error_code=services.infrastructure.EC.STREAM_TOO_LARGE.value,
            payload={
                "url": image_url,
                "path": path,
                "method": "GET",
                "bytes": exc.received_bytes,
                "max_bytes": exc.max_bytes,
            },
        ) from exc
    except ValueError as exc:
        services.core.log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise services.infrastructure.UpstreamError(
            f"unsafe image result URL: {exc}",
            status_code=400,
            error_code=services.infrastructure.EC.INVALID_VALUE.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc
    except (services.infrastructure.httpx.HTTPError, OSError) as exc:
        services.core.log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise services.infrastructure.UpstreamError(
            f"{description} failed: {exc}",
            status_code=0,
            error_code=services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc

    services.core.log_upstream_call(
        endpoint=log_endpoint,
        status=response.status_code,
        duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
        trace_id=trace_id,
        response_headers=response.headers,
    )
    if not 200 <= response.status_code < 300:
        raise services.infrastructure.UpstreamError(
            f"{description} http {response.status_code}",
            status_code=response.status_code,
            error_code=services.infrastructure.EC.UPSTREAM_ERROR.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": path,
                "method": "GET",
            },
        )
    if not response.body:
        raise services.infrastructure.UpstreamError(
            f"{description} returned empty body",
            status_code=response.status_code,
            error_code=services.infrastructure.EC.NO_IMAGE_RETURNED.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": path,
                "method": "GET",
            },
        )
    return response.body


async def _fetch_image_url_as_bytes(
    image_url: str,
    *,
    proxy_url: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> GeneratedPayload:
    """下载 images API 在 data[].url 里返回的图片，转成原始字节。

    OpenAI 协议合法的两种响应形态之一：当 response_format=url（旧默认 / 部分
    第三方网关行为）时，图片以 CDN 链接返回而非 b64_json。下载使用逐跳校验和
    DNS-pinned 直连；不复用 provider client，避免代理或二次 DNS 解析绕过 SSRF
    边界。
    """
    _ = proxy_url
    context = ensure_image_request_context(request_context)
    services = _runtime_services(runtime or context.upstream_runtime)
    fd, raw_path = tempfile.mkstemp(
        prefix="lumen-generated-",
        suffix=".part",
    )
    os.close(fd)
    destination = Path(raw_path)
    started = services.infrastructure.time.monotonic()
    try:
        response = await services.infrastructure.download_public_http_url_to_file(
            image_url,
            destination=destination,
            max_bytes=services.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            max_redirects=5,
            allow_http=True,
            dns_timeout_s=2.0,
            timeout=services.infrastructure.httpx.Timeout(
                connect=services.infrastructure.settings.upstream_connect_timeout_s,
                read=services.infrastructure.settings.upstream_read_timeout_s,
                write=services.infrastructure.settings.upstream_write_timeout_s,
                pool=services.infrastructure.settings.upstream_connect_timeout_s,
            ),
            headers={"User-Agent": "lumen-worker"},
        )
    except services.infrastructure.PublicHttpBodyTooLarge as exc:
        destination.unlink(missing_ok=True)
        services.core.log_upstream_call(
            endpoint="image_url_download",
            status=exc.status_code or 0,
            duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
            trace_id=context.trace_id,
            response_headers=None,
        )
        raise services.infrastructure.UpstreamError(
            "image url download exceeded max bytes",
            status_code=exc.status_code,
            error_code=services.infrastructure.EC.STREAM_TOO_LARGE.value,
            payload={
                "url": image_url,
                "path": "images/result",
                "method": "GET",
                "bytes": exc.received_bytes,
                "max_bytes": exc.max_bytes,
            },
        ) from exc
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise services.infrastructure.UpstreamError(
            f"unsafe image result URL: {exc}",
            status_code=400,
            error_code=services.infrastructure.EC.INVALID_VALUE.value,
            payload={"url": image_url, "path": "images/result", "method": "GET"},
        ) from exc
    except (services.infrastructure.httpx.HTTPError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise services.infrastructure.UpstreamError(
            f"image url download failed: {exc}",
            status_code=0,
            error_code=services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"url": image_url, "path": "images/result", "method": "GET"},
        ) from exc

    services.core.log_upstream_call(
        endpoint="image_url_download",
        status=response.status_code,
        duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
        trace_id=context.trace_id,
        response_headers=response.headers,
    )
    if not 200 <= response.status_code < 300:
        if response.status_code == 404 and response.size > 0:
            try:
                image_artifacts.validate_generated_image_file_sync(
                    response.path,
                    expected_size=response.size,
                )
            except (OSError, ValueError):
                pass
            else:
                services.infrastructure.logger.warning(
                    "image result accepted soft status status=%s bytes=%s trace_id=%s",
                    response.status_code,
                    response.size,
                    context.trace_id,
                )
                return StagedImageFile(
                    path=response.path,
                    size=response.size,
                    sha256=response.sha256,
                    owned=True,
                )
        destination.unlink(missing_ok=True)
        raise services.infrastructure.UpstreamError(
            f"image url download http {response.status_code}",
            status_code=response.status_code,
            # The Images POST already returned a response. A failed result URL
            # is therefore terminal for this dispatch and must not trigger a
            # second billable generation through the Responses fallback.
            error_code=services.infrastructure.EC.NO_IMAGE_RETURNED.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": "images/result",
                "method": "GET",
            },
        )
    if response.size <= 0:
        destination.unlink(missing_ok=True)
        raise services.infrastructure.UpstreamError(
            "image url download returned empty body",
            status_code=response.status_code,
            error_code=services.infrastructure.EC.NO_IMAGE_RETURNED.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": "images/result",
                "method": "GET",
            },
        )
    return StagedImageFile(
        path=response.path,
        size=response.size,
        sha256=response.sha256,
        owned=True,
    )


async def _resolve_image_job_base_url(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = _runtime_services(runtime)
    try:
        raw = await services.infrastructure.resolve("image.job_base_url")
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "image job base URL setting fallback err=%s", exc
        )
        raw = None
    return services.requests.validate_image_job_base_url(
        raw or services.core.DEFAULT_IMAGE_JOB_BASE_URL
    )


def _minimum_image_read_timeout(
    size: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> float:
    services = _runtime_services(runtime)
    pixels = services.requests.parse_size_pixels(size)
    if pixels is not None and pixels > services.core.IMAGE_4K_PIXELS:
        return services.core.IMAGE_READ_TIMEOUT_4K_S
    return services.core.IMAGE_READ_TIMEOUT_MIN_S


def _select_image_read_timeout(
    size: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> float:
    """Select the image read timeout from pixel tier and runtime settings."""
    services = _runtime_services(runtime)
    return max(
        services.infrastructure.settings.upstream_read_timeout_s,
        _minimum_image_read_timeout(size, runtime=runtime),
    )


async def _image_request_timeout(
    size: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[httpx.Timeout, float]:
    services = _runtime_services(runtime)
    timeout_config = await services.lifecycle.resolve_timeout_config()
    read_timeout_s = max(
        timeout_config.read,
        _minimum_image_read_timeout(size, runtime=runtime),
    )
    return timeout_config.to_httpx(read=read_timeout_s), read_timeout_s


def _direct_image_result_unknown_error(
    exc: BaseException,
    *,
    path: str,
    method: str,
    url: str,
    trace_id: str,
    timeout_s: float | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    detail = (
        f"timed out after {timeout_s:.0f}s"
        if timeout_s is not None
        else f"failed after dispatch ({type(exc).__name__})"
    )
    payload: dict[str, Any] = {
        "path": path,
        "method": method,
        "url": url,
        "x_trace_id": trace_id,
        "upstream_result_unknown": True,
        "exception": type(exc).__name__,
    }
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s
    return services.infrastructure.UpstreamError(
        (
            f"{path} {detail}; upstream result is unknown. The request may "
            "already have been accepted, so it was not retried automatically."
        ),
        status_code=0,
        error_code=services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        payload=payload,
    )


def _direct_image_undelivered_error(
    exc: BaseException,
    *,
    path: str,
    method: str,
    url: str,
    trace_id: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    reason = UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    error = services.infrastructure.UpstreamError(
        f"{path} could not connect before the request was delivered: {exc}",
        status_code=0,
        error_code=services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
        payload={
            "path": path,
            "method": method,
            "url": url,
            "x_trace_id": trace_id,
            "receipt_reason": reason,
            "upstream_dispatch_delivery": reason,
            "exception": type(exc).__name__,
        },
    )
    setattr(error, "upstream_receipt_reason", reason)
    return error


def _raise_direct_request_failure(
    exc: BaseException,
    *,
    path: str,
    log_endpoint: str,
    description: str,
    url: str,
    trace_id: str,
    started: float,
    timeout_s: float | None,
    runtime: ImageUpstreamRuntime | None,
) -> NoReturn:
    services = _runtime_services(runtime)
    services.core.log_upstream_call(
        endpoint=log_endpoint,
        status=0,
        duration_ms=(services.infrastructure.time.monotonic() - started) * 1000.0,
        trace_id=trace_id,
        response_headers=None,
    )
    if transport_error_proves_undelivered(exc):
        raise _direct_image_undelivered_error(
            exc,
            path=path,
            method="POST",
            url=url,
            trace_id=trace_id,
            runtime=runtime,
        ) from exc
    raise _direct_image_result_unknown_error(
        exc,
        path=path,
        method="POST",
        url=url,
        trace_id=trace_id,
        timeout_s=timeout_s,
        runtime=runtime,
    ) from exc


def _is_direct_image_result_unknown(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """True 表示这次失败发生在上游 2xx 之后（上游可能已经计费）。

    命中即禁止自动换 provider / 重试，否则一次用户请求会产生第二笔上游成本。

    码集直接取自 ``IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES``，**不再本地复制一份**：
    这里原本硬编码了与该常量重复的清单，两处各自演进就会漂移成「计费侧认为上游
    已扣费所以照结，重试侧却以为可以安全重试」——用户被扣两次。同一个判断只允许
    有一个事实来源。
    """
    services = _runtime_services(runtime)
    return (
        isinstance(exc, services.infrastructure.UpstreamError)
        and (exc.error_code or "") in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES
    )


async def _direct_generate_image_once(
    request: ImageExecutionRequest,
    *,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
    streaming_override: bool = False,
) -> list[tuple[str, str | None]]:
    """Text-to-image via direct `/v1/images/generations` using gpt-image-2."""
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    prompt = request.prompt
    size = request.size
    n = request.n
    quality = request.quality
    output_format = request.output_format
    output_compression = request.output_compression
    background = request.background
    moderation = request.moderation
    context = ensure_image_request_context(request.request_context)
    proxy_url = await services.infrastructure.resolve_provider_proxy_url(proxy_override)
    url = services.requests.image_generations_url(base_url_override)
    pinned_target = (
        None
        if proxy_url
        else services.requests.validated_byok_target_for_request(
            pinned_target_override, url
        )
    )
    if proxy_url:
        client = await services.lifecycle.get_images_client(proxy_url)
    elif pinned_target is not None:
        client = await services.lifecycle.get_images_client(pinned_target=pinned_target)
    else:
        client = await services.lifecycle.get_images_client()
    dispatch_ready_emitted = False
    http_attempts = 0

    async def prepare_attempt(attempt: int) -> None:
        nonlocal dispatch_ready_emitted, http_attempts
        if before_attempt is not None:
            await before_attempt(attempt)
        http_attempts = max(http_attempts, attempt)
        if not dispatch_ready_emitted:
            await services.transport.emit_image_progress(
                request.progress_callback,
                "dispatch_ready",
            )
            dispatch_ready_emitted = True

    # Model 显式 pin：UPSTREAM_MODEL 来自 lumen_core.constants（lumen-core wheel 里固化）。
    # 加 runtime assert 防止未来改动把 model 字段隐式置空 / fallback 到上游默认。
    assert services.infrastructure.UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        services.core.transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    body: dict[str, Any] = {
        "model": services.infrastructure.UPSTREAM_MODEL,
        "prompt": prompt_for_upstream,
        "size": size,
        "n": n,
        "quality": services.core.normalize_image_quality(quality),
    }
    services.core.add_image_output_options(
        body,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    if streaming_override:
        body["stream"] = True
        body["partial_images"] = 0
        body["response_format"] = "b64_json"
    trace_id = context.trace_id
    headers = services.core.auth_headers(api_key_override, trace_id=trace_id)
    services.core.attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/generations",
        body=body,
    )
    request_timeout, read_timeout_s = await _image_request_timeout(
        size,
        runtime=runtime,
    )
    started = services.infrastructure.time.monotonic()
    try:
        async with asyncio.timeout(read_timeout_s):
            if streaming_override:
                return await stream_direct_generation_response(
                    DirectGenerationStreamCall(
                        services=services,
                        request=request,
                        body=body,
                        headers=headers,
                        url=url,
                        trace_id=trace_id,
                        context=context,
                        read_timeout_s=read_timeout_s,
                        proxy_url=proxy_url,
                        pinned_target=pinned_target,
                        prepare_attempt=prepare_attempt,
                    )
                )
            resp = await services.core.post_with_retry(
                client=client,
                url=url,
                headers=headers,
                json_body=body,
                timeout=request_timeout,
                retry_httpx_exceptions=False,
                retry_status_codes=True,
                max_attempts=2,
                before_attempt=prepare_attempt,
            )
    except services.infrastructure.httpx.TimeoutException as exc:
        _raise_direct_request_failure(
            exc,
            path="images/generations",
            log_endpoint="images_generations",
            description="direct image request",
            url=url,
            trace_id=trace_id,
            started=started,
            timeout_s=read_timeout_s,
            runtime=runtime,
        )
    except TimeoutError as exc:
        _raise_direct_request_failure(
            exc,
            path="images/generations",
            log_endpoint="images_generations",
            description="direct image request",
            url=url,
            trace_id=trace_id,
            started=started,
            timeout_s=read_timeout_s,
            runtime=runtime,
        )
    except services.core.RETRY_HTTPX_EXC as exc:
        _raise_direct_request_failure(
            exc,
            path="images/generations",
            log_endpoint="images_generations",
            description="direct image request",
            url=url,
            trace_id=trace_id,
            started=started,
            timeout_s=None,
            runtime=runtime,
        )
    return await _complete_direct_generation_response(
        services=services,
        request=request,
        response=resp,
        started=started,
        trace_id=trace_id,
        url=url,
        context=context,
        proxy_url=proxy_url,
        http_attempts=http_attempts,
    )


def _wrap_inpaint_prompt(
    user_intent: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    """局部 inpaint 的 prompt 包装。

    OpenAI /v1/images/edits + mask 字段必须用 invariant 模板才能正确 inpaint：
    否则 mask 区会被填黑、prompt 内容画到别处。本地 spike 已验证（mean diff 1.9）。

    前缀（"Inside the masked region,"）和后缀（preserve / do not add / blend 三条）
    都稳定，user_intent 夹在中间——这样 prompt cache prefix 在多次 retry 间保持稳定。
    只在 mask 不为空时调用；mask 为空时直接发原始 prompt（保持 i2i 行为不变）。

    第四行 "Blend ..." 让模型在 remove / replace 类指令下知道用周围像素自然过渡填充
    （否则 "Inside the masked region, remove the apple." 模型常困惑该填什么 → 填黑/灰色）。
    """
    services = _runtime_services(runtime)
    return services.requests.wrap_inpaint_prompt(user_intent)


def _raise_if_invalid_multipart_json(
    payload: Any,
    *,
    status: int,
    url: str,
    trace_id: str,
    runtime: ImageUpstreamRuntime | None,
) -> None:
    if not (
        200 <= status < 300
        and isinstance(payload, dict)
        and isinstance(payload.get("raw"), str)
    ):
        return
    decode_error = ValueError("curl multipart response was not valid JSON")
    raise _direct_image_response_result_unknown_error(
        decode_error,
        path="images/edits",
        method="POST",
        url=url,
        trace_id=trace_id,
        status_code=status,
        runtime=runtime,
    ) from decode_error


async def _direct_edit_image_once(
    request: ImageExecutionRequest,
    *,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
) -> list[tuple[str, str | None]]:
    """Image-to-image via direct `/v1/images/edits` (multipart) using gpt-image-2.

    image2 模式下 i2i 的单次调用。多个 ref 图通过 multipart 字段名 `image[]` 上传，
    与上游 OpenAI /v1/images/edits 协议一致。复用 `_curl_post_multipart`（见
    "图生图 multipart 走 curl 子进程" 那段注释，httpx 的 multipart 在某些上游网关下
    会持续 502，curl 反而能 200）。

    mask 不为空时把 PNG 字节作为单字段名 `mask`（不是 `mask[]`）一并发送，触发上游
    inpaint 路径（圆外像素级保留，已 spike 验证）。mask 为 None 时不带这个字段，
    走纯图生图路径，保持现有 i2i 行为。
    """
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    prompt = request.prompt
    size = request.size
    images = request.images
    mask = request.mask
    n = request.n
    quality = request.quality
    output_format = request.output_format
    output_compression = request.output_compression
    background = request.background
    moderation = request.moderation
    context = ensure_image_request_context(request.request_context)
    url = services.requests.image_edits_url(base_url_override)
    assert services.infrastructure.UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        services.core.transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    bg = services.core.normalize_image_background(background_for_upstream)
    fmt = services.core.normalize_image_output_format(output_format_for_upstream)
    compression = services.core.normalize_image_output_compression(
        output_compression, output_format=fmt
    )
    mod_value = services.core.normalize_image_moderation(moderation)
    quality_normalized = services.core.normalize_image_quality(quality)

    data: dict[str, str] = {
        "model": services.infrastructure.UPSTREAM_MODEL,
        "prompt": prompt_for_upstream,
        "size": size,
        "n": str(n),
        "quality": quality_normalized,
        "output_format": fmt,
        "background": bg,
        "moderation": mod_value,
    }
    if compression is not None:
        data["output_compression"] = str(compression)

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for i, raw in enumerate(images):
        files.append(("image[]", (f"ref-{i}.png", raw, "image/png")))
    # inpaint mask（可选）：单字段 `mask`，不是 `mask[]`。content-type image/png。
    # 走和 image[] 同款 _curl_post_multipart 路径；不走 httpx multipart（那条路上历史
    # 在某些网关下持续 502，curl 反而能 200，详见上方"图生图 multipart 走 curl 子进程"
    # 注释）。
    if mask is not None:
        files.append(("mask", ("mask.png", mask, "image/png")))

    trace_id = context.trace_id
    headers = services.core.auth_headers(api_key_override, trace_id=trace_id)
    services.core.attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/edits",
        body=data,
        files=files,
    )
    proxy_url = await services.infrastructure.resolve_provider_proxy_url(proxy_override)
    pinned_target = (
        None
        if proxy_url
        else services.requests.validated_byok_target_for_request(
            pinned_target_override, url
        )
    )
    started = services.infrastructure.time.monotonic()
    _, read_timeout_s = await _image_request_timeout(size, runtime=runtime)
    try:
        request_kwargs: dict[str, Any] = {
            "url": url,
            "data": data,
            "files": files,
            "headers": headers,
            "timeout_s": read_timeout_s,
            "proxy_url": proxy_url,
        }
        if pinned_target is not None:
            request_kwargs["pinned_target"] = pinned_target

        async def record_dispatch_ready() -> None:
            await services.transport.emit_image_progress(
                request.progress_callback,
                "dispatch_ready",
            )

        async def record_response_ready() -> None:
            await services.transport.emit_image_progress(
                request.progress_callback,
                "response_ready",
            )

        request_kwargs["on_dispatch_ready"] = record_dispatch_ready
        request_kwargs["on_response_ready"] = record_response_ready
        status, payload = await services.transport.curl_post_multipart(**request_kwargs)
    except services.infrastructure.httpx.TimeoutException as exc:
        _raise_direct_request_failure(
            exc,
            path="images/edits",
            log_endpoint="images_edits",
            description="direct edit request",
            url=url,
            trace_id=trace_id,
            started=started,
            timeout_s=read_timeout_s,
            runtime=runtime,
        )
    except (
        services.infrastructure.asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_direct_request_failure(
            exc,
            path="images/edits",
            log_endpoint="images_edits",
            description="direct edit request",
            url=url,
            trace_id=trace_id,
            started=started,
            timeout_s=None,
            runtime=runtime,
        )

    duration_ms = (services.infrastructure.time.monotonic() - started) * 1000.0
    services.core.log_upstream_call(
        endpoint="images_edits",
        status=status,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=None,  # curl path 不暴露 response headers
    )

    if status >= 400:
        raise services.core.with_error_context(
            services.core.parse_error(
                payload if isinstance(payload, dict) else {}, status
            ),
            path="images/edits",
            method="POST",
            url=url,
        )
    _raise_if_invalid_multipart_json(
        payload,
        status=status,
        url=url,
        trace_id=trace_id,
        runtime=runtime,
    )
    if isinstance(payload, dict):
        services.core.record_usage(payload.get("usage"))
    return await services.core.extract_image_results(
        payload,
        status,
        proxy_url=proxy_url,
        request_context=context,
    )


__all__ = [
    "_download_result_url_bytes",
    "_fetch_image_url_as_bytes",
    "_resolve_image_job_base_url",
    "_minimum_image_read_timeout",
    "_select_image_read_timeout",
    "_image_request_timeout",
    "_direct_image_result_unknown_error",
    "_direct_image_undelivered_error",
    "_direct_image_response_result_unknown_error",
    "_is_direct_image_result_unknown",
    "_direct_generate_image_once",
    "_wrap_inpaint_prompt",
    "_direct_edit_image_once",
]
