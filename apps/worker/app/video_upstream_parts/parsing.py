"""Provider response parsing and common request/error helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from lumen_core.video_billing import VIDEO_BILLING_TOKENS_PER_SECOND

from .contracts import (
    VideoSubmitRequest,
    VideoUpstreamError,
    VideoProviderStatus,
)


def _provider_task_path_segment(provider_task_id: str) -> str:
    value = provider_task_id.strip()
    if not value:
        raise VideoUpstreamError(
            "video provider task id is empty",
            error_code="bad_response",
            status_code=502,
        )
    return quote(value, safe="")


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
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _status(raw: Any) -> VideoProviderStatus:
    value = str(raw or "").strip().lower()
    mapping: dict[str, VideoProviderStatus] = {
        "queued": "queued",
        "pending": "queued",
        "created": "queued",
        "running": "running",
        "processing": "running",
        "in_progress": "running",
        "waiting": "running",
        "waiting_for_capacity": "running",
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
    return mapping.get(value, "failed")


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
    if any(
        marker in value for marker in ("policy", "moderation", "safety", "sensitive")
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


_VIDEO_URL_PATHS = (
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

_EXPLICIT_VIDEO_URL_PATHS = (
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

_VIDEO_URL_COLLECTION_PATHS = (
    ("data", "video_urls"),
    ("data", "data", "video_urls"),
    ("result", "video_urls"),
    ("output", "video_urls"),
    ("data", "videos"),
    ("data", "data", "videos"),
    ("result", "videos"),
    ("output", "videos"),
    ("data", "outputs"),
    ("data", "data", "outputs"),
    ("result", "outputs"),
    ("output", "outputs"),
)

_VIDEO_URL_ITEM_PATHS = (
    ("video_url",),
    ("url",),
    ("content", "video_url"),
)


def _clean_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_nested_string(
    payload: dict[str, Any],
    paths: tuple[tuple[str, ...], ...],
) -> str | None:
    return _clean_string(_nested_get(payload, *paths))


def _first_collection_url(
    values: Any,
    *,
    allow_strings: bool,
    item_paths: tuple[tuple[str, ...], ...],
) -> str | None:
    if not isinstance(values, list):
        return None
    for item in values:
        if allow_strings:
            direct = _clean_string(item)
            if direct:
                return direct
        if isinstance(item, dict):
            nested = _clean_string(_nested_get(item, *item_paths))
            if nested:
                return nested
    return None


def _video_url(payload: dict[str, Any]) -> str | None:
    direct = _first_nested_string(payload, _VIDEO_URL_PATHS)
    if direct:
        return direct
    collections = [
        (_nested_get(payload, ("output", "results")), False),
        (payload.get("results"), False),
        (_nested_get(payload, ("data", "results"), ("data", "data", "results")), False),
        (payload.get("video_urls"), True),
        *((_nested_get(payload, path), True) for path in _VIDEO_URL_COLLECTION_PATHS),
    ]
    for values, allow_strings in collections:
        result = _first_collection_url(
            values,
            allow_strings=allow_strings,
            item_paths=_VIDEO_URL_ITEM_PATHS,
        )
        if result:
            return result
    content = payload.get("content")
    return _first_collection_url(
        content,
        allow_strings=False,
        item_paths=(("video_url",), ("video", "url"), ("url",)),
    )


def _explicit_video_result_url(payload: dict[str, Any]) -> str | None:
    direct = _first_nested_string(payload, _EXPLICIT_VIDEO_URL_PATHS)
    if direct:
        return direct
    collections = [
        (payload.get("video_urls"), True),
        *((_nested_get(payload, path), True) for path in _VIDEO_URL_COLLECTION_PATHS),
        (payload.get("results"), False),
        (_nested_get(payload, ("data", "results"), ("data", "data", "results")), False),
    ]
    for values, allow_strings in collections:
        result = _first_collection_url(
            values,
            allow_strings=allow_strings,
            item_paths=_VIDEO_URL_ITEM_PATHS,
        )
        if result:
            return result
    return _first_collection_url(
        payload.get("content"),
        allow_strings=False,
        item_paths=(("video_url",), ("video", "url"), ("url",)),
    )


def _absolute_url(raw_url: str | None, base_url: str) -> str | None:
    value = _clean_string(raw_url)
    if not value:
        return None
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
    if not duration.is_finite() or duration < 0:
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
    return _clean_string(raw)


def _submit_headers(req: VideoSubmitRequest) -> dict[str, str]:
    idempotency_key = req.idempotency_key or req.task_id
    return {
        "Idempotency-Key": idempotency_key,
        "X-Request-ID": idempotency_key,
        "X-Lumen-Task-ID": req.task_id,
    }


def _safety_identifier(user_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"lumen:{user_id}".encode("utf-8")).hexdigest()


def _response_json(response: Any) -> dict[str, Any]:
    try:
        raw = response.json()
    except ValueError:
        return {"text": response.text[:2000]}
    return raw if isinstance(raw, dict) else {"data": raw}


def _http_error(
    phase: str,
    status_code: int,
    raw: dict[str, Any],
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
    elif status_code in {408, 504}:
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
    "_absolute_url",
    "_billable",
    "_collapse_url_path_slashes",
    "_duration_usage_total_tokens",
    "_explicit_video_result_url",
    "_failure_class",
    "_http_error",
    "_int_or_none",
    "_nested_get",
    "_provider_task_id",
    "_provider_task_path_segment",
    "_response_json",
    "_safety_identifier",
    "_status",
    "_submit_headers",
    "_usage_total_tokens",
    "_video_url",
]
