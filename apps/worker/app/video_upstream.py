"""Async video provider adapters and compatibility facade."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import httpx
from lumen_core.providers import socks_proxy_url
from lumen_core.url_security import (
    PublicHttpTarget,
    pinned_async_http_transport,
    resolve_public_http_target,
)
from lumen_core.video_providers import VideoProviderDefinition

from .config import settings
from .video_artifacts import (
    DownloadedVideo,
    UnsupportedVideoMediaError,
    detect_video_media,
)
from .video_upstream_content import (
    _clean_reference_label as _clean_reference_label,
    _prompt_with_official_reference_names as _prompt_with_official_reference_names,
    _prompt_with_reference_order as _prompt_with_reference_order,
    _reference_anchor_token as _reference_anchor_token,
    _reference_order_aliases as _reference_order_aliases,
    build_seedance_content,
)
from .video_upstream_parts import parsing as _parsing
from .video_upstream_parts.adapters import (
    DashScopeHappyHorseAdapter,
    FakeVideoAdapter,
    UnifiedVideoCreateAdapter,
    VolcanoNewApiVideoAdapter,
    VolcanoSeedanceAdapter,
    VolcanoThirdPartySeedanceAdapter,
    _require_http_url as _require_http_url,
)
from .video_upstream_parts.contracts import (
    CancelResult,
    PollResult,
    SubmitResult,
    VideoProviderAdapter,
    VideoProviderStatus as VideoProviderStatus,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
)
from .video_upstream_parts.runtime import AdapterRuntime, set_runtime_factory

_provider_task_path_segment = _parsing._provider_task_path_segment
_nested_get = _parsing._nested_get
_int_or_none = _parsing._int_or_none
_status = _parsing._status
_failure_class = _parsing._failure_class
_billable = _parsing._billable
_video_url = _parsing._video_url
_explicit_video_result_url = _parsing._explicit_video_result_url
_absolute_url = _parsing._absolute_url
_collapse_url_path_slashes = _parsing._collapse_url_path_slashes
_usage_total_tokens = _parsing._usage_total_tokens
_duration_usage_total_tokens = _parsing._duration_usage_total_tokens
_provider_task_id = _parsing._provider_task_id
_submit_headers = _parsing._submit_headers
_safety_identifier = _parsing._safety_identifier
_response_json = _parsing._response_json
_http_error = _parsing._http_error

_OMNI_FALLBACK_IMAGE_MAX_BYTES = 64 * 1024 * 1024
_SEEDANCE_INLINE_IMAGE_MAX_BYTES = 12 * 1024 * 1024
_VIDEO_FETCH_MAX_BYTES = 2 * 1024 * 1024 * 1024
_IMAGE_MIME_ALIASES = {
    "image/gif": "image/gif",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/x-png": "image/png",
}


def _image_response_mime(response: httpx.Response, fallback: str | None) -> str | None:
    fallback_value = fallback.strip() if isinstance(fallback, str) else ""
    raw = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if raw:
        if raw.startswith("image/"):
            return raw
        raise VideoUpstreamError(
            "Omni Flash fallback image URL did not return an image",
            error_code="invalid_input",
            status_code=422,
            raw={"content_type": raw},
        )
    return fallback_value or None


def _sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _validated_image_data_url_mime(
    data: bytes,
    declared_mime: str | None,
    *,
    field: str,
    max_bytes: int,
) -> str:
    if not data:
        raise VideoUpstreamError(
            f"{field} image bytes are empty",
            error_code="invalid_input",
            status_code=422,
        )
    if len(data) > max_bytes:
        raise VideoUpstreamError(
            f"{field} image is too large",
            error_code="invalid_input",
            status_code=413,
            raw={"actual_bytes": len(data), "max_bytes": max_bytes},
        )
    sniffed = _sniff_image_mime(data)
    if not sniffed:
        raise VideoUpstreamError(
            f"{field} image bytes are not a supported image",
            error_code="invalid_input",
            status_code=422,
            raw={"content_type": declared_mime},
        )
    declared_value = (
        declared_mime.split(";", 1)[0].strip().lower()
        if isinstance(declared_mime, str)
        else ""
    )
    if declared_value:
        normalized_declared = _IMAGE_MIME_ALIASES.get(declared_value)
        if normalized_declared is None:
            raise VideoUpstreamError(
                f"{field} image MIME is not supported",
                error_code="invalid_input",
                status_code=422,
                raw={
                    "content_type": declared_mime,
                    "detected_content_type": sniffed,
                },
            )
        if normalized_declared != sniffed:
            raise VideoUpstreamError(
                f"{field} image MIME does not match image bytes",
                error_code="invalid_input",
                status_code=422,
                raw={
                    "content_type": declared_mime,
                    "detected_content_type": sniffed,
                },
            )
    return sniffed


def _image_data_url(
    data: bytes,
    mime: str | None,
    *,
    field: str = "inline",
    max_bytes: int = _SEEDANCE_INLINE_IMAGE_MAX_BYTES,
) -> str:
    mime_value = _validated_image_data_url_mime(
        data,
        mime,
        field=field,
        max_bytes=max_bytes,
    )
    return f"data:{mime_value};base64,{base64.b64encode(data).decode('ascii')}"


def _seedance_image_data_url(data: bytes, mime: str | None) -> str:
    return _image_data_url(
        data,
        mime,
        field="Seedance inline/reference",
        max_bytes=_SEEDANCE_INLINE_IMAGE_MAX_BYTES,
    )


def _video_download_client(target: PublicHttpTarget) -> httpx.AsyncClient:
    transport = (
        pinned_async_http_transport(target)
        if getattr(target, "resolved_ips", ())
        else None
    )
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            write=settings.upstream_write_timeout_s,
            pool=30.0,
        ),
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    return httpx.AsyncClient(**client_kwargs)


def _video_redirect_url(response: httpx.Response) -> str | None:
    if response.status_code not in {301, 302, 303, 307, 308}:
        return None
    location = response.headers.get("location")
    if not location:
        raise VideoUpstreamError(
            "video fetch redirect did not include a location",
            error_code="fetch_failed",
            status_code=response.status_code,
        )
    return urljoin(str(response.url), location)


def _validate_video_download_response(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise VideoUpstreamError(
            f"video fetch failed status={response.status_code}",
            error_code="fetch_failed",
            status_code=response.status_code,
        )
    content_length = response.headers.get("content-length")
    if not content_length:
        return
    parsed_length = _int_or_none(content_length)
    if parsed_length is not None and parsed_length > _VIDEO_FETCH_MAX_BYTES:
        raise VideoUpstreamError(
            "video fetch response exceeds maximum size",
            error_code="fetch_failed",
            status_code=413,
        )


async def _download_video_url(
    video_url: str,
    *,
    max_redirects: int = 0,
    headers_for_url: Callable[[str], dict[str, str]] | None = None,
    client_factory: Callable[[PublicHttpTarget], httpx.AsyncClient] | None = None,
    ensure_active: Callable[[], None] | None = None,
) -> DownloadedVideo:
    current_url = video_url
    make_client = client_factory or _video_download_client
    for _redirect in range(max(0, max_redirects) + 1):
        if ensure_active is not None:
            ensure_active()
        try:
            target = await resolve_public_http_target(current_url, allow_http=True)
        except ValueError as exc:
            raise VideoUpstreamError(
                "video result URL must be public HTTP(S)",
                error_code="invalid_input",
                status_code=422,
            ) from exc
        async with make_client(target) as client:
            async with client.stream(
                "GET",
                target.url,
                headers=headers_for_url(target.url) if headers_for_url else None,
            ) as response:
                redirect_url = _video_redirect_url(response)
                if redirect_url is not None:
                    current_url = redirect_url
                    continue
                _validate_video_download_response(response)
                fd, raw_path = tempfile.mkstemp(
                    prefix="lumen-video-download-",
                    suffix=".part",
                )
                path = Path(raw_path)
                total = 0
                prefix = bytearray()
                declared_mime = response.headers.get("content-type")
                try:
                    with os.fdopen(fd, "wb") as file_obj:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            if ensure_active is not None:
                                ensure_active()
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > _VIDEO_FETCH_MAX_BYTES:
                                raise VideoUpstreamError(
                                    "video fetch response exceeds maximum size",
                                    error_code="fetch_failed",
                                    status_code=413,
                                )
                            if len(prefix) < 4096:
                                remaining = 4096 - len(prefix)
                                prefix.extend(chunk[:remaining])
                            file_obj.write(chunk)
                    if not total:
                        raise VideoUpstreamError(
                            "video fetch response was empty",
                            error_code="fetch_failed",
                            status_code=response.status_code,
                        )
                    try:
                        mime, extension = detect_video_media(
                            bytes(prefix),
                            declared_mime,
                        )
                    except UnsupportedVideoMediaError as exc:
                        raise VideoUpstreamError(
                            "video fetch response was not a supported video",
                            error_code="fetch_failed",
                            status_code=415,
                            raw={"content_type": declared_mime},
                        ) from exc
                    if ensure_active is not None:
                        ensure_active()
                    return DownloadedVideo(
                        path=path,
                        mime=mime,
                        extension=extension,
                        size_bytes=total,
                        declared_mime=declared_mime,
                    )
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
    raise VideoUpstreamError(
        "video fetch exceeded redirect limit",
        error_code="fetch_failed",
        status_code=508,
    )


async def _downloaded_video_bytes(downloaded: DownloadedVideo) -> bytes:
    try:
        return await asyncio.to_thread(downloaded.path.read_bytes)
    finally:
        downloaded.cleanup()


async def _fetch_video_url_bytes(video_url: str) -> bytes:
    return await _downloaded_video_bytes(await _download_video_url(video_url))


async def _fetch_image_url_as_data_url(
    raw_url: str,
    *,
    field: str,
    fallback_mime: str | None = None,
) -> str:
    try:
        target = await resolve_public_http_target(raw_url, allow_http=True)
    except ValueError as exc:
        raise VideoUpstreamError(
            f"{field} fallback URL must be public HTTP(S)",
            error_code="invalid_input",
            status_code=422,
        ) from exc

    timeout = httpx.Timeout(
        connect=settings.upstream_connect_timeout_s,
        read=min(settings.upstream_read_timeout_s, 120.0),
        write=settings.upstream_write_timeout_s,
        pool=30.0,
    )
    transport = (
        pinned_async_http_transport(target)
        if getattr(target, "resolved_ips", ())
        else None
    )
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("GET", target.url) as response:
            if response.status_code >= 300:
                raise VideoUpstreamError(
                    f"{field} fallback fetch failed status={response.status_code}",
                    error_code="invalid_input",
                    status_code=response.status_code,
                )
            content_length = response.headers.get("content-length")
            if content_length:
                expected_bytes = _int_or_none(content_length)
                if expected_bytes is None:
                    raise VideoUpstreamError(
                        f"{field} fallback image content-length is invalid",
                        error_code="invalid_input",
                        status_code=422,
                    )
                if expected_bytes > _OMNI_FALLBACK_IMAGE_MAX_BYTES:
                    raise VideoUpstreamError(
                        f"{field} fallback image is too large",
                        error_code="invalid_input",
                        status_code=413,
                    )
            mime = _image_response_mime(response, fallback_mime)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _OMNI_FALLBACK_IMAGE_MAX_BYTES:
                    raise VideoUpstreamError(
                        f"{field} fallback image is too large",
                        error_code="invalid_input",
                        status_code=413,
                    )
                chunks.append(bytes(chunk))
    data = b"".join(chunks)
    if not data:
        raise VideoUpstreamError(
            f"{field} fallback image is empty",
            error_code="invalid_input",
            status_code=422,
        )
    return _image_data_url(
        data,
        mime,
        field=field,
        max_bytes=_OMNI_FALLBACK_IMAGE_MAX_BYTES,
    )


def _seedance_content(
    req: VideoSubmitRequest,
    *,
    allow_input_image_url: bool = False,
    include_reference_order_prompt: bool = False,
    use_official_reference_names: bool = False,
) -> list[dict[str, Any]]:
    return build_seedance_content(
        req,
        allow_input_image_url=allow_input_image_url,
        include_reference_order_prompt=include_reference_order_prompt,
        use_official_reference_names=use_official_reference_names,
        image_data_url=_seedance_image_data_url,
        inline_image_max_bytes=_SEEDANCE_INLINE_IMAGE_MAX_BYTES,
        error_factory=VideoUpstreamError,
    )


def _adapter_runtime() -> AdapterRuntime:
    return AdapterRuntime(
        httpx=httpx,
        settings=settings,
        socks_proxy_url=lambda proxy: socks_proxy_url(proxy),
        pinned_async_http_transport=lambda target: pinned_async_http_transport(target),
        download_video_url=lambda *args, **kwargs: _download_video_url(*args, **kwargs),
        downloaded_video_bytes=lambda *args, **kwargs: _downloaded_video_bytes(
            *args, **kwargs
        ),
        fetch_image_url_as_data_url=lambda *args, **kwargs: (
            _fetch_image_url_as_data_url(*args, **kwargs)
        ),
        image_data_url=lambda *args, **kwargs: _image_data_url(*args, **kwargs),
        seedance_content=lambda *args, **kwargs: _seedance_content(*args, **kwargs),
    )


set_runtime_factory(_adapter_runtime)


def adapter_for_provider(provider: VideoProviderDefinition) -> VideoProviderAdapter:
    if provider.kind == "fake":
        return FakeVideoAdapter(provider)
    if provider.kind == "volcano":
        return VolcanoSeedanceAdapter(provider)
    if provider.kind == "volcano_third_party":
        return VolcanoThirdPartySeedanceAdapter(provider)
    if provider.kind == "volcano_newapi":
        return VolcanoNewApiVideoAdapter(provider)
    if provider.kind == "dashscope":
        return DashScopeHappyHorseAdapter(provider)
    if provider.kind == "omni_flash":
        return UnifiedVideoCreateAdapter(provider)
    raise VideoUpstreamError(
        f"unsupported video provider kind: {provider.kind}",
        error_code="provider_unavailable",
        status_code=503,
    )


__all__ = [
    "CancelResult",
    "DashScopeHappyHorseAdapter",
    "FakeVideoAdapter",
    "PollResult",
    "SubmitResult",
    "UnifiedVideoCreateAdapter",
    "VideoProviderAdapter",
    "VideoReferenceMedia",
    "VideoSubmitRequest",
    "VideoUpstreamError",
    "VolcanoNewApiVideoAdapter",
    "VolcanoSeedanceAdapter",
    "VolcanoThirdPartySeedanceAdapter",
    "adapter_for_provider",
]
