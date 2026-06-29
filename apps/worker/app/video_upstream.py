"""Async video provider adapters."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from lumen_core.providers import socks_proxy_url
from lumen_core.url_security import resolve_public_http_target
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
    label: str | None = None
    ref_id: str | None = None


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
    if isinstance(value, str):
        value = value.strip()
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        parsed = int(float(value)) if isinstance(value, str) else int(value)
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
        "in_progress": "running",
        "succeeded": "succeeded",
        "success": "succeeded",
        "completed": "succeeded",
        "complete": "succeeded",
        "done": "succeeded",
        "finished": "succeeded",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
        "rejected": "failed",
        "reject": "failed",
        "blocked": "failed",
        "moderation_failed": "failed",
        "content_filtered": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "expired": "expired",
    }
    if not value:
        return "running"
    return mapping.get(value, "failed")  # type: ignore[return-value]


def _failure_class(payload: dict[str, Any]) -> str | None:
    raw = _nested_get(
        payload,
        ("error", "type"),
        ("error", "code"),
        ("data", "data", "error", "type"),
        ("data", "data", "error", "code"),
        ("data", "data", "data", "error", "type"),
        ("data", "data", "data", "error", "code"),
        ("data", "fail_reason"),
        ("failure_class",),
        ("status_detail",),
    )
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if (
        "policy" in value
        or "moderation" in value
        or "safety" in value
        or "sensitive" in value
    ):
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
        ("data", "data", "billable"),
        ("data", "data", "data", "billable"),
        ("result", "billable"),
        ("output", "billable"),
        ("upstream_billable",),
        ("data", "upstream_billable"),
        ("data", "data", "upstream_billable"),
        ("data", "data", "data", "upstream_billable"),
        ("result", "upstream_billable"),
        ("output", "upstream_billable"),
        ("billing", "billable"),
        ("data", "billing", "billable"),
        ("data", "data", "billing", "billable"),
        ("data", "data", "data", "billing", "billable"),
        ("result", "billing", "billable"),
        ("output", "billing", "billable"),
        ("usage", "billable"),
        ("data", "usage", "billable"),
        ("data", "data", "usage", "billable"),
        ("data", "data", "data", "usage", "billable"),
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
        ("result", "url"),
        ("output", "video_url"),
        ("output", "url"),
        ("video_url",),
        ("url",),
        ("data", "video_url"),
        ("data", "url"),
        ("data", "content", "video_url"),
        ("data", "data", "video_url"),
        ("data", "data", "url"),
        ("data", "data", "content", "video_url"),
        ("data", "data", "data", "video_url"),
        ("data", "data", "data", "url"),
        ("data", "data", "data", "content", "video_url"),
        ("data", "data", "url"),
        ("data", "data", "result_url"),
        ("data", "data", "data", "url"),
        ("data", "data", "data", "result_url"),
        ("data", "result_url"),
        ("metadata", "url"),
        ("metadata", "fetch_url"),
        ("data", "metadata", "url"),
        ("data", "metadata", "fetch_url"),
        ("data", "data", "metadata", "url"),
        ("data", "data", "metadata", "fetch_url"),
        ("data", "data", "data", "metadata", "url"),
        ("data", "data", "data", "metadata", "fetch_url"),
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
    for results in (
        payload.get("results"),
        _nested_get(payload, ("data", "results"), ("data", "data", "results")),
    ):
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                value = _nested_get(item, ("video_url",), ("url",))
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for values in (
        payload.get("video_urls"),
        _nested_get(
            payload,
            ("data", "video_urls"),
            ("data", "data", "video_urls"),
            ("result", "video_urls"),
            ("output", "video_urls"),
        ),
        _nested_get(
            payload,
            ("data", "videos"),
            ("data", "data", "videos"),
            ("result", "videos"),
            ("output", "videos"),
        ),
        _nested_get(
            payload,
            ("data", "outputs"),
            ("data", "data", "outputs"),
            ("result", "outputs"),
            ("output", "outputs"),
        ),
    ):
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    value = _nested_get(
                        item, ("video_url",), ("url",), ("content", "video_url")
                    )
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


def _explicit_video_result_url(payload: dict[str, Any]) -> str | None:
    raw = _nested_get(
        payload,
        ("content", "video_url"),
        ("result", "video_url"),
        ("result", "url"),
        ("output", "video_url"),
        ("output", "url"),
        ("video_url",),
        ("data", "video_url"),
        ("data", "content", "video_url"),
        ("data", "data", "video_url"),
        ("data", "data", "content", "video_url"),
        ("data", "data", "data", "video_url"),
        ("data", "data", "data", "content", "video_url"),
        ("data", "result_url"),
        ("data", "data", "result_url"),
        ("data", "data", "data", "result_url"),
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    for values in (
        _nested_get(
            payload,
            ("video_urls",),
            ("data", "video_urls"),
            ("data", "data", "video_urls"),
            ("result", "video_urls"),
            ("output", "video_urls"),
        ),
        _nested_get(
            payload,
            ("data", "videos"),
            ("data", "data", "videos"),
            ("result", "videos"),
            ("output", "videos"),
        ),
        _nested_get(
            payload,
            ("data", "outputs"),
            ("data", "data", "outputs"),
            ("result", "outputs"),
            ("output", "outputs"),
        ),
    ):
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    value = _nested_get(
                        item, ("video_url",), ("url",), ("content", "video_url")
                    )
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    for results in (
        payload.get("results"),
        _nested_get(payload, ("data", "results"), ("data", "data", "results")),
    ):
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


def _absolute_url(raw_url: str | None, base_url: str) -> str | None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    value = raw_url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() in {"http", "https"} and parts.hostname:
        return value
    return urljoin(f"{_collapse_url_path_slashes(base_url).rstrip('/')}/", value)


def _collapse_url_path_slashes(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    path = parts.path
    while "//" in path:
        path = path.replace("//", "/")
    return parts._replace(path=path.rstrip("/")).geturl()


def _usage_total_tokens(payload: dict[str, Any]) -> int | None:
    return _int_or_none(
        _nested_get(
            payload,
            ("usage", "completion_tokens"),
            ("data", "usage", "completion_tokens"),
            ("data", "data", "usage", "completion_tokens"),
            ("data", "data", "data", "usage", "completion_tokens"),
            ("result", "usage", "completion_tokens"),
            ("output", "usage", "completion_tokens"),
            ("usage", "total_tokens"),
            ("data", "usage", "total_tokens"),
            ("data", "data", "usage", "total_tokens"),
            ("data", "data", "data", "usage", "total_tokens"),
            ("result", "usage", "total_tokens"),
            ("output", "usage", "total_tokens"),
            ("usage_total_tokens",),
            ("data", "usage_total_tokens"),
            ("data", "data", "usage_total_tokens"),
            ("data", "data", "data", "usage_total_tokens"),
            ("result", "usage_total_tokens"),
            ("output", "usage_total_tokens"),
            ("total_tokens",),
            ("data", "total_tokens"),
            ("data", "data", "total_tokens"),
            ("data", "data", "data", "total_tokens"),
            ("result", "total_tokens"),
            ("output", "total_tokens"),
        )
    )


def _duration_usage_total_tokens(payload: dict[str, Any]) -> int | None:
    raw = _nested_get(
        payload,
        ("usage", "duration"),
        ("data", "usage", "duration"),
        ("data", "data", "usage", "duration"),
        ("data", "data", "data", "usage", "duration"),
        ("result", "usage", "duration"),
        ("output", "usage", "duration"),
        ("usage", "output_video_duration"),
        ("data", "usage", "output_video_duration"),
        ("data", "data", "usage", "output_video_duration"),
        ("data", "data", "data", "usage", "output_video_duration"),
        ("result", "usage", "output_video_duration"),
        ("output", "usage", "output_video_duration"),
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
        ("taskId",),
        ("data", "id"),
        ("data", "task_id"),
        ("data", "taskId"),
        ("result", "id"),
        ("result", "task_id"),
        ("result", "taskId"),
        ("output", "task_id"),
        ("output", "taskId"),
    )
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _image_data_url(data: bytes, mime: str | None) -> str:
    mime_value = (mime or "image/png").strip() or "image/png"
    return f"data:{mime_value};base64,{base64.b64encode(data).decode('ascii')}"


_OMNI_FALLBACK_IMAGE_MAX_BYTES = 64 * 1024 * 1024
_SEEDANCE_INLINE_IMAGE_MAX_BYTES = 12 * 1024 * 1024
_VIDEO_FETCH_MAX_BYTES = 2 * 1024 * 1024 * 1024
_VIDEO_FETCH_MIN_MAGIC_BYTES = 12


def _submit_headers(req: VideoSubmitRequest) -> dict[str, str]:
    return {
        "Idempotency-Key": req.task_id,
        "X-Request-ID": req.task_id,
        "X-Lumen-Task-ID": req.task_id,
    }


def _image_response_mime(response: httpx.Response, fallback: str | None) -> str | None:
    fallback_value = fallback.strip() if isinstance(fallback, str) else ""
    raw = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if raw:
        if raw.startswith("image/"):
            return raw
        if fallback_value.startswith("image/"):
            return fallback_value
        raise VideoUpstreamError(
            "Omni Flash fallback image URL did not return an image",
            error_code="invalid_input",
            status_code=response.status_code,
        )
    return fallback_value or None


def _looks_like_iso_bmff_video(data: bytes) -> bool:
    return len(data) >= _VIDEO_FETCH_MIN_MAGIC_BYTES and data[4:8] == b"ftyp"


def _validate_video_response_bytes(data: bytes, content_type: str) -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type.startswith("video/"):
        return
    if media_type in {"application/octet-stream", "binary/octet-stream", ""}:
        if _looks_like_iso_bmff_video(data):
            return
    raise VideoUpstreamError(
        "video fetch response was not a video",
        error_code="fetch_failed",
        status_code=502,
        raw={"content_type": media_type or None},
    )


async def _fetch_video_url_bytes(video_url: str) -> bytes:
    try:
        target = await resolve_public_http_target(video_url, allow_http=True)
    except ValueError as exc:
        raise VideoUpstreamError(
            "video result URL must be public HTTP(S)",
            error_code="invalid_input",
            status_code=422,
        ) from exc
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            write=settings.upstream_write_timeout_s,
            pool=30.0,
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        async with client.stream("GET", target.url) as response:
            if response.status_code >= 400:
                raise VideoUpstreamError(
                    f"video fetch failed status={response.status_code}",
                    error_code="fetch_failed",
                    status_code=response.status_code,
                )
            content_length = response.headers.get("content-length")
            if content_length:
                parsed_length = _int_or_none(content_length)
                if parsed_length is not None and parsed_length > _VIDEO_FETCH_MAX_BYTES:
                    raise VideoUpstreamError(
                        "video fetch response exceeds maximum size",
                        error_code="fetch_failed",
                        status_code=413,
                    )
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _VIDEO_FETCH_MAX_BYTES:
                    raise VideoUpstreamError(
                        "video fetch response exceeds maximum size",
                        error_code="fetch_failed",
                        status_code=413,
                    )
                chunks.append(bytes(chunk))
            data = b"".join(chunks)
            if not data:
                raise VideoUpstreamError(
                    "video fetch response was empty",
                    error_code="fetch_failed",
                    status_code=response.status_code,
                )
            _validate_video_response_bytes(
                data, response.headers.get("content-type", "")
            )
            return data


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
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream("GET", target.url) as response:
            if response.status_code >= 300:
                raise VideoUpstreamError(
                    f"{field} fallback fetch failed status={response.status_code}",
                    error_code="invalid_input",
                    status_code=response.status_code,
                )
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    expected_bytes = int(content_length)
                except ValueError:
                    expected_bytes = 0
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
    return _image_data_url(data, mime)


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


def _clean_reference_label(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    if not value:
        return None
    return value[:80]


def _reference_anchor_token(kind: str, index: int, ref_id: str | None = None) -> str:
    clean = (ref_id or "").strip().lower()
    parts = clean.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and int(parts[2]) > 0
    ):
        return f"[{clean}]"
    return f"[ref:{kind}:{index}]"


def _reference_order_aliases(
    *,
    kind: Literal["image", "video"],
    index: int,
    label: str | None,
    official: str,
    localized: str,
    anchor: str,
) -> list[str]:
    aliases: list[str] = []
    zh_digits = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
    }
    noun = "图片" if kind == "image" else "视频"
    short_noun = "图" if kind == "image" else "视频"
    for alias in (
        anchor,
        anchor.strip("[]"),
        _clean_reference_label(label),
        localized,
        f"[{localized}]",
        f"{noun}{index}",
        f"{short_noun}{index}",
        f"视频素材{index}" if kind == "video" else None,
        f"视频素材 {index}" if kind == "video" else None,
        f"参考视频{index}" if kind == "video" else None,
        f"参考视频 {index}" if kind == "video" else None,
        f"动作参考{index}" if kind == "video" else None,
        f"动作参考 {index}" if kind == "video" else None,
        f"运动参考{index}" if kind == "video" else None,
        f"运动参考 {index}" if kind == "video" else None,
        f"第{index}张{noun}" if kind == "image" else f"第{index}个{noun}",
        f"第{index}张{short_noun}" if kind == "image" else f"第{index}段{noun}",
        f"第{index}段素材" if kind == "video" else None,
        f"第{index}个视频素材" if kind == "video" else None,
        f"第{zh_digits[index]}张{noun}"
        if index in zh_digits and kind == "image"
        else None,
        f"第{zh_digits[index]}张{short_noun}"
        if index in zh_digits and kind == "image"
        else None,
        f"第{zh_digits[index]}个{noun}"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}段{noun}"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}段素材"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}个视频素材"
        if index in zh_digits and kind == "video"
        else None,
    ):
        if alias and alias not in aliases and alias != official:
            aliases.append(alias)
    return aliases


def _prompt_with_reference_order(req: VideoSubmitRequest) -> str:
    if req.action != "reference" or not req.reference_media:
        return req.prompt

    lines: list[str] = []
    image_index = 0
    video_index = 0
    for item in req.reference_media:
        if item.kind == "image":
            image_index += 1
            official = f"Image {image_index}"
            localized = f"图片 {image_index}"
            description = f"reference image #{image_index}"
            anchor = _reference_anchor_token("image", image_index, item.ref_id)
        elif item.kind == "video":
            video_index += 1
            official = f"Video {video_index}"
            localized = f"视频 {video_index}"
            description = f"reference video #{video_index}"
            anchor = _reference_anchor_token("video", video_index, item.ref_id)
        else:
            continue

        aliases = _reference_order_aliases(
            kind=item.kind,
            index=image_index if item.kind == "image" else video_index,
            label=item.label,
            official=official,
            localized=localized,
            anchor=anchor,
        )
        alias_text = f"; user-prompt aliases: {', '.join(aliases)}" if aliases else ""
        lines.append(
            f"- {official}: {description} in the content array; stable anchor: "
            f"{anchor}{alias_text}."
        )

    if not lines:
        return req.prompt

    return (
        "Reference asset contract for this video request. Interpret the user's "
        "asset mentions by the stable anchors and official type + number below. "
        "If the user prompt includes an anchor such as [ref:image:1], bind that "
        "instruction only to the matching reference asset:\n"
        + "\n".join(lines)
        + "\n\nUser prompt:\n"
        + req.prompt
    )


def _seedance_content(
    req: VideoSubmitRequest,
    *,
    allow_input_image_url: bool = False,
    include_reference_order_prompt: bool = False,
) -> list[dict[str, Any]]:
    prompt = (
        _prompt_with_reference_order(req)
        if include_reference_order_prompt
        else req.prompt
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if req.action == "i2v":
        image_url = req.input_image_url if allow_input_image_url else None
        if not image_url:
            if not req.input_image_bytes:
                raise VideoUpstreamError(
                    "missing input image bytes",
                    error_code="invalid_input",
                    status_code=422,
                )
            if len(req.input_image_bytes) > _SEEDANCE_INLINE_IMAGE_MAX_BYTES:
                raise VideoUpstreamError(
                    "input image is too large for inline video submission",
                    error_code="invalid_input",
                    status_code=413,
                )
            image_url = _image_data_url(req.input_image_bytes, req.input_image_mime)
        content.append(
            {
                "type": "image_url",
                "role": "first_frame",
                "image_url": {"url": image_url},
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
                content.append(
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
                content.append(
                    {
                        "type": "video_url",
                        "role": "reference_video",
                        "video_url": {"url": url},
                    }
                )
    return content


class VolcanoSeedanceAdapter:
    def __init__(self, provider: VideoProviderDefinition) -> None:
        self.provider = provider

    def _client_base_url(self) -> str:
        return self.provider.base_url

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
            "base_url": self._client_base_url(),
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
            "headers": {"Authorization": f"Bearer {self.provider.api_key}"},
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    async def submit(self, req: VideoSubmitRequest) -> SubmitResult:
        body: dict[str, Any] = {
            "model": req.upstream_model,
            "content": _seedance_content(req, include_reference_order_prompt=True),
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
        return await _fetch_video_url_bytes(video_url)

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
            "content": _seedance_content(
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
        async with self._client() as client:
            response = await client.get(
                self._path(f"video/generations/{provider_task_id}")
            )
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
        async with self._client() as client:
            response = await client.delete(
                self._path(f"video/generations/{provider_task_id}")
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
        return urljoin(
            f"{self._client_base_url().rstrip('/')}/",
            self._path(f"videos/{provider_task_id}/content"),
        )

    def _media_url(self, item: VideoReferenceMedia, *, field: str) -> str:
        if item.url:
            return item.url
        if item.kind == "image" and item.data:
            if len(item.data) > _SEEDANCE_INLINE_IMAGE_MAX_BYTES:
                raise VideoUpstreamError(
                    f"{field} is too large for inline video submission",
                    error_code="invalid_input",
                    status_code=413,
                )
            return _image_data_url(item.data, item.mime)
        raise VideoUpstreamError(
            f"{field} requires a public URL or base64 image",
            error_code="invalid_input",
            status_code=422,
        )

    def _reference_media_arrays(self, req: VideoSubmitRequest) -> tuple[list[str], list[str]]:
        images: list[str] = []
        videos: list[str] = []
        if req.action == "i2v":
            if req.input_image_url:
                images.append(req.input_image_url)
            elif req.input_image_bytes:
                if len(req.input_image_bytes) > _SEEDANCE_INLINE_IMAGE_MAX_BYTES:
                    raise VideoUpstreamError(
                        "input image is too large for inline video submission",
                        error_code="invalid_input",
                        status_code=413,
                    )
                images.append(_image_data_url(req.input_image_bytes, req.input_image_mime))
            else:
                raise VideoUpstreamError(
                    "missing input image bytes",
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
        return images, videos

    def _submit_body(self, req: VideoSubmitRequest) -> dict[str, Any]:
        images, videos = self._reference_media_arrays(req)
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
        async with self._client() as client:
            response = await client.get(self._path(f"videos/{provider_task_id}"))
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
        video_url = _absolute_url(_explicit_video_result_url(raw), self._client_base_url())
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

    def _raw_client(self) -> httpx.AsyncClient:
        proxy_url = (
            socks_proxy_url(self.provider.proxy) if self.provider.proxy else None
        )
        timeout = httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            write=settings.upstream_write_timeout_s,
            pool=30.0,
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    async def fetch_result(self, video_url: str) -> bytes:
        current_url = video_url
        for _redirect in range(self._MAX_CONTENT_REDIRECTS + 1):
            try:
                target = await resolve_public_http_target(current_url, allow_http=True)
            except ValueError as exc:
                raise VideoUpstreamError(
                    "video result URL must be public HTTP(S)",
                    error_code="invalid_input",
                    status_code=422,
                ) from exc
            async with self._raw_client() as client:
                async with client.stream(
                    "GET",
                    target.url,
                    headers=self._content_request_headers(target.url),
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoUpstreamError(
                                "video fetch redirect did not include a location",
                                error_code="fetch_failed",
                                status_code=response.status_code,
                            )
                        current_url = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        raise VideoUpstreamError(
                            f"video fetch failed status={response.status_code}",
                            error_code="fetch_failed",
                            status_code=response.status_code,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        parsed_length = _int_or_none(content_length)
                        if (
                            parsed_length is not None
                            and parsed_length > _VIDEO_FETCH_MAX_BYTES
                        ):
                            raise VideoUpstreamError(
                                "video fetch response exceeds maximum size",
                                error_code="fetch_failed",
                                status_code=413,
                            )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _VIDEO_FETCH_MAX_BYTES:
                            raise VideoUpstreamError(
                                "video fetch response exceeds maximum size",
                                error_code="fetch_failed",
                                status_code=413,
                            )
                        chunks.append(bytes(chunk))
                    data = b"".join(chunks)
                    if not data:
                        raise VideoUpstreamError(
                            "video fetch response was empty",
                            error_code="fetch_failed",
                            status_code=response.status_code,
                        )
                    _validate_video_response_bytes(
                        data, response.headers.get("content-type", "")
                    )
                    return data
        raise VideoUpstreamError(
            "video fetch exceeded redirect limit",
            error_code="fetch_failed",
            status_code=508,
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
        return _image_data_url(item.data, item.mime)

    async def _media_data_url(self, item: VideoReferenceMedia, *, field: str) -> str:
        if item.kind != "image":
            raise VideoUpstreamError(
                "Omni Flash unified video create supports image references only",
                error_code="invalid_input",
                status_code=422,
            )
        if item.data:
            return _image_data_url(item.data, item.mime)
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
        return await _fetch_image_url_as_data_url(
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
            return [_image_data_url(req.input_image_bytes, req.input_image_mime)]
        if req.action == "reference":
            image_refs = self._reference_image_refs(req)
            return [
                self._media_url(item, field="Omni Flash reference image")
                for item in image_refs
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
                return [_image_data_url(req.input_image_bytes, req.input_image_mime)]
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
            image_refs = self._reference_image_refs(req)
            return [
                await self._media_data_url(
                    item,
                    field="Omni Flash reference image",
                )
                for item in image_refs
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
        self, path: str, body: dict[str, Any], req: VideoSubmitRequest
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
        async with self._client() as client:
            response = await client.get(
                self._path(f"video/generations/{provider_task_id}")
            )
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
            "follow_redirects": False,
            "trust_env": False,
            "headers": {
                "Authorization": f"Bearer {self.provider.api_key}",
                "X-DashScope-Async": "enable",
            },
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

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
        return await _fetch_video_url_bytes(video_url)

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
    message = _nested_get(
        raw,
        ("error", "message"),
        ("error",),
        ("message",),
        ("text",),
    )
    if not isinstance(message, str) or not message:
        message = f"video upstream {phase} failed status={status_code}"
    if status_code in {401, 403}:
        code = "upstream_auth_error"
    elif status_code == 408 or status_code == 504:
        code = "upstream_timeout"
    elif status_code == 429:
        code = "capacity"
    elif phase == "poll" and status_code == 404:
        code = "upstream_not_ready"
    elif _is_upstream_model_unavailable_message(message):
        code = "provider_unavailable"
    elif 400 <= status_code < 500:
        code = "invalid_input"
    elif status_code >= 500:
        code = "provider_error"
    return VideoUpstreamError(
        message,
        error_code=code,
        status_code=status_code,
        raw=raw,
    )


def _is_upstream_model_unavailable_message(message: str) -> bool:
    value = message.strip().lower()
    return any(
        marker in value
        for marker in (
            "model_not_found",
            "no available channel for model",
            "不是 gemini 原生 api 格式的有效 gemini 模型",
            "not a valid gemini model",
        )
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
