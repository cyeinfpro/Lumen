"""Async video provider adapters."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from lumen_core.providers import socks_proxy_url
from lumen_core.video_billing import VIDEO_BILLING_TOKENS_PER_SECOND
from lumen_core.video_providers import VideoProviderDefinition

from .config import settings


VideoProviderStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]


@dataclass(frozen=True)
class VideoReferenceMedia:
    kind: Literal["image", "video"]
    data: bytes | None = None
    mime: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class VideoSubmitRequest:
    task_id: str
    user_id: str
    action: Literal["t2v", "i2v", "reference"]
    model: str
    upstream_model: str
    prompt: str
    duration_s: int
    resolution: str
    aspect_ratio: str
    generate_audio: bool = True
    seed: int | None = None
    watermark: bool = False
    input_image_url: str | None = None
    input_image_bytes: bytes | None = None
    input_image_mime: str | None = None
    reference_media: list[VideoReferenceMedia] = field(default_factory=list)
    callback_url: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    provider_task_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class PollResult:
    status: VideoProviderStatus
    progress: int | None = None
    video_url: str | None = None
    failure_class: str | None = None
    usage_total_tokens: int | None = None
    upstream_billable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    accepted: bool
    raw: dict[str, Any] = field(default_factory=dict)


class VideoUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "upstream_unknown",
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.status_code = status_code
        self.raw = raw or {}
        super().__init__(message)


class VideoProviderAdapter(Protocol):
    async def submit(self, req: VideoSubmitRequest) -> SubmitResult: ...

    async def poll(self, provider_task_id: str) -> PollResult: ...

    async def fetch_result(self, video_url: str) -> bytes: ...

    async def cancel(self, provider_task_id: str) -> CancelResult | None: ...


def _nested_get(payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        cur: Any = payload
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur is not None:
            return cur
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _status(raw: Any) -> VideoProviderStatus:
    value = str(raw or "").strip().lower()
    mapping = {
        "queued": "queued",
        "pending": "queued",
        "created": "queued",
        "running": "running",
        "processing": "running",
        "succeeded": "succeeded",
        "success": "succeeded",
        "completed": "succeeded",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "expired": "expired",
    }
    return mapping.get(value, "running")  # type: ignore[return-value]


def _failure_class(payload: dict[str, Any]) -> str | None:
    raw = _nested_get(
        payload,
        ("error", "type"),
        ("error", "code"),
        ("failure_class",),
        ("status_detail",),
    )
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if "policy" in value or "moderation" in value or "safety" in value:
        return "content_policy"
    if "timeout" in value:
        return "timeout"
    if "capacity" in value or "rate" in value:
        return "capacity"
    if "invalid" in value:
        return "invalid_input"
    return value[:64]


def _billable(payload: dict[str, Any]) -> bool | None:
    raw = _nested_get(
        payload,
        ("billable",),
        ("data", "billable"),
        ("result", "billable"),
        ("output", "billable"),
        ("upstream_billable",),
        ("data", "upstream_billable"),
        ("result", "upstream_billable"),
        ("output", "upstream_billable"),
        ("billing", "billable"),
        ("data", "billing", "billable"),
        ("result", "billing", "billable"),
        ("output", "billing", "billable"),
        ("usage", "billable"),
        ("data", "usage", "billable"),
        ("result", "usage", "billable"),
        ("output", "usage", "billable"),
    )
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"true", "1", "yes"}:
            return True
        if value in {"false", "0", "no"}:
            return False
    return None


def _video_url(payload: dict[str, Any]) -> str | None:
    raw = _nested_get(
        payload,
        ("content", "video_url"),
        ("result", "video_url"),
        ("output", "video_url"),
        ("video_url",),
        ("data", "video_url"),
        ("data", "content", "video_url"),
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    output = payload.get("output")
    if isinstance(output, dict):
        results = output.get("results")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                value = _nested_get(item, ("video_url",), ("url",))
                if isinstance(value, str) and value.strip():
                    return value.strip()
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            value = _nested_get(item, ("video_url",), ("video", "url"), ("url",))
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _usage_total_tokens(payload: dict[str, Any]) -> int | None:
    return _int_or_none(
        _nested_get(
            payload,
            ("usage", "completion_tokens"),
            ("data", "usage", "completion_tokens"),
            ("result", "usage", "completion_tokens"),
            ("output", "usage", "completion_tokens"),
            ("usage", "total_tokens"),
            ("data", "usage", "total_tokens"),
            ("result", "usage", "total_tokens"),
            ("output", "usage", "total_tokens"),
            ("usage_total_tokens",),
            ("data", "usage_total_tokens"),
            ("result", "usage_total_tokens"),
            ("output", "usage_total_tokens"),
            ("total_tokens",),
            ("data", "total_tokens"),
            ("result", "total_tokens"),
            ("output", "total_tokens"),
        )
    )


def _duration_usage_total_tokens(payload: dict[str, Any]) -> int | None:
    raw = _nested_get(
        payload,
        ("usage", "duration"),
        ("data", "usage", "duration"),
        ("result", "usage", "duration"),
        ("output", "usage", "duration"),
        ("usage", "output_video_duration"),
        ("data", "usage", "output_video_duration"),
        ("result", "usage", "output_video_duration"),
        ("output", "usage", "output_video_duration"),
        ("output_video_duration",),
        ("data", "output_video_duration"),
        ("result", "output_video_duration"),
        ("output", "output_video_duration"),
        ("duration",),
        ("data", "duration"),
        ("result", "duration"),
        ("output", "duration"),
    )
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        duration = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if duration < 0:
        return None
    tokens = (duration * Decimal(VIDEO_BILLING_TOKENS_PER_SECOND)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(tokens)


def _provider_task_id(payload: dict[str, Any]) -> str | None:
    raw = _nested_get(
        payload,
        ("id",),
        ("task_id",),
        ("data", "id"),
        ("data", "task_id"),
        ("output", "task_id"),
    )
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _image_data_url(data: bytes, mime: str | None) -> str:
    mime_value = (mime or "image/png").strip() or "image/png"
    return f"data:{mime_value};base64,{base64.b64encode(data).decode('ascii')}"


def _safety_identifier(user_id: str) -> str:
    return hashlib.sha256(f"lumen:{user_id}".encode("utf-8")).hexdigest()


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


class VolcanoSeedanceAdapter:
    def __init__(self, provider: VideoProviderDefinition) -> None:
        self.provider = provider

    def _client(self) -> httpx.AsyncClient:
        proxy_url = (
            socks_proxy_url(self.provider.proxy) if self.provider.proxy else None
        )
        timeout = httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=min(settings.upstream_read_timeout_s, 120.0),
            write=settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "base_url": self.provider.base_url,
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {"Authorization": f"Bearer {self.provider.api_key}"},
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        body: dict[str, Any] = {
            "model": req.upstream_model,
            "content": [{"type": "text", "text": req.prompt}],
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
        if req.action == "i2v":
            if not req.input_image_bytes:
                raise VideoUpstreamError(
                    "missing input image bytes",
                    error_code="invalid_input",
                    status_code=422,
                )
            body["content"].append(
                {
                    "type": "image_url",
                    "role": "first_frame",
                    "image_url": {
                        "url": _image_data_url(
                            req.input_image_bytes,
                            req.input_image_mime,
                        )
                    },
                }
            )
        if req.action == "reference":
            image_refs = [item for item in req.reference_media if item.kind == "image"]
            video_refs = [item for item in req.reference_media if item.kind == "video"]
            if not image_refs and not video_refs:
                raise VideoUpstreamError(
                    "reference generation requires reference image or video",
                    error_code="invalid_input",
                    status_code=422,
                )
            if len(image_refs) > 9 or len(video_refs) > 3:
                raise VideoUpstreamError(
                    "too many reference media items",
                    error_code="invalid_input",
                    status_code=422,
                )
            for item in req.reference_media:
                if item.kind == "image":
                    url = item.url
                    if not url:
                        if not item.data:
                            raise VideoUpstreamError(
                                "missing reference image data",
                                error_code="invalid_input",
                                status_code=422,
                            )
                        url = _image_data_url(item.data, item.mime)
                    body["content"].append(
                        {
                            "type": "image_url",
                            "role": "reference_image",
                            "image_url": {"url": url},
                        }
                    )
                elif item.kind == "video":
                    url = item.url
                    if not url:
                        raise VideoUpstreamError(
                            "reference video requires a public URL or asset ID",
                            error_code="invalid_input",
                            status_code=422,
                        )
                    body["content"].append(
                        {
                            "type": "video_url",
                            "role": "reference_video",
                            "video_url": {"url": url},
                        }
                    )
        async with self._client() as client:
            response = await client.post("/contents/generations/tasks", json=body)
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
        async with self._client() as client:
            response = await client.get(
                f"/contents/generations/tasks/{provider_task_id}"
            )
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

    async def fetch_result(self, video_url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_s,
                read=settings.upstream_read_timeout_s,
                write=settings.upstream_write_timeout_s,
                pool=30.0,
            ),
            follow_redirects=True,
        ) as client:
            response = await client.get(video_url)
        if response.status_code >= 400:
            raise VideoUpstreamError(
                f"video fetch failed status={response.status_code}",
                error_code="fetch_failed",
                status_code=response.status_code,
            )
        return bytes(response.content)

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        async with self._client() as client:
            response = await client.delete(
                f"/contents/generations/tasks/{provider_task_id}"
            )
        raw = _response_json(response)
        if response.status_code in {404, 410}:
            return CancelResult(accepted=False, raw=raw)
        if response.status_code >= 400:
            raise _http_error("cancel", response.status_code, raw)
        return CancelResult(accepted=True, raw=raw)


class DashScopeHappyHorseAdapter:
    def __init__(self, provider: VideoProviderDefinition) -> None:
        self.provider = provider

    def _client(self) -> httpx.AsyncClient:
        proxy_url = (
            socks_proxy_url(self.provider.proxy) if self.provider.proxy else None
        )
        timeout = httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=min(settings.upstream_read_timeout_s, 120.0),
            write=settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "base_url": self.provider.base_url,
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "Authorization": f"Bearer {self.provider.api_key}",
                "X-DashScope-Async": "enable",
            },
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        input_payload: dict[str, Any] = {"prompt": req.prompt}
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
        async with self._client() as client:
            response = await client.get(f"/api/v1/tasks/{provider_task_id}")
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
        return PollResult(
            status=status,
            progress=progress,
            video_url=_video_url(raw),
            failure_class=_failure_class(raw),
            usage_total_tokens=usage_tokens,
            upstream_billable=True if status == "succeeded" else _billable(raw),
            raw=raw,
        )

    async def fetch_result(self, video_url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_s,
                read=settings.upstream_read_timeout_s,
                write=settings.upstream_write_timeout_s,
                pool=30.0,
            ),
            follow_redirects=True,
        ) as client:
            response = await client.get(video_url)
        if response.status_code >= 400:
            raise VideoUpstreamError(
                f"video fetch failed status={response.status_code}",
                error_code="fetch_failed",
                status_code=response.status_code,
            )
        return bytes(response.content)

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        # DashScope's documented async task API does not expose a portable
        # cancellation endpoint for HappyHorse. Lumen still marks local cancel
        # requests and settles by the terminal poll result.
        del provider_task_id
        return None


class FakeVideoAdapter:
    """Deterministic local adapter for tests and development."""

    def __init__(self, provider: VideoProviderDefinition) -> None:
        self.provider = provider

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        digest = hashlib.sha256(req.task_id.encode("utf-8")).hexdigest()[:16]
        return SubmitResult(
            provider_task_id=f"fake-video-{digest}", raw={"id": f"fake-video-{digest}"}
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

    async def fetch_result(self, video_url: str) -> bytes:
        del video_url
        # A tiny ftyp+mdat-ish placeholder. Metadata extraction may fail, but
        # storage and API media serving remain testable.
        return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"

    async def cancel(self, provider_task_id: str) -> CancelResult | None:
        return CancelResult(
            accepted=True, raw={"id": provider_task_id, "deleted": True}
        )


def adapter_for_provider(provider: VideoProviderDefinition) -> VideoProviderAdapter:
    if provider.kind == "fake":
        return FakeVideoAdapter(provider)
    if provider.kind == "volcano":
        return VolcanoSeedanceAdapter(provider)
    if provider.kind == "dashscope":
        return DashScopeHappyHorseAdapter(provider)
    raise VideoUpstreamError(
        f"unsupported video provider kind: {provider.kind}",
        error_code="provider_unavailable",
        status_code=503,
    )


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        raw = response.json()
    except ValueError:
        return {"text": response.text[:2000]}
    return raw if isinstance(raw, dict) else {"data": raw}


def _http_error(
    phase: str, status_code: int, raw: dict[str, Any]
) -> VideoUpstreamError:
    code = "upstream_unknown"
    if status_code in {401, 403}:
        code = "upstream_auth_error"
    elif status_code == 408 or status_code == 504:
        code = "upstream_timeout"
    elif status_code == 429:
        code = "capacity"
    elif 400 <= status_code < 500:
        code = "invalid_input"
    elif status_code >= 500:
        code = "provider_error"
    message = _nested_get(raw, ("error", "message"), ("message",), ("text",))
    if not isinstance(message, str) or not message:
        message = f"video upstream {phase} failed status={status_code}"
    return VideoUpstreamError(
        message,
        error_code=code,
        status_code=status_code,
        raw=raw,
    )


__all__ = [
    "CancelResult",
    "DashScopeHappyHorseAdapter",
    "FakeVideoAdapter",
    "PollResult",
    "SubmitResult",
    "VideoProviderAdapter",
    "VideoReferenceMedia",
    "VideoSubmitRequest",
    "VideoUpstreamError",
    "VolcanoSeedanceAdapter",
    "adapter_for_provider",
]
