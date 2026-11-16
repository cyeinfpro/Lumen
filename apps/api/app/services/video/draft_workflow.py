"""Persisted draft workflow validation; never accept upstream task IDs from clients."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from sqlalchemy import select

from lumen_core.model_entities import VideoGeneration, Video
from lumen_core.video_providers import seedance_supports_draft
from .errors import video_http_error

_DRAFT_TTL_CHANGE = datetime(2026, 10, 28, 2, tzinfo=UTC)


def draft_expiry(row: Any) -> datetime | None:
    if not (row.upstream_request or {}).get("draft"):
        return None
    raw = row.upstream_response or {}
    created = raw.get("created_at")
    if created is None and isinstance(raw.get("data"), dict):
        created = raw["data"].get("created_at")
    try:
        start = datetime.fromtimestamp(float(created), UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        # Earlier local timestamp is conservative if the upstream omits created_at.
        candidates = [
            value.replace(tzinfo=UTC) if value.tzinfo is None else value
            for value in (
                getattr(row, "created_at", None),
                getattr(row, "submit_started_at", None),
                getattr(row, "submitted_at", None),
            )
            if isinstance(value, datetime)
        ]
        if not candidates:
            return None
        start = min(candidates)
    return start + timedelta(days=14 if start >= _DRAFT_TTL_CHANGE else 7)


def validate_official_source(source: Any, *, now: datetime | None = None) -> datetime:
    request = source.upstream_request or {}
    if (source.status != "succeeded" or not request.get("draft")
            or not source.provider_task_id or source.provider_kind != "volcano"
            or not seedance_supports_draft(source.model, request.get("upstream_model"))):
        raise video_http_error("draft_source_invalid", "请选择已成功的 Seedance 2.5 官方草稿", 422)
    expiry = draft_expiry(source)
    if expiry is None or expiry <= (now or datetime.now(UTC)):
        raise video_http_error("draft_expired", "草稿已过期，请重新生成草稿", 410)
    return expiry


async def load_workflow_source(db, body, user_id: str):
    source_id = body.draft_generation_id or body.reference_generation_id
    if not source_id:
        return None
    source = (await db.execute(select(VideoGeneration).where(
        VideoGeneration.id == source_id, VideoGeneration.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if source is None:
        raise video_http_error("draft_source_not_found", "找不到草稿任务", 404)
    if body.draft_generation_id:
        validate_official_source(source)
    else:
        if source.status != "succeeded":
            raise video_http_error("reference_draft_not_ready", "参考草稿尚未成功", 409)
        video = (await db.execute(select(Video).where(
            Video.owner_generation_id == source.id, Video.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if video is None or not any(
            item.kind == "video" and item.video_id == video.id
            for item in body.reference_media
        ):
            raise video_http_error("reference_draft_video_missing", "请附加该草稿生成的视频作为参考素材", 422)
    return source


def official_completion_body(body, source):
    # Original media remains billing metadata only: no resubmission or reload.
    return body.model_copy(update={
        "model": source.model, "action": source.action, "prompt": source.prompt,
        "input_image_id": source.input_image_id, "reference_media": [],
        "duration_s": source.duration_s, "aspect_ratio": source.aspect_ratio,
        "seed": source.seed, "generate_audio": source.generate_audio,
        "resolution": "1080p", "draft": False,
    })


def workflow_metadata(body, source) -> dict[str, Any]:
    result = {
        "draft": body.draft, "draft_generation_id": body.draft_generation_id,
        "reference_draft": body.reference_draft,
        "reference_generation_id": body.reference_generation_id,
    }
    if body.draft_generation_id and source is not None:
        req = source.upstream_request or {}
        result.update({
            "draft_task_id": source.provider_task_id,
            "draft_expires_at": validate_official_source(source).isoformat(),
            "source_provider_name": source.provider_name,
            "source_upstream_model": req.get("upstream_model"),
            "source_provider_snapshot": dict(req.get("provider_snapshot") or {}),
        })
    return result
