"""Video response-model construction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import Video, VideoGeneration
from lumen_core.schemas import (
    MoneyOut,
    VideoGenerationOut,
    VideoOut,
    VideoReferenceMediaOut,
    VideoTemporaryDownloadOut,
)
from lumen_core.url_security import is_private_host


_TEMPORARY_DOWNLOAD_MIN_REMAINING = timedelta(seconds=60)


def money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(int(amount_micro)))


def video_binary_url(video_id: str) -> str:
    return f"/api/videos/{video_id}/binary"


def video_poster_url(video_id: str, poster_storage_key: str | None) -> str | None:
    return f"/api/videos/{video_id}/poster" if poster_storage_key else None


def _nested_text(raw: Any, path: tuple[str, ...]) -> str | None:
    current = raw
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current.strip() if isinstance(current, str) and current.strip() else None


def _upstream_video_url(raw: Any) -> str | None:
    for path in (
        ("content", "video_url"),
        ("data", "content", "video_url"),
        ("data", "data", "content", "video_url"),
        ("data", "data", "data", "content", "video_url"),
    ):
        value = _nested_text(raw, path)
        if value:
            return value
    return None


def temporary_video_download_out(
    row: VideoGeneration,
    *,
    now: datetime | None = None,
) -> VideoTemporaryDownloadOut | None:
    if row.provider_kind != "volcano":
        return None
    raw_url = _upstream_video_url(row.upstream_response)
    if not raw_url:
        return None
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or is_private_host(parsed.hostname)
    ):
        return None
    host = parsed.hostname.rstrip(".").lower()
    if host != "volces.com" and not host.endswith(".volces.com"):
        return None
    query = parse_qs(parsed.query, keep_blank_values=False)
    algorithm = (query.get("X-Tos-Algorithm") or [""])[0]
    signature = (query.get("X-Tos-Signature") or [""])[0]
    signed_at_raw = (query.get("X-Tos-Date") or [""])[0]
    expires_raw = (query.get("X-Tos-Expires") or [""])[0]
    if algorithm != "TOS4-HMAC-SHA256" or not signature:
        return None
    try:
        expires_s = int(expires_raw)
        if expires_s <= 0:
            return None
        signed_at = datetime.strptime(signed_at_raw, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None
    expires_at = signed_at + timedelta(seconds=expires_s)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    remaining_s = int((expires_at - current.astimezone(timezone.utc)).total_seconds())
    if remaining_s <= int(_TEMPORARY_DOWNLOAD_MIN_REMAINING.total_seconds()):
        return None
    return VideoTemporaryDownloadOut(
        source="volcano",
        url=raw_url,
        expires_at=expires_at,
        expires_in_s=remaining_s,
    )


def generation_elapsed_ms(
    row: VideoGeneration,
    *,
    now: datetime | None = None,
) -> int | None:
    started_at = row.created_at
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    else:
        started_at = started_at.astimezone(timezone.utc)
    finished_at = row.finished_at
    if finished_at is None:
        finished_at = now or datetime.now(timezone.utc)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    else:
        finished_at = finished_at.astimezone(timezone.utc)
    elapsed_s = max(0.0, (finished_at - started_at).total_seconds())
    return int(elapsed_s * 1000)


def video_out(video: Video) -> VideoOut:
    return VideoOut(
        id=video.id,
        url=video_binary_url(video.id),
        poster_url=video_poster_url(video.id, video.poster_storage_key),
        width=video.width,
        height=video.height,
        duration_ms=video.duration_ms,
        fps=video.fps,
        has_audio=video.has_audio,
        mime=video.mime,
        size_bytes=video.size_bytes,
        faststart=video.faststart,
        created_at=video.created_at,
    )


def is_internal_reference_url(raw_url: str | None) -> bool:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return False
    path = urlsplit(raw_url).path
    return path.startswith("/api/images/reference/") or path.startswith(
        "/api/videos/reference/"
    )


def reference_media_out(
    snapshot: dict[str, Any],
) -> VideoReferenceMediaOut | None:
    kind = snapshot.get("kind")
    if kind not in {"image", "video", "audio"}:
        return None
    raw_url = snapshot.get("url") if isinstance(snapshot.get("url"), str) else None
    url = None if is_internal_reference_url(raw_url) else raw_url
    return VideoReferenceMediaOut(
        kind=kind,
        image_id=snapshot.get("image_id")
        if isinstance(snapshot.get("image_id"), str)
        else None,
        video_id=snapshot.get("video_id")
        if isinstance(snapshot.get("video_id"), str)
        else None,
        url=url,
        label=snapshot.get("label") if isinstance(snapshot.get("label"), str) else None,
        ref_id=snapshot.get("ref_id")
        if isinstance(snapshot.get("ref_id"), str)
        else None,
        mime=snapshot.get("mime") if isinstance(snapshot.get("mime"), str) else None,
    )


def _public_retry_error(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "at",
        "attempt",
        "poll_count",
        "error_code",
        "retryable",
        "terminal",
        "next_retry_delay_s",
    ):
        value = raw.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            out[key] = value
    return out


def _public_retry_history(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [_public_retry_error(item) for item in raw if isinstance(item, dict)][-5:]


def public_video_diagnostics(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "billing_decision",
        "pricing_variant",
        "billing_model",
        "requested_model",
        "reference_media_count",
        "reference_image_public_variant",
        "reference_video_public_variant",
        "reference_image_public_variant_error_count",
        "reference_video_public_variant_error_count",
        "requires_public_media",
        "prefers_public_media_url",
        "reference_public_media_url_enabled",
        "submit_retry_count",
        "poll_retry_count",
        "deadline_expired_polling_continues",
        "extended_polling_continues",
        "extended_poll_delay_s",
        "max_poll_count",
        "max_poll_duration_s",
        "max_provider_poll_duration_s",
        "poll_elapsed_s",
        "faststart",
        "ffmpeg_missing",
    ):
        value = raw.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            out[key] = value
    for key in ("last_submit_error", "last_poll_error"):
        safe = _public_retry_error(raw.get(key))
        if safe:
            out[key] = safe
    for key in ("submit_retry_history", "poll_retry_history"):
        safe_history = _public_retry_history(raw.get(key))
        if safe_history:
            out[key] = safe_history
    return out


def generation_reference_media(
    row: VideoGeneration,
) -> list[VideoReferenceMediaOut]:
    raw = (row.upstream_request or {}).get("reference_media")
    if not isinstance(raw, list):
        return []
    out: list[VideoReferenceMediaOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reference = reference_media_out(item)
        if reference is not None:
            out.append(reference)
    return out


async def video_for_generation(
    db: AsyncSession,
    video_generation_id: str,
) -> Video | None:
    return (
        await db.execute(
            select(Video).where(
                Video.owner_generation_id == video_generation_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def generation_out(
    db: AsyncSession,
    row: VideoGeneration,
    video: Video | None = None,
) -> VideoGenerationOut:
    if video is None:
        video = await video_for_generation(db, row.id)
    return VideoGenerationOut(
        id=row.id,
        action=row.action,
        model=row.model,
        prompt=row.prompt,
        input_image_id=row.input_image_id,
        reference_media=generation_reference_media(row),
        duration_s=row.duration_s,
        resolution=row.resolution,
        aspect_ratio=row.aspect_ratio,
        fps=row.fps,
        generate_audio=row.generate_audio,
        seed=row.seed,
        status=row.status,
        progress_stage=row.progress_stage,
        progress_pct=row.progress_pct,
        submission_epoch=int(getattr(row, "submission_epoch", 0) or 0),
        provider_name=row.provider_name,
        provider_kind=row.provider_kind,
        est_token_upper=row.est_token_upper,
        est_cost=money(row.est_cost_micro),
        billed_tokens=row.billed_tokens,
        billed_cost=money(row.billed_cost_micro)
        if row.billed_cost_micro is not None
        else None,
        video=video_out(video) if video is not None else None,
        temporary_download=temporary_video_download_out(row),
        elapsed_ms=generation_elapsed_ms(row),
        error_code=row.error_code,
        error_message=row.error_message,
        diagnostics=public_video_diagnostics(row.diagnostics),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        submit_started_at=getattr(row, "submit_started_at", None),
        submitted_at=row.submitted_at,
        finished_at=row.finished_at,
    )
