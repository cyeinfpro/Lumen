"""DashScope HappyHorse video upstream adapter."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from lumen_core.video_providers import VideoProviderDefinition

from ..video_artifacts import DownloadedVideo
from .content import prompt_with_reference_order as _prompt_with_reference_order
from .contracts import (
    CancelResult,
    PollResult,
    SubmitResult,
    VideoSubmitRequest,
    VideoUpstreamError,
)
from .parsing import (
    absolute_url as _absolute_url,
    billable as _billable,
    duration_usage_total_tokens as _duration_usage_total_tokens,
    failure_class as _failure_class,
    http_error as _http_error,
    int_or_none as _int_or_none,
    nested_get as _nested_get,
    provider_task_id as _provider_task_id,
    provider_task_path_segment as _provider_task_path_segment,
    response_json as _response_json,
    status as _status,
    submit_headers as _submit_headers,
    video_url as _video_url,
)
from .runtime import AdapterRuntime, current_runtime


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
            # 同 VolcanoSeedanceAdapter.poll：相对路径必须补齐成绝对 URL。
            # 该 adapter 没有 _client_base_url()，client 也是直接用 provider.base_url。
            video_url=_absolute_url(_video_url(raw), self.provider.base_url),
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


require_http_url = _require_http_url


__all__ = ["DashScopeHappyHorseAdapter", "require_http_url"]
