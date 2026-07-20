"""Direct Images API request execution extracted from app.upstream."""

from __future__ import annotations

import importlib
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


class _FacadeProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_facade(), name)


_runtime = _FacadeProxy()


async def _download_result_url_bytes(
    image_url: str,
    *,
    path: str,
    log_endpoint: str,
    description: str,
    allowed_base_url: str | None = None,
) -> bytes:
    started = _runtime.time.monotonic()
    trace_id = _runtime._generate_trace_id()
    try:
        response = await _runtime.download_public_http_url(
            image_url,
            max_bytes=_runtime._IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            max_redirects=5,
            allow_http=True,
            allowed_private_origins=((allowed_base_url,) if allowed_base_url else ()),
            dns_timeout_s=2.0,
            timeout=_runtime.httpx.Timeout(
                connect=_runtime.settings.upstream_connect_timeout_s,
                read=_runtime.settings.upstream_read_timeout_s,
                write=_runtime.settings.upstream_write_timeout_s,
                pool=_runtime.settings.upstream_connect_timeout_s,
            ),
            headers={"User-Agent": "lumen-worker"},
        )
    except _runtime.PublicHttpBodyTooLarge as exc:
        _runtime._log_upstream_call(
            endpoint=log_endpoint,
            status=exc.status_code or 0,
            duration_ms=(_runtime.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"{description} exceeded max bytes",
            status_code=exc.status_code,
            error_code=_runtime.EC.STREAM_TOO_LARGE.value,
            payload={
                "url": image_url,
                "path": path,
                "method": "GET",
                "bytes": exc.received_bytes,
                "max_bytes": exc.max_bytes,
            },
        ) from exc
    except ValueError as exc:
        _runtime._log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(_runtime.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"unsafe image result URL: {exc}",
            status_code=400,
            error_code=_runtime.EC.INVALID_VALUE.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc
    except (_runtime.httpx.HTTPError, OSError) as exc:
        _runtime._log_upstream_call(
            endpoint=log_endpoint,
            status=0,
            duration_ms=(_runtime.time.monotonic() - started) * 1000.0,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"{description} failed: {exc}",
            status_code=0,
            error_code=_runtime.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={"url": image_url, "path": path, "method": "GET"},
        ) from exc

    _runtime._log_upstream_call(
        endpoint=log_endpoint,
        status=response.status_code,
        duration_ms=(_runtime.time.monotonic() - started) * 1000.0,
        trace_id=trace_id,
        response_headers=response.headers,
    )
    if not 200 <= response.status_code < 300:
        raise _runtime.UpstreamError(
            f"{description} http {response.status_code}",
            status_code=response.status_code,
            error_code=_runtime.EC.UPSTREAM_ERROR.value,
            payload={
                "url": image_url,
                "final_url": response.url,
                "path": path,
                "method": "GET",
            },
        )
    if not response.body:
        raise _runtime.UpstreamError(
            f"{description} returned empty body",
            status_code=response.status_code,
            error_code=_runtime.EC.NO_IMAGE_RETURNED.value,
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
) -> bytes:
    """下载 images API 在 data[].url 里返回的图片，转成原始字节。

    OpenAI 协议合法的两种响应形态之一：当 response_format=url（旧默认 / 部分
    第三方网关行为）时，图片以 CDN 链接返回而非 b64_json。下载使用逐跳校验和
    DNS-pinned 直连；不复用 provider client，避免代理或二次 DNS 解析绕过 SSRF
    边界。
    """
    _ = proxy_url
    return await _runtime._download_result_url_bytes(
        image_url,
        path="images/result",
        log_endpoint="image_url_download",
        description="image url download",
    )


async def _resolve_image_job_base_url() -> str:
    try:
        raw = await _runtime.resolve("image.job_base_url")
    except Exception as exc:  # noqa: BLE001
        _runtime.logger.debug("image job base URL setting fallback err=%s", exc)
        raw = None
    return _runtime._validate_image_job_base_url(
        raw or _runtime._DEFAULT_IMAGE_JOB_BASE_URL
    )


def _minimum_image_read_timeout(size: str) -> float:
    pixels = _runtime._parse_size_pixels(size)
    if pixels is not None and pixels > _runtime._IMAGE_4K_PIXELS:
        return _runtime._IMAGE_READ_TIMEOUT_4K_S
    return _runtime._IMAGE_READ_TIMEOUT_MIN_S


async def _image_request_timeout(size: str) -> tuple[_runtime.httpx.Timeout, float]:
    timeout_config = await _runtime._resolve_timeout_config()
    read_timeout_s = max(
        timeout_config.read, _runtime._minimum_image_read_timeout(size)
    )
    return timeout_config.to_httpx(read=read_timeout_s), read_timeout_s


def _direct_image_result_unknown_error(
    exc: BaseException,
    *,
    path: str,
    method: str,
    url: str,
    trace_id: str,
    timeout_s: float,
) -> _runtime.UpstreamError:
    exc_type = type(exc).__name__
    return _runtime.UpstreamError(
        (
            f"{path} timed out after {timeout_s:.0f}s; upstream result is unknown. "
            "The request may already have been accepted, so it was not retried automatically."
        ),
        status_code=0,
        error_code=_runtime.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        payload={
            "path": path,
            "method": method,
            "url": url,
            "x_trace_id": trace_id,
            "timeout_s": timeout_s,
            "upstream_result_unknown": True,
            "exception": exc_type,
        },
    )


def _is_direct_image_result_unknown(exc: BaseException) -> bool:
    return (
        isinstance(exc, _runtime.UpstreamError)
        and exc.error_code == _runtime.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
    )


async def _direct_generate_image_once(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    base_url_override: str,
    api_key_override: str,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> list[tuple[str, str | None]]:
    """Text-to-image via direct `/v1/images/generations` using gpt-image-2."""
    proxy_url = await _runtime.resolve_provider_proxy_url(proxy_override)
    url = _runtime._image_generations_url(base_url_override)
    pinned_target = (
        None
        if proxy_url
        else _runtime._validated_byok_target_for_request(pinned_target_override, url)
    )
    if proxy_url:
        client = await _runtime._get_images_client(proxy_url)
    elif pinned_target is not None:
        client = await _runtime._get_images_client(pinned_target=pinned_target)
    else:
        client = await _runtime._get_images_client()
    # Model 显式 pin：UPSTREAM_MODEL 来自 lumen_core.constants（lumen-core wheel 里固化）。
    # 加 runtime assert 防止未来改动把 model 字段隐式置空 / fallback 到上游默认。
    assert _runtime.UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _runtime._transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    body: dict[str, Any] = {
        "model": _runtime.UPSTREAM_MODEL,
        "prompt": prompt_for_upstream,
        "size": size,
        "n": n,
        "quality": _runtime._normalize_image_quality(quality),
    }
    _runtime._add_image_output_options(
        body,
        output_format=output_format_for_upstream,
        output_compression=output_compression,
        background=background_for_upstream,
        moderation=moderation,
    )
    trace_id = _runtime._generate_trace_id()
    headers = _runtime._auth_headers(api_key_override, trace_id=trace_id)
    _runtime._attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/generations",
        body=body,
    )
    request_timeout, read_timeout_s = await _runtime._image_request_timeout(size)
    started = _runtime.time.monotonic()
    try:
        resp = await _runtime._post_with_retry(
            client=client,
            url=url,
            headers=headers,
            json_body=body,
            timeout=request_timeout,
            retry_httpx_exceptions=False,
            before_attempt=before_attempt,
        )
    except _runtime.httpx.TimeoutException as exc:
        duration_ms = (_runtime.time.monotonic() - started) * 1000.0
        _runtime._log_upstream_call(
            endpoint="images_generations",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime._direct_image_result_unknown_error(
            exc,
            path="images/generations",
            method="POST",
            url=url,
            trace_id=trace_id,
            timeout_s=read_timeout_s,
        ) from exc
    except _runtime._RETRY_HTTPX_EXC as exc:
        duration_ms = (_runtime.time.monotonic() - started) * 1000.0
        _runtime._log_upstream_call(
            endpoint="images_generations",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"direct image request failed: {exc}",
            status_code=0,
            error_code=_runtime.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    duration_ms = (_runtime.time.monotonic() - started) * 1000.0
    _runtime._log_upstream_call(
        endpoint="images_generations",
        status=resp.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(resp, "headers", None),
    )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise _runtime.UpstreamError(
            "upstream returned invalid JSON",
            status_code=resp.status_code,
            error_code=_runtime.EC.BAD_RESPONSE.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    if resp.status_code >= 400:
        raise _runtime._with_error_context(
            _runtime._parse_error(
                payload if isinstance(payload, dict) else {}, resp.status_code
            ),
            path="images/generations",
            method="POST",
            url=url,
        )
    # JSON 响应里的 usage（如有）也走标准埋点。
    if isinstance(payload, dict):
        _runtime._record_usage(payload.get("usage"))
    return await _runtime._extract_image_results(
        payload, resp.status_code, proxy_url=proxy_url
    )


def _wrap_inpaint_prompt(user_intent: str) -> str:
    """局部 inpaint 的 prompt 包装。

    OpenAI /v1/images/edits + mask 字段必须用 invariant 模板才能正确 inpaint：
    否则 mask 区会被填黑、prompt 内容画到别处。本地 spike 已验证（mean diff 1.9）。

    前缀（"Inside the masked region,"）和后缀（preserve / do not add / blend 三条）
    都稳定，user_intent 夹在中间——这样 prompt cache prefix 在多次 retry 间保持稳定。
    只在 mask 不为空时调用；mask 为空时直接发原始 prompt（保持 i2i 行为不变）。

    第四行 "Blend ..." 让模型在 remove / replace 类指令下知道用周围像素自然过渡填充
    （否则 "Inside the masked region, remove the apple." 模型常困惑该填什么 → 填黑/灰色）。
    """
    return _runtime.upstream_image_requests._wrap_inpaint_prompt(user_intent)


async def _direct_edit_image_once(
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
    url = _runtime._image_edits_url(base_url_override)
    assert _runtime.UPSTREAM_MODEL, "model must be set"
    prompt_for_upstream, output_format_for_upstream, background_for_upstream = (
        _runtime._transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
        )
    )
    bg = _runtime._normalize_image_background(background_for_upstream)
    fmt = _runtime._normalize_image_output_format(output_format_for_upstream)
    compression = _runtime._normalize_image_output_compression(
        output_compression, output_format=fmt
    )
    mod_value = _runtime._normalize_image_moderation(moderation)
    quality_normalized = _runtime._normalize_image_quality(quality)

    data: dict[str, str] = {
        "model": _runtime.UPSTREAM_MODEL,
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

    trace_id = _runtime._generate_trace_id()
    headers = _runtime._auth_headers(api_key_override, trace_id=trace_id)
    _runtime._attach_image_idempotency_key(
        headers,
        trace_id=trace_id,
        endpoint="images/edits",
        body=data,
        files=files,
    )
    proxy_url = await _runtime.resolve_provider_proxy_url(proxy_override)
    pinned_target = (
        None
        if proxy_url
        else _runtime._validated_byok_target_for_request(pinned_target_override, url)
    )
    started = _runtime.time.monotonic()
    _, read_timeout_s = await _runtime._image_request_timeout(size)
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
        status, payload = await _runtime._curl_post_multipart(**request_kwargs)
    except _runtime.httpx.TimeoutException as exc:
        duration_ms = (_runtime.time.monotonic() - started) * 1000.0
        _runtime._log_upstream_call(
            endpoint="images_edits",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime._direct_image_result_unknown_error(
            exc,
            path="images/edits",
            method="POST",
            url=url,
            trace_id=trace_id,
            timeout_s=read_timeout_s,
        ) from exc
    except (_runtime.asyncio.CancelledError, _runtime.UpstreamCancelled):
        raise
    except Exception as exc:  # noqa: BLE001
        duration_ms = (_runtime.time.monotonic() - started) * 1000.0
        _runtime._log_upstream_call(
            endpoint="images_edits",
            status=0,
            duration_ms=duration_ms,
            trace_id=trace_id,
            response_headers=None,
        )
        raise _runtime.UpstreamError(
            f"direct edit request failed: {exc}",
            status_code=0,
            error_code=_runtime.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
            payload={
                "path": "images/edits",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc

    duration_ms = (_runtime.time.monotonic() - started) * 1000.0
    _runtime._log_upstream_call(
        endpoint="images_edits",
        status=status,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=None,  # curl path 不暴露 response headers
    )

    if status >= 400:
        raise _runtime._with_error_context(
            _runtime._parse_error(payload if isinstance(payload, dict) else {}, status),
            path="images/edits",
            method="POST",
            url=url,
        )
    if isinstance(payload, dict):
        _runtime._record_usage(payload.get("usage"))
    return await _runtime._extract_image_results(payload, status, proxy_url=proxy_url)


__all__ = [
    "_download_result_url_bytes",
    "_fetch_image_url_as_bytes",
    "_resolve_image_job_base_url",
    "_minimum_image_read_timeout",
    "_image_request_timeout",
    "_direct_image_result_unknown_error",
    "_is_direct_image_result_unknown",
    "_direct_generate_image_once",
    "_wrap_inpaint_prompt",
    "_direct_edit_image_once",
]
