"""Provider-specific video upstream adapters."""

from __future__ import annotations

import hashlib
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from lumen_core.video_providers import (
    VideoProviderDefinition,
    seedance_20_duration_is_valid,
)

from ..video_artifacts import DownloadedVideo, downloaded_video_from_bytes
from ..video_upstream_content import _prompt_with_reference_order
from .contracts import (
    CancelResult,
    PollResult,
    SubmitResult,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
)
from .parsing import (
    _absolute_url,
    _billable,
    _collapse_url_path_slashes,
    _duration_usage_total_tokens,
    _explicit_video_result_url,
    _failure_class,
    _http_error,
    _int_or_none,
    _nested_get,
    _provider_task_id,
    _provider_task_path_segment,
    _response_json,
    _safety_identifier,
    _status,
    _submit_headers,
    _usage_total_tokens,
    _video_url,
)
from .runtime import AdapterRuntime, current_runtime


class VolcanoSeedanceAdapter:
    def __init__(
        self,
        provider: VideoProviderDefinition,
        *,
        runtime: AdapterRuntime | None = None,
    ) -> None:
        self.provider = provider
        self.runtime = runtime or current_runtime()

    def _client_base_url(self) -> str:
        return self.provider.base_url

    def _client(self) -> Any:
        proxy_url = (
            self.runtime.socks_proxy_url(self.provider.proxy)
            if self.provider.proxy
            else None
        )
        timeout = self.runtime.httpx.Timeout(
            connect=self.runtime.settings.upstream_connect_timeout_s,
            read=min(self.runtime.settings.upstream_read_timeout_s, 120.0),
            write=self.runtime.settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "base_url": self._client_base_url(),
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
            "headers": {"Authorization": f"Bearer {self.provider.api_key}"},
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return self.runtime.httpx.AsyncClient(**kwargs)

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        if not seedance_20_duration_is_valid(
            req.duration_s,
            req.model,
            req.upstream_model,
        ):
            raise VideoUpstreamError(
                "Seedance 2.0 duration must be -1 or between 4 and 15 seconds",
                error_code="invalid_input",
                status_code=422,
            )
        body: dict[str, Any] = {
            "model": req.upstream_model,
            "content": self.runtime.seedance_content(
                req,
                use_official_reference_names=True,
            ),
            "ratio": req.aspect_ratio,
            "resolution": req.resolution,
            "duration": req.duration_s,
            "generate_audio": req.generate_audio,
            "watermark": req.watermark,
            "safety_identifier": _safety_identifier(req.user_id),
        }
        if req.seed is not None:
            body["seed"] = req.seed
        if req.callback_url:
            body["callback_url"] = req.callback_url
        async with self._client() as client:
            response = await client.post(
                "/contents/generations/tasks",
                json=body,
                headers=_submit_headers(req),
            )
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("submit", response.status_code, raw)
        provider_task_id = _provider_task_id(raw)
        if provider_task_id is None:
            raise VideoUpstreamError(
                "video submit response did not include task id",
                error_code="bad_response",
                status_code=response.status_code,
                raw=raw,
            )
        return SubmitResult(provider_task_id=provider_task_id, raw=raw)

    async def poll(self, provider_task_id: str) -> PollResult:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.get(f"/contents/generations/tasks/{task_segment}")
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("poll", response.status_code, raw)
        status = _status(_nested_get(raw, ("status",), ("data", "status")))
        progress = _int_or_none(
            _nested_get(raw, ("progress",), ("data", "progress"), ("percent",))
        )
        return PollResult(
            status=status,
            progress=progress,
            video_url=_video_url(raw),
            failure_class=_failure_class(raw),
            usage_total_tokens=_usage_total_tokens(raw),
            upstream_billable=_billable(raw),
            raw=raw,
        )

    async def download_result(
        self,
        video_url: str,
        *,
        ensure_active: Callable[[], None] | None = None,
    ) -> DownloadedVideo:
        return await self.runtime.download_video_url(
            video_url,
            ensure_active=ensure_active,
        )

    async def fetch_result(self, video_url: str) -> bytes:
        downloaded = await self.download_result(video_url)
        return await self.runtime.downloaded_video_bytes(downloaded)

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.delete(
                f"/contents/generations/tasks/{task_segment}"
            )
        raw = _response_json(response)
        if response.status_code in {404, 410}:
            return CancelResult(accepted=False, raw=raw)
        if response.status_code >= 400:
            raise _http_error("cancel", response.status_code, raw)
        return CancelResult(accepted=True, raw=raw)


class VolcanoThirdPartySeedanceAdapter(VolcanoSeedanceAdapter):
    """Seedance-compatible third-party gateways such as MOYU."""

    def _client_base_url(self) -> str:
        return _collapse_url_path_slashes(self.provider.base_url)

    def _path(self, suffix: str) -> str:
        base_path = urlsplit(self._client_base_url()).path.rstrip("/")
        if base_path.endswith("/v1"):
            return suffix
        return f"v1/{suffix}"

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        prompt = _prompt_with_reference_order(req)
        metadata: dict[str, Any] = {
            "content": self.runtime.seedance_content(
                req,
                allow_input_image_url=True,
                include_reference_order_prompt=True,
            ),
            "resolution": req.resolution,
            "ratio": req.aspect_ratio,
            "generate_audio": req.generate_audio,
        }
        if req.duration_s != -1:
            metadata["duration"] = req.duration_s
        if req.seed is not None:
            metadata["seed"] = req.seed
        if req.watermark:
            metadata["watermark"] = req.watermark
        body = {
            "model": req.upstream_model,
            "prompt": prompt or "video generation",
            "metadata": metadata,
        }
        async with self._client() as client:
            response = await client.post(
                self._path("video/generations"),
                json=body,
                headers=_submit_headers(req),
            )
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("submit", response.status_code, raw)
        provider_task_id = _provider_task_id(raw)
        if provider_task_id is None:
            raise VideoUpstreamError(
                "video submit response did not include task id",
                error_code="bad_response",
                status_code=response.status_code,
                raw=raw,
            )
        return SubmitResult(provider_task_id=provider_task_id, raw=raw)

    async def poll(self, provider_task_id: str) -> PollResult:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.get(self._path(f"video/generations/{task_segment}"))
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("poll", response.status_code, raw)
        status = _status(
            _nested_get(
                raw,
                ("data", "status"),
                ("data", "data", "status"),
                ("data", "data", "data", "status"),
                ("status",),
            )
        )
        upstream_billable = _billable(raw)
        return PollResult(
            status=status,
            progress=_int_or_none(
                _nested_get(
                    raw,
                    ("data", "progress"),
                    ("data", "data", "progress"),
                    ("data", "data", "data", "progress"),
                    ("progress",),
                )
            ),
            video_url=_absolute_url(_video_url(raw), self._client_base_url()),
            failure_class=_failure_class(raw),
            usage_total_tokens=_usage_total_tokens(raw),
            upstream_billable=upstream_billable
            if upstream_billable is not None
            else (True if status == "succeeded" else None),
            raw=raw,
        )

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.delete(
                self._path(f"video/generations/{task_segment}")
            )
        raw = _response_json(response)
        if response.status_code in {404, 410}:
            return CancelResult(accepted=False, raw=raw)
        if response.status_code >= 400:
            raise _http_error("cancel", response.status_code, raw)
        return CancelResult(accepted=True, raw=raw)


class VolcanoNewApiVideoAdapter(VolcanoSeedanceAdapter):
    """New API compatible async video gateways using /v1/videos."""

    _MAX_CONTENT_REDIRECTS = 5

    def _client_base_url(self) -> str:
        return _collapse_url_path_slashes(self.provider.base_url)

    def _path(self, suffix: str) -> str:
        base_path = urlsplit(self._client_base_url()).path.rstrip("/")
        if base_path.endswith("/v1"):
            return suffix
        return f"v1/{suffix}"

    def _content_url(self, provider_task_id: str) -> str:
        task_segment = _provider_task_path_segment(provider_task_id)
        return urljoin(
            f"{self._client_base_url().rstrip('/')}/",
            self._path(f"videos/{task_segment}/content"),
        )

    def _media_url(self, item: VideoReferenceMedia, *, field: str) -> str:
        if item.url:
            return item.url
        raise VideoUpstreamError(
            f"{field} requires a public URL",
            error_code="invalid_input",
            status_code=422,
        )

    def _reference_media_arrays(
        self, req: VideoSubmitRequest
    ) -> tuple[list[str], list[str], list[str]]:
        images: list[str] = []
        videos: list[str] = []
        audios: list[str] = []
        if req.action == "i2v":
            if req.input_image_url:
                images.append(req.input_image_url)
            else:
                raise VideoUpstreamError(
                    "input image requires a public URL",
                    error_code="invalid_input",
                    status_code=422,
                )
        elif req.action == "reference":
            if not req.reference_media:
                raise VideoUpstreamError(
                    "reference generation requires reference image or video",
                    error_code="invalid_input",
                    status_code=422,
                )
            for item in req.reference_media:
                if item.kind == "image":
                    images.append(self._media_url(item, field="reference image"))
                elif item.kind == "video":
                    videos.append(self._media_url(item, field="reference video"))
                elif item.kind == "audio":
                    audios.append(self._media_url(item, field="reference audio"))
        elif req.action != "t2v":
            raise VideoUpstreamError(
                f"unsupported video action: {req.action}",
                error_code="invalid_input",
                status_code=422,
            )
        if len(images) > 4:
            raise VideoUpstreamError(
                "New API video generation supports at most 4 reference images",
                error_code="invalid_input",
                status_code=422,
            )
        if len(videos) > 3:
            raise VideoUpstreamError(
                "New API video generation supports at most 3 reference videos",
                error_code="invalid_input",
                status_code=422,
            )
        if len(audios) > 1:
            raise VideoUpstreamError(
                "New API video generation supports at most 1 reference audio",
                error_code="invalid_input",
                status_code=422,
            )
        return images, videos, audios

    def _submit_body(self, req: VideoSubmitRequest) -> dict[str, Any]:
        images, videos, audios = self._reference_media_arrays(req)
        body: dict[str, Any] = {
            "model": req.upstream_model,
            "prompt": _prompt_with_reference_order(req),
            "seconds": 15 if req.duration_s == -1 else req.duration_s,
        }
        if req.aspect_ratio != "adaptive":
            body["aspect_ratio"] = req.aspect_ratio
        if images:
            body["images"] = images
        if videos:
            body["videos"] = videos
        if audios:
            body["audios"] = audios
        return body

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        async with self._client() as client:
            response = await client.post(
                self._path("videos"),
                json=self._submit_body(req),
                headers=_submit_headers(req),
            )
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("submit", response.status_code, raw)
        provider_task_id = _provider_task_id(raw)
        if provider_task_id is None:
            raise VideoUpstreamError(
                "video submit response did not include task id",
                error_code="bad_response",
                status_code=response.status_code,
                raw=raw,
            )
        return SubmitResult(provider_task_id=provider_task_id, raw=raw)

    async def poll(self, provider_task_id: str) -> PollResult:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.get(self._path(f"videos/{task_segment}"))
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("poll", response.status_code, raw)
        status = _status(
            _nested_get(
                raw,
                ("status",),
                ("data", "status"),
                ("result", "status"),
                ("output", "status"),
                ("task_status",),
                ("data", "task_status"),
                ("output", "task_status"),
            )
        )
        progress = _int_or_none(
            _nested_get(
                raw,
                ("progress",),
                ("data", "progress"),
                ("result", "progress"),
                ("output", "progress"),
                ("percent",),
            )
        )
        video_url = _absolute_url(
            _explicit_video_result_url(raw), self._client_base_url()
        )
        if status == "succeeded" and not video_url:
            video_url = self._content_url(provider_task_id)
        upstream_billable = _billable(raw)
        return PollResult(
            status=status,
            progress=progress,
            video_url=video_url,
            failure_class=_failure_class(raw),
            usage_total_tokens=_usage_total_tokens(raw)
            or _duration_usage_total_tokens(raw),
            upstream_billable=upstream_billable
            if upstream_billable is not None
            else (True if status == "succeeded" else None),
            raw=raw,
        )

    def _content_request_headers(self, raw_url: str) -> dict[str, str]:
        target = urlsplit(raw_url)
        base = urlsplit(self._client_base_url())
        target_port = target.port or (443 if target.scheme == "https" else 80)
        base_port = base.port or (443 if base.scheme == "https" else 80)
        if (
            target.scheme.lower() == base.scheme.lower()
            and target.hostname == base.hostname
            and target_port == base_port
        ):
            return {"Authorization": f"Bearer {self.provider.api_key}"}
        return {}

    def _raw_client(self, target: Any = None) -> Any:
        proxy_url = (
            self.runtime.socks_proxy_url(self.provider.proxy)
            if self.provider.proxy
            else None
        )
        timeout = self.runtime.httpx.Timeout(
            connect=self.runtime.settings.upstream_connect_timeout_s,
            read=self.runtime.settings.upstream_read_timeout_s,
            write=self.runtime.settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        elif target is not None and target.resolved_ips:
            kwargs["transport"] = self.runtime.pinned_async_http_transport(target)
        return self.runtime.httpx.AsyncClient(**kwargs)

    async def download_result(
        self,
        video_url: str,
        *,
        ensure_active: Callable[[], None] | None = None,
    ) -> DownloadedVideo:
        return await self.runtime.download_video_url(
            video_url,
            max_redirects=self._MAX_CONTENT_REDIRECTS,
            headers_for_url=self._content_request_headers,
            client_factory=self._raw_client,
            ensure_active=ensure_active,
        )

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        del provider_task_id
        return None


class UnifiedVideoCreateAdapter(VolcanoSeedanceAdapter):
    """Third-party unified video gateways using /v1/video/generations."""

    def _client_base_url(self) -> str:
        return _collapse_url_path_slashes(self.provider.base_url)

    def _path(self, suffix: str) -> str:
        base_path = urlsplit(self._client_base_url()).path.rstrip("/")
        if base_path.endswith("/v1"):
            return suffix
        return f"v1/{suffix}"

    def _is_invalid_path_error(self, exc: VideoUpstreamError) -> bool:
        if exc.status_code not in {404, 405}:
            return False
        messages = [
            str(exc),
            _nested_get(
                exc.raw,
                ("error", "message"),
                ("error",),
                ("message",),
                ("text",),
            ),
        ]
        return any(
            isinstance(message, str) and "invalid url" in message.lower()
            for message in messages
        )

    def _media_url(self, item: VideoReferenceMedia, *, field: str) -> str:
        if item.kind != "image":
            raise VideoUpstreamError(
                "Omni Flash unified video create supports image references only",
                error_code="invalid_input",
                status_code=422,
            )
        if item.url:
            return item.url
        if not item.data:
            raise VideoUpstreamError(
                f"{field} is required",
                error_code="invalid_input",
                status_code=422,
            )
        return self.runtime.image_data_url(
            item.data,
            item.mime,
            field=field,
            max_bytes=64 * 1024 * 1024,
        )

    async def _media_data_url(
        self,
        item: VideoReferenceMedia,
        *,
        field: str,
    ) -> str:
        if item.kind != "image":
            raise VideoUpstreamError(
                "Omni Flash unified video create supports image references only",
                error_code="invalid_input",
                status_code=422,
            )
        if item.data:
            return self.runtime.image_data_url(
                item.data,
                item.mime,
                field=field,
                max_bytes=64 * 1024 * 1024,
            )
        if item.url:
            return await self._fetch_image_url_data_url(
                item.url,
                field=field,
                fallback_mime=item.mime,
            )
        raise VideoUpstreamError(
            f"{field} is required",
            error_code="invalid_input",
            status_code=422,
        )

    async def _fetch_image_url_data_url(
        self,
        raw_url: str,
        *,
        field: str,
        fallback_mime: str | None = None,
    ) -> str:
        return await self.runtime.fetch_image_url_as_data_url(
            raw_url,
            field=field,
            fallback_mime=fallback_mime,
        )

    def _reference_image_refs(
        self, req: VideoSubmitRequest
    ) -> list[VideoReferenceMedia]:
        image_refs = [item for item in req.reference_media if item.kind == "image"]
        if len(image_refs) != len(req.reference_media):
            raise VideoUpstreamError(
                "Omni Flash unified video create supports image references only",
                error_code="invalid_input",
                status_code=422,
            )
        if not image_refs:
            raise VideoUpstreamError(
                "reference generation requires reference images",
                error_code="invalid_input",
                status_code=422,
            )
        if len(image_refs) > 9:
            raise VideoUpstreamError(
                "too many reference image items",
                error_code="invalid_input",
                status_code=422,
            )
        return image_refs

    def _images(self, req: VideoSubmitRequest) -> list[str]:
        if req.action == "t2v":
            return []
        if req.action == "i2v":
            if req.input_image_url:
                return [req.input_image_url]
            if not req.input_image_bytes:
                raise VideoUpstreamError(
                    "missing input image bytes",
                    error_code="invalid_input",
                    status_code=422,
                )
            return [
                self.runtime.image_data_url(
                    req.input_image_bytes,
                    req.input_image_mime,
                    field="Omni Flash input",
                    max_bytes=64 * 1024 * 1024,
                )
            ]
        if req.action == "reference":
            return [
                self._media_url(item, field="Omni Flash reference image")
                for item in self._reference_image_refs(req)
            ]
        raise VideoUpstreamError(
            f"unsupported video action: {req.action}",
            error_code="invalid_input",
            status_code=422,
        )

    async def _data_url_images(self, req: VideoSubmitRequest) -> list[str]:
        if req.action == "t2v":
            return []
        if req.action == "i2v":
            if req.input_image_bytes:
                return [
                    self.runtime.image_data_url(
                        req.input_image_bytes,
                        req.input_image_mime,
                        field="Omni Flash input",
                        max_bytes=64 * 1024 * 1024,
                    )
                ]
            if req.input_image_url:
                return [
                    await self._fetch_image_url_data_url(
                        req.input_image_url,
                        field="Omni Flash input image",
                        fallback_mime=req.input_image_mime,
                    )
                ]
            raise VideoUpstreamError(
                "missing input image bytes",
                error_code="invalid_input",
                status_code=422,
            )
        if req.action == "reference":
            return [
                await self._media_data_url(
                    item,
                    field="Omni Flash reference image",
                )
                for item in self._reference_image_refs(req)
            ]
        raise VideoUpstreamError(
            f"unsupported video action: {req.action}",
            error_code="invalid_input",
            status_code=422,
        )

    def _submit_body(
        self,
        req: VideoSubmitRequest,
        *,
        images: list[str],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.upstream_model,
            "prompt": _prompt_with_reference_order(req),
            "size": req.resolution.upper(),
        }
        if req.aspect_ratio != "adaptive":
            body["aspect_ratio"] = req.aspect_ratio
        if req.duration_s != -1:
            body["duration"] = req.duration_s
        if req.seed is not None and req.seed != -1:
            body["seed"] = req.seed
        if req.watermark:
            body["watermark"] = req.watermark
        if not req.generate_audio:
            body["generate_audio"] = False
        if req.callback_url:
            body["callback_url"] = req.callback_url
        if images:
            body["images"] = images
        return body

    async def _post_submit_body(
        self,
        path: str,
        body: dict[str, Any],
        req: VideoSubmitRequest,
    ) -> SubmitResult:
        async with self._client() as client:
            response = await client.post(path, json=body, headers=_submit_headers(req))
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("submit", response.status_code, raw)
        provider_task_id = _provider_task_id(raw)
        if provider_task_id is None:
            raise VideoUpstreamError(
                "video submit response did not include task id",
                error_code="bad_response",
                status_code=response.status_code,
                raw=raw,
            )
        return SubmitResult(provider_task_id=provider_task_id, raw=raw)

    def _should_retry_with_data_urls(self, exc: VideoUpstreamError) -> bool:
        if exc.error_code != "invalid_input" or exc.status_code in {404, 405}:
            return False
        messages = [
            str(exc),
            _nested_get(exc.raw, ("error", "message"), ("message",), ("text",)),
        ]
        return any(
            isinstance(message, str) and "invalid url" in message.lower()
            for message in messages
        )

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        submit_path = self._path("video/generations")
        images = self._images(req)
        body = self._submit_body(req, images=images)
        try:
            return await self._post_submit_body(submit_path, body, req)
        except VideoUpstreamError as exc:
            if self._is_invalid_path_error(exc):
                submit_path = self._path("video/create")
                try:
                    return await self._post_submit_body(submit_path, body, req)
                except VideoUpstreamError as legacy_exc:
                    exc = legacy_exc
            if not self._should_retry_with_data_urls(exc):
                raise
            try:
                fallback_images = await self._data_url_images(req)
            except VideoUpstreamError:
                raise exc
            if fallback_images == images:
                raise exc
            return await self._post_submit_body(
                submit_path,
                self._submit_body(req, images=fallback_images),
                req,
            )

    async def poll(self, provider_task_id: str) -> PollResult:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.get(self._path(f"video/generations/{task_segment}"))
        raw = _response_json(response)
        if response.status_code >= 400:
            exc = _http_error("poll", response.status_code, raw)
            if not self._is_invalid_path_error(exc):
                raise exc
            async with self._client() as client:
                response = await client.get(
                    self._path("video/query"),
                    params={"id": provider_task_id},
                )
            raw = _response_json(response)
            if response.status_code >= 400:
                raise _http_error("poll", response.status_code, raw)
        status = _status(
            _nested_get(
                raw,
                ("status",),
                ("data", "status"),
                ("data", "data", "status"),
                ("result", "status"),
                ("output", "status"),
                ("task_status",),
                ("data", "task_status"),
                ("output", "task_status"),
            )
        )
        progress = _int_or_none(
            _nested_get(
                raw,
                ("progress",),
                ("data", "progress"),
                ("data", "data", "progress"),
                ("result", "progress"),
                ("output", "progress"),
                ("percent",),
            )
        )
        upstream_billable = _billable(raw)
        video_url = _absolute_url(_video_url(raw), self._client_base_url())
        explicit_video_url = _absolute_url(
            _explicit_video_result_url(raw),
            self._client_base_url(),
        )
        if status == "running" and explicit_video_url:
            status = "succeeded"
            video_url = explicit_video_url
        return PollResult(
            status=status,
            progress=progress,
            video_url=video_url,
            failure_class=_failure_class(raw),
            usage_total_tokens=_usage_total_tokens(raw)
            or _duration_usage_total_tokens(raw),
            upstream_billable=upstream_billable
            if upstream_billable is not None
            else (True if status == "succeeded" else None),
            raw=raw,
        )

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        del provider_task_id
        return None


class DashScopeHappyHorseAdapter:
    def __init__(
        self,
        provider: VideoProviderDefinition,
        *,
        runtime: AdapterRuntime | None = None,
    ) -> None:
        self.provider = provider
        self.runtime = runtime or current_runtime()

    def _client(self) -> Any:
        proxy_url = (
            self.runtime.socks_proxy_url(self.provider.proxy)
            if self.provider.proxy
            else None
        )
        timeout = self.runtime.httpx.Timeout(
            connect=self.runtime.settings.upstream_connect_timeout_s,
            read=min(self.runtime.settings.upstream_read_timeout_s, 120.0),
            write=self.runtime.settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "base_url": self.provider.base_url,
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
            "headers": {
                "Authorization": f"Bearer {self.provider.api_key}",
                "X-DashScope-Async": "enable",
            },
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return self.runtime.httpx.AsyncClient(**kwargs)

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        input_payload: dict[str, Any] = {"prompt": _prompt_with_reference_order(req)}
        if req.action == "i2v":
            input_payload["media"] = [
                {
                    "type": "first_frame",
                    "url": _require_http_url(
                        req.input_image_url,
                        field="HappyHorse image-to-video input image URL",
                    ),
                }
            ]
        elif req.action == "reference":
            if not req.reference_media:
                raise VideoUpstreamError(
                    "HappyHorse reference-to-video requires reference images",
                    error_code="invalid_input",
                    status_code=422,
                )
            urls: list[str] = []
            for item in req.reference_media:
                if item.kind != "image":
                    raise VideoUpstreamError(
                        "HappyHorse reference-to-video does not support reference videos",
                        error_code="invalid_input",
                        status_code=422,
                    )
                urls.append(
                    _require_http_url(
                        item.url,
                        field="HappyHorse reference image URL",
                    )
                )
            if len(urls) > 9:
                raise VideoUpstreamError(
                    "HappyHorse reference-to-video supports at most 9 reference images",
                    error_code="invalid_input",
                    status_code=422,
                )
            input_payload["media"] = [
                {"type": "reference_image", "url": url} for url in urls
            ]

        parameters: dict[str, Any] = {
            "resolution": req.resolution.upper(),
            "watermark": req.watermark,
        }
        if req.duration_s != -1:
            parameters["duration"] = req.duration_s
        if req.action in {"t2v", "reference"} and req.aspect_ratio != "adaptive":
            parameters["ratio"] = req.aspect_ratio
        if req.seed is not None:
            if req.seed == -1:
                pass
            elif 0 <= req.seed <= 2_147_483_647:
                parameters["seed"] = req.seed
            else:
                raise VideoUpstreamError(
                    "HappyHorse seed must be between 0 and 2147483647",
                    error_code="invalid_input",
                    status_code=422,
                )
        body = {
            "model": req.upstream_model,
            "input": input_payload,
            "parameters": parameters,
        }
        async with self._client() as client:
            response = await client.post(
                "/api/v1/services/aigc/video-generation/video-synthesis",
                json=body,
                headers=_submit_headers(req),
            )
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("submit", response.status_code, raw)
        provider_task_id = _provider_task_id(raw)
        if provider_task_id is None:
            raise VideoUpstreamError(
                "HappyHorse submit response did not include task id",
                error_code="bad_response",
                status_code=response.status_code,
                raw=raw,
            )
        return SubmitResult(provider_task_id=provider_task_id, raw=raw)

    async def poll(self, provider_task_id: str) -> PollResult:
        task_segment = _provider_task_path_segment(provider_task_id)
        async with self._client() as client:
            response = await client.get(f"/api/v1/tasks/{task_segment}")
        raw = _response_json(response)
        if response.status_code >= 400:
            raise _http_error("poll", response.status_code, raw)
        status = _status(
            _nested_get(raw, ("output", "task_status"), ("task_status",), ("status",))
        )
        progress = _int_or_none(
            _nested_get(raw, ("output", "progress"), ("progress",), ("percent",))
        )
        usage_tokens = _duration_usage_total_tokens(raw)
        upstream_billable = _billable(raw)
        return PollResult(
            status=status,
            progress=progress,
            video_url=_video_url(raw),
            failure_class=_failure_class(raw),
            usage_total_tokens=usage_tokens,
            upstream_billable=upstream_billable
            if upstream_billable is not None
            else (True if status == "succeeded" else None),
            raw=raw,
        )

    async def download_result(
        self,
        video_url: str,
        *,
        ensure_active: Callable[[], None] | None = None,
    ) -> DownloadedVideo:
        return await self.runtime.download_video_url(
            video_url,
            ensure_active=ensure_active,
        )

    async def fetch_result(self, video_url: str) -> bytes:
        downloaded = await self.download_result(video_url)
        return await self.runtime.downloaded_video_bytes(downloaded)

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        # HappyHorse has no portable cancellation endpoint.
        del provider_task_id
        return None


class FakeVideoAdapter:
    """Deterministic local adapter for tests and development."""

    def __init__(
        self,
        provider: VideoProviderDefinition,
        *,
        runtime: AdapterRuntime | None = None,
    ) -> None:
        self.provider = provider
        self.runtime = runtime or current_runtime()

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        digest = hashlib.sha256(req.task_id.encode("utf-8")).hexdigest()[:16]
        return SubmitResult(
            provider_task_id=f"fake-video-{digest}",
            raw={"id": f"fake-video-{digest}"},
        )

    async def poll(self, provider_task_id: str) -> PollResult:
        return PollResult(
            status="succeeded",
            progress=100,
            video_url=f"fake://{provider_task_id}",
            usage_total_tokens=1000,
            upstream_billable=True,
            raw={"id": provider_task_id, "status": "succeeded"},
        )

    async def download_result(
        self,
        video_url: str,
        *,
        ensure_active: Callable[[], None] | None = None,
    ) -> DownloadedVideo:
        del video_url
        if ensure_active is not None:
            ensure_active()
        return downloaded_video_from_bytes(
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat",
            declared_mime="video/mp4",
        )

    async def fetch_result(self, video_url: str) -> bytes:
        downloaded = await self.download_result(video_url)
        return await self.runtime.downloaded_video_bytes(downloaded)

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        return CancelResult(
            accepted=True,
            raw={"id": provider_task_id, "deleted": True},
        )


def _require_http_url(raw: str | None, *, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise VideoUpstreamError(
            f"{field} is required",
            error_code="invalid_input",
            status_code=422,
        )
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise VideoUpstreamError(
            f"{field} must be an HTTP(S) URL",
            error_code="invalid_input",
            status_code=422,
        )
    return value


__all__ = [
    "DashScopeHappyHorseAdapter",
    "FakeVideoAdapter",
    "UnifiedVideoCreateAdapter",
    "VolcanoNewApiVideoAdapter",
    "VolcanoSeedanceAdapter",
    "VolcanoThirdPartySeedanceAdapter",
]
