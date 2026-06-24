"""Video generation API and media endpoints."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import secrets
import stat
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterable, Iterator
from urllib.parse import urlencode, urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_QUEUED,
    VideoGenerationStage,
    VideoGenerationStatus,
    task_channel,
)
from lumen_core.models import Image, OutboxEvent, PricingRule, Video, VideoGeneration
from lumen_core.models import new_uuid7
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    MoneyOut,
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoModelOptionOut,
    VideoOptionsOut,
    VideoOut,
    VideoPriceOptionOut,
    VideoReferenceMediaIn,
    VideoReferenceMediaOut,
    normalize_asset_reference_url,
)
from lumen_core.video_billing import (
    SMART_VIDEO_DURATION_S,
    SUPPORTED_VIDEO_DURATIONS_S,
    VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
    VIDEO_PRICING_SCOPE,
    VIDEO_PRICING_UNIT,
    VIDEO_PRICING_VARIANTS,
    VideoBillingError,
    estimate_video_cost,
    expand_video_duration_estimates,
    is_seedance_20_fast_identifier,
    split_video_resolution_pricing_variant,
    video_billing_model,
    video_pricing_variant,
)
from lumen_core.video_providers import (
    VIDEO_ACTIONS,
    parse_video_provider_config_json,
    select_video_provider,
)

from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..public_urls import resolve_public_base_url
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..sse_publish import publish_sse_event
from ..video_reference_images import (
    VIDEO_REFERENCE_IMAGE_KIND,
    VideoReferenceImageError,
    ensure_video_reference_image_variant,
)
from ..video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_KIND,
    VIDEO_REFERENCE_VIDEO_MIME,
    VideoReferenceVideoError,
    ensure_video_reference_video_variant,
    video_reference_variant_metadata,
)


router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_VIDEO_DURATIONS = list(SUPPORTED_VIDEO_DURATIONS_S)
_DEFAULT_VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"]
_VIDEO_RESOLUTION_ORDER = {
    value: index for index, value in enumerate(_DEFAULT_VIDEO_RESOLUTIONS)
}
_SEEDANCE_20_FAST_RESOLUTIONS = ("480p", "720p")
_SEEDANCE_20_STANDARD_RESOLUTIONS = ("480p", "720p", "1080p", "4k")
_HAPPYHORSE_RESOLUTIONS = ("720p", "1080p")
_OMNI_FLASH_RESOLUTIONS = ("720p", "1080p", "4k")
_OMNI_FLASH_DURATIONS = tuple(range(6, 11))
_HAPPYHORSE_ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
_OMNI_FLASH_ASPECT_RATIOS = ("adaptive", "16:9", "9:16", "1:1")
_HAPPYHORSE_MODEL_PREFIX = "happyhorse-1.0"
_OMNI_FLASH_MODEL_PREFIXES = ("omni-flash", "gemini_omni_flash")
_DEFAULT_VIDEO_ASPECT_RATIOS = [
    "adaptive",
    "16:9",
    "9:16",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
]
_VIDEO_DEADLINE = timedelta(minutes=10)
_VIDEO_LIST_LIMIT_MAX = 100
_VIDEO_REFERENCE_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
_VIDEO_REFERENCE_UPLOAD_MAX_COUNT = 20
_VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES = 1024 * 1024 * 1024
_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)
_VIDEO_REFERENCE_MIME_EXT = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}
_VIDEO_TERMINAL_STATUSES = {
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
}


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _money(amount_micro: int) -> MoneyOut:
    return MoneyOut(**billing_core.money_dict(int(amount_micro)))


def _video_binary_url(video_id: str) -> str:
    return f"/api/videos/{video_id}/binary"


def _video_poster_url(video_id: str, poster_storage_key: str | None) -> str | None:
    return f"/api/videos/{video_id}/poster" if poster_storage_key else None


def _video_out(video: Video) -> VideoOut:
    return VideoOut(
        id=video.id,
        url=_video_binary_url(video.id),
        poster_url=_video_poster_url(video.id, video.poster_storage_key),
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


def _reference_upload_ext(file: UploadFile) -> tuple[str, str]:
    mime = (file.content_type or "").strip().lower()
    if mime not in _VIDEO_REFERENCE_MIME_EXT:
        suffix = Path(file.filename or "").suffix.lower().lstrip(".")
        by_suffix = {
            "mp4": "video/mp4",
            "mov": "video/quicktime",
        }
        mime = by_suffix.get(suffix, mime)
    ext = _VIDEO_REFERENCE_MIME_EXT.get(mime)
    if ext is None:
        raise _http(
            "unsupported_video_type",
            "reference video must be mp4 or mov",
            415,
        )
    return mime, ext


def _reference_token_expiry(now: datetime | None = None) -> str:
    return (
        (now or datetime.now(timezone.utc)) + _REFERENCE_ACCESS_TOKEN_TTL
    ).isoformat()


def _parse_reference_token_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _reference_token_is_valid(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
    token: str,
    updated_at: datetime | None,
) -> bool:
    expected = metadata.get(token_key)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        return False
    expires_at = _parse_reference_token_expiry(metadata.get(expires_key))
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        return expires_at > now
    if updated_at is None:
        return False
    fallback_updated_at = (
        updated_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None
        else updated_at.astimezone(timezone.utc)
    )
    return fallback_updated_at + _REFERENCE_ACCESS_TOKEN_TTL > now


def _ensure_reference_access_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> str:
    token = metadata.get(token_key)
    expires_at = _parse_reference_token_expiry(metadata.get(expires_key))
    if (
        not isinstance(token, str)
        or not token
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        token = secrets.token_urlsafe(32)
    metadata[token_key] = token
    metadata[expires_key] = _reference_token_expiry()
    return token


def _looks_like_reference_video(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _reference_video_upload_key(user_id: str, video_id: str, ext: str) -> str:
    return f"u/{user_id}/vref/{video_id}/original.{ext}"


def _ensure_reference_video_access_token(video: Video) -> str:
    metadata = dict(video.metadata_jsonb or {})
    token = _ensure_reference_access_token(
        metadata,
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
    )
    video.metadata_jsonb = metadata
    return token


def _reference_video_public_url(
    video: Video,
    public_base_url: str,
    *,
    variant: str | None = None,
) -> str:
    token = _ensure_reference_video_access_token(video)
    query_args = {"token": token}
    if variant:
        query_args["variant"] = variant
    query = urlencode(query_args)
    return (
        f"{public_base_url.rstrip('/')}/api/videos/reference/{video.id}/binary?{query}"
    )


def _ensure_reference_image_access_token(image: Image) -> str:
    metadata = dict(image.metadata_jsonb or {})
    token = _ensure_reference_access_token(
        metadata,
        token_key="video_reference_access_token",
        expires_key="video_reference_access_token_expires_at",
    )
    image.metadata_jsonb = metadata
    return token


def _reference_image_public_url(
    image: Image,
    public_base_url: str,
    *,
    variant: str | None = None,
) -> str:
    token = _ensure_reference_image_access_token(image)
    query_args = {"token": token}
    if variant:
        query_args["variant"] = variant
    query = urlencode(query_args)
    return (
        f"{public_base_url.rstrip('/')}/api/images/reference/{image.id}/binary?{query}"
    )


async def _reference_image_upstream_public_url(
    db: AsyncSession,
    image: Image,
    public_base_url: str,
    *,
    required: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    try:
        variant = await ensure_video_reference_image_variant(
            db,
            image,
            storage_root=settings.storage_root,
        )
    except VideoReferenceImageError as exc:
        if required:
            raise _http(exc.code, exc.message, exc.status_code) from exc
        logger.warning(
            "video reference image variant unavailable; falling back to inline "
            "media image_id=%s code=%s",
            image.id,
            exc.code,
            exc_info=True,
        )
        return None, {
            "upstream_reference_variant": None,
            "upstream_reference_variant_error": {
                "code": exc.code,
                "message": exc.message[:300],
            },
        }
    except Exception as exc:  # noqa: BLE001
        if required:
            raise _http(
                "video_reference_variant_failed",
                "video reference image variant failed",
                503,
            ) from exc
        logger.warning(
            "video reference image variant failed; falling back to inline media "
            "image_id=%s",
            image.id,
            exc_info=True,
        )
        return None, {
            "upstream_reference_variant": None,
            "upstream_reference_variant_error": {
                "code": "video_reference_variant_failed",
                "message": str(exc)[:300],
            },
        }
    return (
        _reference_image_public_url(
            image,
            public_base_url,
            variant=VIDEO_REFERENCE_IMAGE_KIND,
        ),
        {
            "upstream_reference_variant": VIDEO_REFERENCE_IMAGE_KIND,
            "upstream_reference_storage_key": variant.storage_key,
            "upstream_reference_mime": "image/jpeg",
            "upstream_reference_width": variant.width,
            "upstream_reference_height": variant.height,
        },
    )


async def _reference_video_upstream_public_url(
    db: AsyncSession,
    video: Video,
    public_base_url: str,
) -> tuple[str, dict[str, Any]]:
    try:
        variant = await ensure_video_reference_video_variant(
            db,
            video,
            storage_root=settings.storage_root,
        )
    except VideoReferenceVideoError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "video reference video variant failed video_id=%s",
            video.id,
            exc_info=True,
        )
        raise _http(
            "video_reference_variant_failed",
            "video reference variant failed",
            503,
        ) from exc
    return (
        _reference_video_public_url(
            video,
            public_base_url,
            variant=VIDEO_REFERENCE_VIDEO_KIND,
        ),
        {
            "upstream_reference_variant": VIDEO_REFERENCE_VIDEO_KIND,
            "upstream_reference_storage_key": variant["storage_key"],
            "upstream_reference_mime": VIDEO_REFERENCE_VIDEO_MIME,
            "upstream_reference_width": variant["width"],
            "upstream_reference_height": variant["height"],
            "upstream_reference_size_bytes": variant["size_bytes"],
            "upstream_reference_sha256": variant["sha256"],
        },
    )


def _provider_requires_public_media(provider: Any) -> bool:
    return getattr(provider, "kind", None) == "dashscope"


def _provider_prefers_public_media_url(provider: Any) -> bool:
    return _provider_requires_public_media(provider) or getattr(
        provider, "kind", None
    ) in {"volcano_third_party", "omni_flash"}


def _write_new_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _unlink_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _reference_media_out(snapshot: dict[str, Any]) -> VideoReferenceMediaOut | None:
    kind = snapshot.get("kind")
    if kind not in {"image", "video"}:
        return None
    raw_url = snapshot.get("url") if isinstance(snapshot.get("url"), str) else None
    url = None if _is_internal_reference_url(raw_url) else raw_url
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
        mime=snapshot.get("mime") if isinstance(snapshot.get("mime"), str) else None,
    )


def _is_internal_reference_url(raw_url: str | None) -> bool:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return False
    path = urlsplit(raw_url).path
    return path.startswith("/api/images/reference/") or path.startswith(
        "/api/videos/reference/"
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


def _public_video_diagnostics(raw: Any) -> dict[str, Any]:
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


def _generation_reference_media(row: VideoGeneration) -> list[VideoReferenceMediaOut]:
    raw = (row.upstream_request or {}).get("reference_media")
    if not isinstance(raw, list):
        return []
    out: list[VideoReferenceMediaOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = _reference_media_out(item)
        if ref is not None:
            out.append(ref)
    return out


async def _video_for_generation(
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


async def _generation_out(
    db: AsyncSession, row: VideoGeneration, video: Video | None = None
) -> VideoGenerationOut:
    if video is None:
        video = await _video_for_generation(db, row.id)
    return VideoGenerationOut(
        id=row.id,
        action=row.action,
        model=row.model,
        prompt=row.prompt,
        input_image_id=row.input_image_id,
        reference_media=_generation_reference_media(row),
        duration_s=row.duration_s,
        resolution=row.resolution,
        aspect_ratio=row.aspect_ratio,
        fps=row.fps,
        generate_audio=row.generate_audio,
        seed=row.seed,
        status=row.status,
        progress_stage=row.progress_stage,
        progress_pct=row.progress_pct,
        provider_name=row.provider_name,
        provider_kind=row.provider_kind,
        est_token_upper=row.est_token_upper,
        est_cost=_money(row.est_cost_micro),
        billed_tokens=row.billed_tokens,
        billed_cost=_money(row.billed_cost_micro)
        if row.billed_cost_micro is not None
        else None,
        video=_video_out(video) if video is not None else None,
        error_code=row.error_code,
        error_message=row.error_message,
        diagnostics=_public_video_diagnostics(row.diagnostics),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        submitted_at=row.submitted_at,
        finished_at=row.finished_at,
    )


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    return await get_setting(db, spec)


async def _video_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "video.enabled"), False
    )


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"), False
    )


async def _allow_negative_balance(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"), False
    )


async def _video_provider_state(db: AsyncSession):
    raw_video = await _setting_raw(db, "video.providers")
    raw_shared = await _setting_raw(db, "providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    return providers, errors


async def _video_hold_estimates(db: AsyncSession) -> dict[str, Any]:
    raw = await _setting_raw(db, "video.token_hold_estimates")
    if not raw:
        raise _http(
            "video_estimates_missing",
            "video token hold estimates are not configured",
            503,
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _http(
            "video_estimates_invalid", "video token hold estimates are invalid", 503
        ) from exc
    if not isinstance(parsed, dict):
        raise _http(
            "video_estimates_invalid",
            "video token hold estimates must be an object",
            503,
        )
    return expand_video_duration_estimates(parsed)


@router.post("/upload", response_model=VideoOut, dependencies=[Depends(verify_csrf)])
async def upload_reference_video(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
) -> VideoOut:
    mime, ext = _reference_upload_ext(file)
    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > _VIDEO_REFERENCE_UPLOAD_MAX_BYTES:
            raise _http(
                "too_large",
                f"file exceeds {_VIDEO_REFERENCE_UPLOAD_MAX_BYTES // (1024 * 1024)}MB",
                413,
            )
    if not buf:
        raise _http("empty_file", "empty file", 400)
    data = bytes(buf)
    if not _looks_like_reference_video(data):
        raise _http(
            "invalid_video_file",
            "reference video must be a valid mp4 or mov file",
            415,
        )
    sha = hashlib.sha256(data).hexdigest()
    existing = (
        await db.execute(
            select(Video).where(
                Video.user_id == user.id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.sha256 == sha,
                Video.storage_key.like(f"u/{user.id}/vref/%"),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        _ensure_reference_video_access_token(existing)
        await db.commit()
        await db.refresh(existing)
        return _video_out(existing)
    count, total_bytes = (
        await db.execute(
            select(
                func.count(Video.id),
                func.coalesce(func.sum(Video.size_bytes), 0),
            ).where(
                Video.user_id == user.id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.storage_key.like(f"u/{user.id}/vref/%"),
            )
        )
    ).one()
    if int(count or 0) >= _VIDEO_REFERENCE_UPLOAD_MAX_COUNT:
        raise _http(
            "reference_video_quota_exceeded",
            f"reference video limit is {_VIDEO_REFERENCE_UPLOAD_MAX_COUNT} files",
            429,
        )
    if int(total_bytes or 0) + len(data) > _VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES:
        raise _http(
            "reference_video_quota_exceeded",
            "reference video storage quota exceeded",
            429,
        )
    video = Video(
        user_id=user.id,
        owner_generation_id=None,
        storage_key="",
        poster_storage_key=None,
        mime=mime,
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=len(data),
        sha256=sha,
        etag=sha,
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb={
            "source": "uploaded_reference",
            "filename": file.filename or "",
            "reference_access_token": secrets.token_urlsafe(32),
            "reference_access_token_expires_at": _reference_token_expiry(),
        },
    )
    db.add(video)
    await db.flush()
    key = _reference_video_upload_key(user.id, video.id, ext)
    video.storage_key = key
    path = _fs_path(key)
    try:
        await asyncio.to_thread(_write_new_file_atomic, path, data)
        await db.commit()
    except Exception:
        await db.rollback()
        await asyncio.to_thread(_unlink_file_if_exists, path)
        raise
    await db.refresh(video)
    return _video_out(video)


def _estimate_pairs(estimates: dict[str, Any]) -> tuple[list[int], list[str]]:
    durations: set[int] = set()
    resolutions: set[str] = set()
    for model_value in estimates.values():
        if not isinstance(model_value, dict):
            continue
        for action_value in model_value.values():
            if not isinstance(action_value, dict):
                continue
            for key in action_value:
                if not isinstance(key, str) or ":" not in key:
                    continue
                resolution, duration = key.rsplit(":", 1)
                try:
                    duration_s = int(duration)
                except ValueError:
                    continue
                if resolution:
                    resolutions.add(resolution)
                if duration_s > 0:
                    durations.add(duration_s)
    return (
        sorted(durations) or list(_DEFAULT_VIDEO_DURATIONS),
        _ordered_video_resolutions(resolutions) or list(_DEFAULT_VIDEO_RESOLUTIONS),
    )


def _duration_options(estimates: dict[str, Any]) -> list[int]:
    durations, _resolutions = _estimate_pairs(estimates)
    return [
        SMART_VIDEO_DURATION_S,
        *[item for item in durations if item != SMART_VIDEO_DURATION_S],
    ]


def _duration_options_for_model(
    model: str,
    *,
    upstream_model: str | None = None,
    available_durations: Iterable[int] | None = None,
) -> list[int]:
    if _is_omni_flash_model(model, upstream_model):
        return list(_OMNI_FLASH_DURATIONS)
    available = set(available_durations or _DEFAULT_VIDEO_DURATIONS)
    positive_durations = sorted(item for item in available if item > 0)
    return [SMART_VIDEO_DURATION_S, *positive_durations]


def _estimate_duration_options_for_model_action(
    estimates: dict[str, Any],
    *,
    model: str,
    action: str,
    resolutions: Iterable[str],
) -> list[int]:
    model_value = estimates.get(model)
    if not isinstance(model_value, dict):
        return []
    actions = [action]
    if action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        actions = [
            "reference_image",
            "reference_video",
            VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
            "i2v",
        ]
    allowed_resolutions = set(resolutions)
    durations: set[int] = set()
    for action_name in actions:
        action_value = model_value.get(action_name)
        if not isinstance(action_value, dict):
            continue
        for key in action_value:
            if not isinstance(key, str) or ":" not in key:
                continue
            resolution, duration = key.rsplit(":", 1)
            if allowed_resolutions and resolution not in allowed_resolutions:
                continue
            try:
                duration_s = int(duration)
            except ValueError:
                continue
            if duration_s > 0:
                durations.add(duration_s)
    return sorted(durations)


def _video_price_action_for_provider(provider_kind: str, action: str) -> str:
    if (
        provider_kind in {"dashscope", "omni_flash"}
        and action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT
    ):
        return "reference_image"
    return action


def _duration_options_for_provider_action(
    estimates: dict[str, Any],
    *,
    model: str,
    upstream_model: str | None,
    provider_kind: str,
    action: str,
    resolutions: Iterable[str],
    fallback_durations: Iterable[int],
    allow_action_fallback: bool = True,
    allow_global_fallback: bool = True,
) -> list[int]:
    if _is_omni_flash_model(model, upstream_model):
        return _duration_options_for_model(model, upstream_model=upstream_model)
    billing_model = video_billing_model(model, upstream_model)
    price_action = _video_price_action_for_provider(provider_kind, action)
    estimate_durations = _estimate_duration_options_for_model_action(
        estimates,
        model=billing_model,
        action=price_action,
        resolutions=resolutions,
    )
    if estimate_durations:
        return _duration_options_for_model(
            model,
            upstream_model=upstream_model,
            available_durations=estimate_durations,
        )
    if allow_action_fallback:
        action_durations = _estimate_duration_options_for_model_action(
            estimates,
            model=billing_model,
            action=price_action,
            resolutions=[],
        )
        if action_durations:
            return _duration_options_for_model(
                model,
                upstream_model=upstream_model,
                available_durations=action_durations,
            )
    if not allow_global_fallback:
        return []
    return _duration_options_for_model(
        model,
        upstream_model=upstream_model,
        available_durations=fallback_durations,
    )


def _ordered_video_resolutions(values: Iterable[str]) -> list[str]:
    return sorted(
        set(values),
        key=lambda value: (_VIDEO_RESOLUTION_ORDER.get(value, 999), value),
    )


def _is_seedance_20_fast_model(*identifiers: str | None) -> bool:
    return is_seedance_20_fast_identifier(*identifiers)


def _is_seedance_20_mini_model(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("_", "-").replace(".", "-")
        if "seedance-2-0-mini" in value:
            return True
    return False


def _is_seedance_20_standard_model(*identifiers: str | None) -> bool:
    if _is_seedance_20_fast_model(*identifiers) or _is_seedance_20_mini_model(
        *identifiers
    ):
        return False
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("_", "-").replace(".", "-")
        if "seedance-2-0" in value:
            return True
    return False


def _is_happyhorse_model(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        if identifier.strip().lower().startswith(_HAPPYHORSE_MODEL_PREFIX):
            return True
    return False


def _is_omni_flash_model(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("-", "_")
        if any(
            value.startswith(prefix.replace("-", "_"))
            for prefix in _OMNI_FLASH_MODEL_PREFIXES
        ):
            return True
    return False


def _video_resolution_options_for_model(
    model: str,
    *,
    upstream_model: str | None = None,
    available_resolutions: Iterable[str] | None = None,
) -> list[str]:
    available = _ordered_video_resolutions(
        available_resolutions or _DEFAULT_VIDEO_RESOLUTIONS
    )
    if _is_happyhorse_model(model, upstream_model):
        allowed = set(_HAPPYHORSE_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    if _is_omni_flash_model(model, upstream_model):
        allowed = set(_OMNI_FLASH_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    if _is_seedance_20_fast_model(model, upstream_model) or _is_seedance_20_mini_model(
        model, upstream_model
    ):
        allowed = set(_SEEDANCE_20_FAST_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    if _is_seedance_20_standard_model(model, upstream_model):
        allowed = set(_SEEDANCE_20_STANDARD_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    return [resolution for resolution in available if resolution != "4k"]


def _request_fingerprint(body: VideoCreateIn) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generation_request_fingerprint(row: VideoGeneration) -> str | None:
    if isinstance(row.request_fingerprint, str) and row.request_fingerprint:
        return row.request_fingerprint
    diagnostics = row.diagnostics or {}
    value = diagnostics.get("request_fingerprint")
    return value if isinstance(value, str) and value else None


def _needs_reference_public_base_url(
    body: VideoCreateIn,
    fallback_snapshots: list[dict[str, Any]] | None,
    *,
    requires_public_media: bool = False,
    prefers_public_media_url: bool = False,
) -> bool:
    use_public_media = requires_public_media or prefers_public_media_url
    if use_public_media and body.action == "i2v" and body.input_image_id:
        return True
    if use_public_media and any(
        item.kind == "image" and item.image_id for item in body.reference_media
    ):
        return True
    if any(item.kind == "video" and item.video_id for item in body.reference_media):
        return True
    if fallback_snapshots is None:
        return False
    return any(
        (
            item.get("kind") == "video"
            and isinstance(item.get("video_id"), str)
            and not isinstance(item.get("url"), str)
        )
        or (
            use_public_media
            and item.get("kind") == "image"
            and isinstance(item.get("image_id"), str)
            and not isinstance(item.get("url"), str)
        )
        for item in fallback_snapshots
        if isinstance(item, dict)
    )


async def _reference_public_base_url(
    request: Request | None,
    db: AsyncSession,
    body: VideoCreateIn,
    fallback_snapshots: list[dict[str, Any]] | None,
    *,
    requires_public_media: bool = False,
    prefers_public_media_url: bool = False,
) -> str | None:
    hard_requires_public_base = _needs_reference_public_base_url(
        body,
        fallback_snapshots,
        requires_public_media=requires_public_media,
    )
    if not _needs_reference_public_base_url(
        body,
        fallback_snapshots,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
    ):
        return None
    if request is None:
        if hard_requires_public_base:
            raise _http(
                "video_reference_public_url_missing",
                "PUBLIC_BASE_URL or site.public_base_url is required for upstream-readable video media",
                503,
            )
        return None
    try:
        return await resolve_public_base_url(request, db)
    except Exception as exc:
        if not hard_requires_public_base:
            logger.info(
                "video reference public URL unavailable; falling back to inline media",
                exc_info=True,
            )
            return None
        raise _http(
            "video_reference_public_url_missing",
            "PUBLIC_BASE_URL or site.public_base_url is required for upstream-readable video media",
            503,
        ) from exc


def _ensure_idempotent_replay_matches(
    row: VideoGeneration,
    request_fingerprint: str,
) -> None:
    existing_fingerprint = _generation_request_fingerprint(row)
    if existing_fingerprint and existing_fingerprint != request_fingerprint:
        raise _http(
            "idempotency_request_mismatch",
            "idempotency_key was already used with a different video request",
            409,
        )


async def _video_price_options(db: AsyncSession) -> list[VideoPriceOptionOut]:
    rows = (
        (
            await db.execute(
                select(PricingRule).where(
                    PricingRule.scope == VIDEO_PRICING_SCOPE,
                    PricingRule.unit == VIDEO_PRICING_UNIT,
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[VideoPriceOptionOut] = []
    for row in rows:
        action, resolution = split_video_resolution_pricing_variant(row.variant)
        if action not in VIDEO_PRICING_VARIANTS:
            continue
        out.append(
            VideoPriceOptionOut(
                model=row.key,
                action=action,  # type: ignore[arg-type]
                resolution=resolution,  # type: ignore[arg-type]
                variant=row.variant,
                price=_money(row.price_micro),
                enabled=row.enabled,
                note=row.note,
            )
        )
    return out


def _has_video_price(
    price_pairs: set[tuple[str, str, str | None]],
    *,
    model: str,
    action: str,
    resolutions: list[str] | tuple[str, ...] | None = None,
) -> bool:
    def has(action_name: str, resolution: str | None) -> bool:
        return (model, action_name, resolution) in price_pairs or (
            model,
            action_name,
            None,
        ) in price_pairs

    resolution_options = list(resolutions or [None])
    if action != VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        return any(has(action, resolution) for resolution in resolution_options)
    for resolution in resolution_options:
        legacy_priced = has(VIDEO_LEGACY_REFERENCE_PRICING_VARIANT, resolution)
        if (
            legacy_priced
            or has("reference_image", resolution)
            or has("i2v", resolution)
            or has("reference_video", resolution)
        ):
            return True
    return False


def _public_video_hold_estimates(
    estimates: dict[str, Any],
    model_billing_models: dict[str, dict[str, str]],
) -> dict[str, Any]:
    allowed_models = {
        billing_model
        for action_map in model_billing_models.values()
        for billing_model in action_map.values()
        if isinstance(billing_model, str) and billing_model
    }
    out: dict[str, Any] = {}
    for model in sorted(allowed_models):
        value = estimates.get(model)
        if isinstance(value, dict):
            out[model] = value
    return out


@router.get("/options", response_model=VideoOptionsOut)
async def video_options(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoOptionsOut:
    enabled = await _video_enabled(db)
    estimates: dict[str, Any] = {}
    unavailable_reason: str | None = None
    try:
        estimates = await _video_hold_estimates(db)
    except HTTPException as exc:
        unavailable_reason = (
            exc.detail.get("error", {}).get("code")
            if isinstance(exc.detail, dict)
            else "video_estimates_missing"
        )

    providers, provider_errors = await _video_provider_state(db)
    if provider_errors:
        unavailable_reason = "video_provider_config_invalid"
    prices = await _video_price_options(db)
    price_pairs = {(item.model, item.action, item.resolution) for item in prices}
    durations, resolutions = _estimate_pairs(estimates)
    global_durations = _duration_options(estimates)

    model_actions: dict[str, set[str]] = {}
    model_durations: dict[str, set[int]] = {}
    model_action_durations: dict[str, dict[str, set[int]]] = {}
    model_action_resolution_durations: dict[str, dict[str, dict[str, set[int]]]] = {}
    model_resolutions: dict[str, set[str]] = {}
    model_billing_models: dict[str, dict[str, str]] = {}
    for provider in providers:
        mapping = provider.models or {}
        for key in mapping:
            if ":" not in key:
                for action in VIDEO_ACTIONS:
                    if not provider.supports(key, action):
                        continue
                    upstream_model = provider.upstream_model_for(key, action)
                    billing_model = video_billing_model(key, upstream_model)
                    allowed_resolutions = _video_resolution_options_for_model(
                        key,
                        upstream_model=upstream_model,
                        available_resolutions=resolutions,
                    )
                    price_action = _video_price_action_for_provider(
                        provider.kind, action
                    )
                    action_resolutions = [
                        resolution
                        for resolution in allowed_resolutions
                        if _has_video_price(
                            price_pairs,
                            model=billing_model,
                            action=price_action,
                            resolutions=[resolution],
                        )
                    ]
                    estimate_durations = _estimate_duration_options_for_model_action(
                        estimates,
                        model=billing_model,
                        action=price_action,
                        resolutions=action_resolutions,
                    )
                    action_durations = _duration_options_for_provider_action(
                        estimates,
                        model=key,
                        upstream_model=upstream_model,
                        provider_kind=provider.kind,
                        action=action,
                        resolutions=action_resolutions,
                        fallback_durations=durations or global_durations,
                    )
                    if action_resolutions:
                        model_actions.setdefault(key, set()).add(action)
                        model_durations.setdefault(key, set()).update(action_durations)
                        model_action_durations.setdefault(key, {}).setdefault(
                            action, set()
                        ).update(action_durations)
                        action_resolution_durations = (
                            model_action_resolution_durations.setdefault(
                                key, {}
                            ).setdefault(action, {})
                        )
                        for resolution in action_resolutions:
                            resolution_durations = (
                                _duration_options_for_provider_action(
                                    estimates,
                                    model=key,
                                    upstream_model=upstream_model,
                                    provider_kind=provider.kind,
                                    action=action,
                                    resolutions=[resolution],
                                    fallback_durations=estimate_durations
                                    or durations
                                    or global_durations,
                                    allow_action_fallback=False,
                                    allow_global_fallback=False,
                                )
                            )
                            action_resolution_durations.setdefault(
                                resolution, set()
                            ).update(resolution_durations)
                        model_resolutions.setdefault(key, set()).update(
                            action_resolutions
                        )
                        model_billing_models.setdefault(key, {})[action] = billing_model
                continue
            model, action = key.rsplit(":", 1)
            if action not in VIDEO_ACTIONS or not provider.supports(model, action):
                continue
            upstream_model = provider.upstream_model_for(model, action)
            billing_model = video_billing_model(model, upstream_model)
            allowed_resolutions = _video_resolution_options_for_model(
                model,
                upstream_model=upstream_model,
                available_resolutions=resolutions,
            )
            price_action = _video_price_action_for_provider(provider.kind, action)
            action_resolutions = [
                resolution
                for resolution in allowed_resolutions
                if _has_video_price(
                    price_pairs,
                    model=billing_model,
                    action=price_action,
                    resolutions=[resolution],
                )
            ]
            estimate_durations = _estimate_duration_options_for_model_action(
                estimates,
                model=billing_model,
                action=price_action,
                resolutions=action_resolutions,
            )
            action_durations = _duration_options_for_provider_action(
                estimates,
                model=model,
                upstream_model=upstream_model,
                provider_kind=provider.kind,
                action=action,
                resolutions=action_resolutions,
                fallback_durations=durations or global_durations,
            )
            if action_resolutions:
                model_actions.setdefault(model, set()).add(action)
                model_durations.setdefault(model, set()).update(action_durations)
                model_action_durations.setdefault(model, {}).setdefault(
                    action, set()
                ).update(action_durations)
                action_resolution_durations = (
                    model_action_resolution_durations.setdefault(model, {}).setdefault(
                        action, {}
                    )
                )
                for resolution in action_resolutions:
                    resolution_durations = _duration_options_for_provider_action(
                        estimates,
                        model=model,
                        upstream_model=upstream_model,
                        provider_kind=provider.kind,
                        action=action,
                        resolutions=[resolution],
                        fallback_durations=estimate_durations
                        or durations
                        or global_durations,
                        allow_action_fallback=False,
                        allow_global_fallback=False,
                    )
                    action_resolution_durations.setdefault(resolution, set()).update(
                        resolution_durations
                    )
                model_resolutions.setdefault(model, set()).update(action_resolutions)
                model_billing_models.setdefault(model, {})[action] = billing_model
    if enabled and not model_actions and unavailable_reason is None:
        unavailable_reason = "video_provider_or_pricing_missing"
    model_options: list[VideoModelOptionOut] = []
    for model, actions in sorted(model_actions.items()):
        sorted_actions = sorted(actions)
        billing_models = {
            action: model_billing_models.get(model, {}).get(action, model)
            for action in sorted_actions
        }
        unique_billing_models = set(billing_models.values())
        model_options.append(
            VideoModelOptionOut(
                model=model,
                billing_model=(
                    next(iter(unique_billing_models))
                    if len(unique_billing_models) == 1
                    else None
                ),
                billing_models=billing_models,
                actions=sorted_actions,  # type: ignore[arg-type]
                durations_s=sorted(model_durations.get(model, set())),
                durations_by_action={
                    action: sorted(durations)
                    for action, durations in model_action_durations.get(
                        model, {}
                    ).items()
                },
                durations_by_action_resolution={
                    action: {
                        resolution: sorted(durations)
                        for resolution, durations in resolution_map.items()
                    }
                    for action, resolution_map in model_action_resolution_durations.get(
                        model, {}
                    ).items()
                },
                resolutions=_ordered_video_resolutions(
                    model_resolutions.get(model, set())
                ),  # type: ignore[arg-type]
            )
        )
    public_hold_estimates = _public_video_hold_estimates(
        estimates,
        model_billing_models,
    )
    return VideoOptionsOut(
        enabled=enabled and unavailable_reason is None,
        models=model_options,
        durations_s=_duration_options(estimates),
        resolutions=resolutions,
        aspect_ratios=list(_DEFAULT_VIDEO_ASPECT_RATIOS),
        generate_audio=True,
        pricing=prices,
        hold_estimates=public_hold_estimates,
        unavailable_reason=None
        if enabled and unavailable_reason is None
        else unavailable_reason or "video_disabled",
    )


async def _require_video_create_ready(
    db: AsyncSession,
    body: VideoCreateIn,
) -> tuple[Any, dict[str, Any]]:
    if not await _video_enabled(db):
        raise _http("video_disabled", "video generation is disabled", 503)
    if not await _billing_enabled(db):
        raise _http("billing_disabled", "video generation requires wallet billing", 503)
    estimates = await _video_hold_estimates(db)
    durations, resolutions = _estimate_pairs(estimates)
    if body.resolution not in resolutions:
        raise _http("invalid_resolution", "resolution is not available", 422)
    if body.aspect_ratio not in _DEFAULT_VIDEO_ASPECT_RATIOS:
        raise _http("invalid_aspect_ratio", "aspect_ratio is not available", 422)
    providers, provider_errors = await _video_provider_state(db)
    if provider_errors:
        raise _http("video_provider_config_invalid", "; ".join(provider_errors), 503)
    provider = select_video_provider(providers, model=body.model, action=body.action)
    if provider is None:
        raise _http(
            "video_provider_missing",
            "no enabled video provider supports this model/action",
            503,
        )
    upstream_model = provider.upstream_model_for(body.model, body.action)
    model_resolutions = _video_resolution_options_for_model(
        body.model,
        upstream_model=upstream_model,
        available_resolutions=resolutions,
    )
    if body.resolution not in model_resolutions:
        raise _http(
            "invalid_resolution",
            "resolution is not available for this model",
            422,
            model=body.model,
            resolution=body.resolution,
            available_resolutions=model_resolutions,
        )
    model_durations = _duration_options_for_provider_action(
        estimates,
        model=body.model,
        upstream_model=upstream_model,
        provider_kind=provider.kind,
        action=body.action,
        resolutions=[body.resolution],
        fallback_durations=durations,
        allow_action_fallback=False,
        allow_global_fallback=False,
    )
    if body.duration_s not in model_durations:
        raise _http(
            "invalid_duration",
            "duration_s is not available for this model",
            422,
            model=body.model,
            duration_s=body.duration_s,
            available_durations_s=model_durations,
        )
    return provider, estimates


async def _input_image_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str | None,
    fallback_snapshot: tuple[str | None, str | None, str | None] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
) -> tuple[str | None, str | None, str | None]:
    if image_id is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        return None, None, None
    if (
        fallback_snapshot is not None
        and fallback_snapshot[0]
        and reference_public_base_url is None
    ):
        return fallback_snapshot
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if img is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        raise _http("input_image_not_found", "input image not found", 404)
    url = None
    if reference_public_base_url is not None:
        url, _meta = await _reference_image_upstream_public_url(
            db,
            img,
            reference_public_base_url,
            required=required_public_media,
        )
    return img.storage_key, img.sha256, url


def _validate_reference_url(raw_url: str) -> str:
    asset_url = normalize_asset_reference_url(raw_url)
    if asset_url is not None:
        if not asset_url:
            raise _http("invalid_reference_url", "asset reference is empty", 422)
        return asset_url
    value = raw_url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() == "asset":
        asset_id = (parts.netloc or parts.path.strip("/")).strip()
        if not asset_id:
            raise _http("invalid_reference_url", "asset reference is empty", 422)
        return f"asset://{asset_id.lower()}"
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise _http(
            "invalid_reference_url",
            "reference URL must be http, https, or asset://",
            422,
        )
    if parts.username or parts.password:
        raise _http(
            "invalid_reference_url",
            "reference URL must not include credentials",
            422,
        )
    return value


async def _reference_media_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    items: list[VideoReferenceMediaIn],
    fallback_snapshots: list[dict[str, Any]] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
) -> list[dict[str, Any]]:
    if fallback_snapshots is not None:
        snapshots = [dict(item) for item in fallback_snapshots]
        if reference_public_base_url is not None:
            for snapshot in snapshots:
                if snapshot.get("kind") == "image" and isinstance(
                    snapshot.get("image_id"), str
                ):
                    image = (
                        await db.execute(
                            select(Image).where(
                                Image.id == snapshot["image_id"],
                                Image.user_id == user_id,
                                Image.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    if image is not None:
                        url, meta = await _reference_image_upstream_public_url(
                            db,
                            image,
                            reference_public_base_url,
                            required=required_public_media,
                        )
                        snapshot["url"] = url
                        snapshot.update(meta)
                if snapshot.get("kind") == "video" and isinstance(
                    snapshot.get("video_id"), str
                ):
                    video = (
                        await db.execute(
                            select(Video).where(
                                Video.id == snapshot["video_id"],
                                Video.user_id == user_id,
                                Video.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    if video is not None:
                        url, meta = await _reference_video_upstream_public_url(
                            db,
                            video,
                            reference_public_base_url,
                        )
                        snapshot["url"] = url
                        snapshot.update(meta)
        return snapshots
    snapshots: list[dict[str, Any]] = []
    image_index = 0
    video_index = 0
    for item in items:
        if item.kind == "image":
            image_index += 1
            default_label = f"Image {image_index}"
        else:
            video_index += 1
            default_label = f"Video {video_index}"
        label = (item.label or "").strip() or default_label
        if item.url:
            snapshots.append(
                {
                    "kind": item.kind,
                    "url": _validate_reference_url(item.url),
                    "label": label,
                    "source": "url",
                }
            )
            continue
        if item.kind == "image" and item.image_id:
            image = (
                await db.execute(
                    select(Image).where(
                        Image.id == item.image_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if image is None:
                raise _http(
                    "reference_image_not_found", "reference image not found", 404
                )
            url = None
            upstream_meta: dict[str, Any] = {}
            if reference_public_base_url is not None:
                url, upstream_meta = await _reference_image_upstream_public_url(
                    db,
                    image,
                    reference_public_base_url,
                    required=required_public_media,
                )
            snapshots.append(
                {
                    "kind": "image",
                    "image_id": image.id,
                    "label": label,
                    "storage_key": image.storage_key,
                    "sha256": image.sha256,
                    "mime": image.mime,
                    "url": url,
                    "source": "image",
                    **upstream_meta,
                }
            )
            continue
        if item.kind == "video" and item.video_id:
            video = (
                await db.execute(
                    select(Video).where(
                        Video.id == item.video_id,
                        Video.user_id == user_id,
                        Video.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if video is None:
                raise _http(
                    "reference_video_not_found", "reference video not found", 404
                )
            snapshots.append(
                {
                    "kind": "video",
                    "video_id": video.id,
                    "label": label,
                    "storage_key": video.storage_key,
                    "sha256": video.sha256,
                    "mime": video.mime,
                    "source": "video",
                }
            )
            if reference_public_base_url is not None:
                url, upstream_meta = await _reference_video_upstream_public_url(
                    db,
                    video,
                    reference_public_base_url,
                )
                snapshots[-1]["url"] = url
                snapshots[-1].update(upstream_meta)
            else:
                snapshots[-1]["url"] = None
            continue
        raise _http("invalid_reference_media", "reference media is invalid", 422)
    return snapshots


async def _create_video_generation_record(
    db: AsyncSession,
    body: VideoCreateIn,
    user: CurrentUser,
    *,
    request: Request | None = None,
    input_image_snapshot: tuple[str | None, str | None, str | None] | None = None,
    reference_media_snapshot: list[dict[str, Any]] | None = None,
    workflow_metadata: dict[str, Any] | None = None,
) -> VideoGenerationOut:
    provider, estimates = await _require_video_create_ready(db, body)
    requires_public_media = _provider_requires_public_media(provider)
    prefers_public_media_url = _provider_prefers_public_media_url(provider)
    reference_public_base = await _reference_public_base_url(
        request,
        db,
        body,
        reference_media_snapshot,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
    )
    input_storage_key, input_sha256, input_image_url = await _input_image_snapshot(
        db,
        user_id=user.id,
        image_id=body.input_image_id,
        fallback_snapshot=input_image_snapshot,
        reference_public_base_url=reference_public_base
        if prefers_public_media_url
        else None,
        required_public_media=requires_public_media,
    )
    reference_snapshots = await _reference_media_snapshots(
        db,
        user_id=user.id,
        items=body.reference_media,
        fallback_snapshots=reference_media_snapshot,
        reference_public_base_url=reference_public_base,
        required_public_media=requires_public_media,
    )
    if provider.kind == "dashscope" and any(
        item.get("kind") == "video"
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise _http(
            "unsupported_reference_media",
            "HappyHorse reference-to-video supports image references only",
            422,
        )
    if provider.kind == "omni_flash" and any(
        item.get("kind") == "video"
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise _http(
            "unsupported_reference_media",
            "Omni Flash unified video create supports image references only",
            422,
        )
    if (
        provider.kind == "dashscope"
        and body.action in {"t2v", "reference"}
        and body.aspect_ratio != "adaptive"
        and body.aspect_ratio not in _HAPPYHORSE_ASPECT_RATIOS
    ):
        raise _http(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for HappyHorse",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(_HAPPYHORSE_ASPECT_RATIOS),
        )
    if (
        provider.kind == "omni_flash"
        and body.aspect_ratio not in _OMNI_FLASH_ASPECT_RATIOS
    ):
        raise _http(
            "invalid_aspect_ratio",
            "aspect_ratio is not available for Omni Flash",
            422,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            available_aspect_ratios=list(_OMNI_FLASH_ASPECT_RATIOS),
        )
    upstream_model = provider.upstream_model_for(body.model, body.action)
    billing_model = video_billing_model(body.model, upstream_model)
    pricing_variant = video_pricing_variant(
        body.action,
        reference_snapshots,
        resolution=body.resolution,
    )
    used_reference_image_public_variant = (
        isinstance(input_image_url, str)
        and f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in input_image_url
    ) or any(
        item.get("upstream_reference_variant") == VIDEO_REFERENCE_IMAGE_KIND
        or (
            isinstance(item.get("url"), str)
            and f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in item["url"]
        )
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "image"
    )
    used_reference_video_public_variant = any(
        item.get("upstream_reference_variant") == VIDEO_REFERENCE_VIDEO_KIND
        or (
            isinstance(item.get("url"), str)
            and f"variant={VIDEO_REFERENCE_VIDEO_KIND}" in item["url"]
        )
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "video"
    )
    reference_image_variant_error_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict)
        and item.get("kind") == "image"
        and isinstance(item.get("upstream_reference_variant_error"), dict)
    )
    reference_video_variant_error_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict)
        and item.get("kind") == "video"
        and isinstance(item.get("upstream_reference_variant_error"), dict)
    )
    try:
        cost = await estimate_video_cost(
            db,
            model=billing_model,
            action=body.action,
            resolution=body.resolution,
            duration_s=body.duration_s,
            generate_audio=body.generate_audio,
            estimates=estimates,
            pricing_variant=pricing_variant,
        )
    except VideoBillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    if cost.hold_micro <= 0:
        raise _http("video_hold_invalid", "video hold amount must be positive", 422)

    now = datetime.now(timezone.utc)
    request_fingerprint = _request_fingerprint(body)
    upstream_request = {
        "model": body.model,
        "requested_model": body.model,
        "billing_model": billing_model,
        "provider_name": provider.name,
        "provider_kind": provider.kind,
        "upstream_model": upstream_model,
        "input_image_url": input_image_url,
        "reference_media": reference_snapshots,
        "pricing_variant": pricing_variant,
    }
    if workflow_metadata:
        upstream_request.update(workflow_metadata)

    vg = VideoGeneration(
        id=new_uuid7(),
        user_id=user.id,
        action=body.action,
        model=body.model,
        provider_name=provider.name,
        provider_kind=provider.kind,
        prompt=body.prompt,
        input_image_id=body.input_image_id,
        input_image_storage_key=input_storage_key,
        input_image_sha256=input_sha256,
        duration_s=body.duration_s,
        resolution=body.resolution,
        aspect_ratio=body.aspect_ratio,
        fps=None,
        generate_audio=body.generate_audio,
        seed=body.seed,
        watermark=body.watermark,
        upstream_request=upstream_request,
        diagnostics={
            "request_fingerprint": request_fingerprint,
            "reference_media_count": len(reference_snapshots),
            "pricing_variant": pricing_variant,
            "billing_model": billing_model,
            "requested_model": body.model,
            "requires_public_media": requires_public_media,
            "prefers_public_media_url": prefers_public_media_url,
            "reference_public_media_url_enabled": reference_public_base is not None,
            "reference_image_public_variant": (
                VIDEO_REFERENCE_IMAGE_KIND
                if used_reference_image_public_variant
                else None
            ),
            "reference_video_public_variant": (
                VIDEO_REFERENCE_VIDEO_KIND
                if used_reference_video_public_variant
                else None
            ),
            "reference_image_public_variant_error_count": (
                reference_image_variant_error_count
            ),
            "reference_video_public_variant_error_count": (
                reference_video_variant_error_count
            ),
        },
        status=VideoGenerationStatus.QUEUED.value,
        progress_stage=VideoGenerationStage.QUEUED.value,
        progress_pct=0,
        deadline_at=now + _VIDEO_DEADLINE,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
        est_token_upper=cost.estimated_tokens,
        est_cost_micro=cost.hold_micro,
    )
    db.add(vg)
    try:
        await billing_core.hold(
            db,
            user.id,
            cost.hold_micro,
            ref_type="video_generation",
            ref_id=vg.id,
            idempotency_key=f"video_generation:hold:{vg.id}",
            allow_negative=await _allow_negative_balance(db),
            meta={
                "model": body.model,
                "billing_model": billing_model,
                "requested_model": body.model,
                "action": body.action,
                "resolution": body.resolution,
                "duration_s": body.duration_s,
                "estimated_tokens": cost.estimated_tokens,
                "unit_price_micro": cost.unit_price_micro,
                "provider_name": provider.name,
                "upstream_model": upstream_model,
                "reference_media_count": len(reference_snapshots),
                "pricing_variant": pricing_variant,
            },
        )
    except billing_core.BillingError as exc:
        await db.rollback()
        raise _http(exc.code, exc.message, exc.status_code) from exc
    payload = {
        "task_id": vg.id,
        "user_id": user.id,
        "kind": "video_generation",
    }
    outbox = OutboxEvent(kind="video_generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        winner = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.user_id == user.id,
                    VideoGeneration.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if winner is not None:
            _ensure_idempotent_replay_matches(winner, request_fingerprint)
            return await _generation_out(db, winner)
        raise _http("idempotency_conflict", "idempotency_key conflict", 409) from exc
    await db.refresh(vg)
    await invalidate_balance_cache(user.id)
    await _publish_video_queued(payload)
    return await _generation_out(db, vg)


async def _publish_video_queued(payload: dict[str, Any]) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_video_generation",
            payload["task_id"],
            _job_id=arq_job_id(
                "video_generation",
                payload["task_id"],
                payload.get("outbox_id"),
            ),
        )
        redis = get_redis()
        await publish_sse_event(
            redis,
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=EV_VIDEO_QUEUED,
            data={
                "video_generation_id": payload["task_id"],
                "kind": "video_generation",
                "status": VideoGenerationStatus.QUEUED.value,
                "stage": VideoGenerationStage.QUEUED.value,
                "progress_pct": 0,
                "video_id": None,
                "error_code": None,
            },
        )
    except Exception:
        task_publish_errors_total.labels(kind="video_generation").inc()
        logger.warning(
            "best-effort video queued publish failed task_id=%s",
            payload.get("task_id"),
            exc_info=True,
        )


@router.post(
    "/generations",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_video_generation(
    body: VideoCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "account_mode_forbidden", "video generation requires wallet mode", 403
        )
    existing = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.user_id == user.id,
                VideoGeneration.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        _ensure_idempotent_replay_matches(existing, _request_fingerprint(body))
        return await _generation_out(db, existing)
    return await _create_video_generation_record(db, body, user, request=request)


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        ts, row_id = cursor.split("|", 1)
        created_at = datetime.fromisoformat(ts)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must include timezone")
        return created_at, row_id
    except (ValueError, TypeError):
        raise _http("invalid_cursor", "cursor is invalid", 422)


def _encode_cursor(row: VideoGeneration) -> str:
    return f"{row.created_at.isoformat()}|{row.id}"


@router.get("/generations", response_model=VideoGenerationsOut)
async def list_video_generations(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=_VIDEO_LIST_LIMIT_MAX),
) -> VideoGenerationsOut:
    stmt = select(VideoGeneration).where(VideoGeneration.user_id == user.id)
    if status:
        stmt = stmt.where(VideoGeneration.status == status)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        created_at, row_id = decoded
        stmt = stmt.where(
            or_(
                VideoGeneration.created_at < created_at,
                and_(
                    VideoGeneration.created_at == created_at,
                    VideoGeneration.id < row_id,
                ),
            )
        )
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    VideoGeneration.created_at.desc(), VideoGeneration.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    generation_ids = [row.id for row in page]
    videos_by_generation_id: dict[str, Video] = {}
    if generation_ids:
        videos = (
            (
                await db.execute(
                    select(Video).where(
                        Video.owner_generation_id.in_(generation_ids),
                        Video.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        videos_by_generation_id = {video.owner_generation_id: video for video in videos}
    return VideoGenerationsOut(
        items=[
            await _generation_out(db, row, videos_by_generation_id.get(row.id))
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/generations/{generation_id}", response_model=VideoGenerationOut)
async def get_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    row = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "video generation not found", 404)
    return await _generation_out(db, row)


@router.post(
    "/generations/{generation_id}/cancel",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def cancel_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    row = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == generation_id, VideoGeneration.user_id == user.id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "video generation not found", 404)
    now = datetime.now(timezone.utc)
    if row.status not in _VIDEO_TERMINAL_STATUSES:
        row.cancel_requested_at = row.cancel_requested_at or now
        if (
            row.status == VideoGenerationStatus.QUEUED.value
            and not row.provider_task_id
        ):
            row.status = VideoGenerationStatus.CANCELED.value
            row.progress_stage = VideoGenerationStage.FINISHED.value
            row.progress_pct = 100
            row.error_code = "canceled"
            row.error_message = "cancelled before upstream submission"
            row.finished_at = now
            await billing_core.release(
                db,
                user.id,
                ref_type="video_generation",
                ref_id=row.id,
                idempotency_key=f"video_generation:release:{row.id}",
                meta={
                    "model": row.model,
                    "action": row.action,
                    "provider_name": row.provider_name,
                    "billing_decision": "pre_submit_cancel_release",
                },
            )
    await db.commit()
    await db.refresh(row)
    await invalidate_balance_cache(user.id)
    try:
        await publish_sse_event(
            get_redis(),
            user_id=user.id,
            channel=task_channel(row.id),
            event_name=EV_VIDEO_CANCELED,
            data={
                "video_generation_id": row.id,
                "kind": "video_generation",
                "status": row.status,
                "stage": row.progress_stage,
                "progress_pct": row.progress_pct,
                "video_id": None,
                "error_code": row.error_code,
            },
        )
    except Exception:
        logger.warning("video cancel SSE publish failed id=%s", row.id, exc_info=True)
    return await _generation_out(db, row)


@router.post(
    "/generations/{generation_id}/retry",
    response_model=VideoGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def retry_video_generation(
    generation_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "account_mode_forbidden", "video generation requires wallet mode", 403
        )
    row = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "video generation not found", 404)
    if row.status not in {
        VideoGenerationStatus.FAILED.value,
        VideoGenerationStatus.CANCELED.value,
        VideoGenerationStatus.EXPIRED.value,
    }:
        raise _http(
            "video_retry_not_terminal",
            "only failed, canceled, or expired video tasks can be retried",
            409,
            status=row.status,
        )
    reference_snapshots = []
    raw_reference_media = (row.upstream_request or {}).get("reference_media")
    if isinstance(raw_reference_media, list):
        reference_snapshots = [
            item for item in raw_reference_media if isinstance(item, dict)
        ]
    reference_inputs: list[VideoReferenceMediaIn] = []
    valid_reference_snapshots: list[dict[str, Any]] = []
    for item in reference_snapshots:
        if item.get("kind") not in {"image", "video"}:
            continue
        try:
            reference_input = VideoReferenceMediaIn(
                kind=str(item.get("kind")),
                image_id=item.get("image_id")
                if isinstance(item.get("image_id"), str)
                else None,
                video_id=item.get("video_id")
                if isinstance(item.get("video_id"), str)
                else None,
                url=item.get("url") if isinstance(item.get("url"), str) else None,
                label=item.get("label") if isinstance(item.get("label"), str) else None,
            )
            reference_inputs.append(reference_input)
            valid_reference_snapshots.append(item)
        except ValueError:
            logger.warning(
                "video retry skipped invalid reference snapshot id=%s snapshot=%r",
                row.id,
                item,
            )
    if row.action == "reference" and not reference_inputs:
        raise _http(
            "reference_media_missing",
            "original reference media snapshot is missing; create a new video task",
            409,
        )
    try:
        body = VideoCreateIn(
            action=row.action,  # type: ignore[arg-type]
            model=row.model,
            prompt=row.prompt,
            input_image_id=row.input_image_id,
            reference_media=reference_inputs,
            duration_s=row.duration_s,
            resolution=row.resolution,  # type: ignore[arg-type]
            aspect_ratio=row.aspect_ratio,
            generate_audio=row.generate_audio,
            seed=row.seed,
            watermark=row.watermark,
            idempotency_key=f"retry:{row.id}:{row.updated_at.isoformat()}",
        )
    except ValueError as exc:
        raise _http(
            "invalid_retry_request",
            "original video generation request is no longer valid",
            422,
        ) from exc
    return await _create_video_generation_record(
        db,
        body,
        user,
        request=request,
        input_image_snapshot=(
            row.input_image_storage_key,
            row.input_image_sha256,
            (row.upstream_request or {}).get("input_image_url")
            if isinstance((row.upstream_request or {}).get("input_image_url"), str)
            else None,
        ),
        reference_media_snapshot=valid_reference_snapshots,
    )


@router.delete(
    "/{video_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def delete_video(
    video_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user.id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    video.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=204)


def _fs_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    p = (root / key_path).resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise _http("invalid_path", "storage path escapes root", 400) from exc
    return p


def _open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _http(
                "invalid_path", "symlink storage paths are not allowed", 400
            ) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(st.st_size)
    except Exception:
        os.close(fd)
        raise


def _iter_file_and_close(
    f: BinaryIO,
    *,
    start: int = 0,
    length: int | None = None,
) -> Iterator[bytes]:
    try:
        if start:
            f.seek(start)
        remaining = length
        while remaining is None or remaining > 0:
            chunk_size = (
                1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            )
            data = f.read(chunk_size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data
    finally:
        f.close()


def _quote_etag(etag: str) -> str:
    value = etag.strip()
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"'


def _etag_matches(if_none_match: str | None, quoted_etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == quoted_etag:
            return True
    return False


def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    if not range_header.startswith("bytes=") or "," in range_header:
        return None
    spec = range_header.removeprefix("bytes=").strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def _media_response(
    request: Request,
    path: Path,
    *,
    media_type: str,
    etag: str,
    last_modified: datetime | None,
    immutable: bool,
    download_filename: str | None = None,
) -> Response:
    quoted_etag = _quote_etag(etag)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": (
            "private, max-age=31536000, immutable"
            if immutable
            else "private, max-age=3600"
        ),
        "ETag": quoted_etag,
    }
    if download_filename:
        headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
    if last_modified is not None:
        headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)
    if _etag_matches(request.headers.get("if-none-match"), quoted_etag):
        return Response(status_code=304, headers=headers)
    f, size = _open_regular_file_no_symlink(path)
    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            f.close()
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        start, end = parsed
        length = end - start + 1
        return StreamingResponse(
            _iter_file_and_close(f, start=start, length=length),
            status_code=206,
            media_type=media_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )
    return StreamingResponse(
        _iter_file_and_close(f),
        media_type=media_type,
        headers={**headers, "Content-Length": str(size)},
    )


async def _owned_video(db: AsyncSession, user_id: str, video_id: str) -> Video:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    return video


@router.get("/reference/{video_id}/binary")
async def reference_video_binary(
    video_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=80),
) -> Response:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    metadata = video.metadata_jsonb or {}
    if not _reference_token_is_valid(
        metadata,
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
        token=token,
        updated_at=video.updated_at,
    ):
        raise _http("not_found", "video not found", 404)
    if variant:
        if variant != VIDEO_REFERENCE_VIDEO_KIND:
            raise _http("not_found", "video not found", 404)
        variant_meta = video_reference_variant_metadata(video)
        if variant_meta is None:
            raise _http("not_found", "video not found", 404)
        return _media_response(
            request,
            _fs_path(str(variant_meta["storage_key"])),
            media_type=VIDEO_REFERENCE_VIDEO_MIME,
            etag=str(variant_meta["sha256"]),
            last_modified=video.updated_at,
            immutable=True,
        )
    return _media_response(
        request,
        _fs_path(video.storage_key),
        media_type=video.mime,
        etag=video.etag or video.sha256,
        last_modified=video.updated_at,
        immutable=True,
    )


@router.get("/{video_id}/binary")
async def video_binary(
    video_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    download: bool = Query(False),
) -> Response:
    video = await _owned_video(db, user.id, video_id)
    extension = Path(video.storage_key).suffix.lower() or ".mp4"
    return _media_response(
        request,
        _fs_path(video.storage_key),
        media_type=video.mime,
        etag=video.etag or video.sha256,
        last_modified=video.updated_at,
        immutable=True,
        download_filename=f"lumen-video-{video.id}{extension}" if download else None,
    )


@router.get("/{video_id}/poster")
async def video_poster(
    video_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    video = await _owned_video(db, user.id, video_id)
    if not video.poster_storage_key:
        raise _http("not_found", "poster not found", 404)
    return _media_response(
        request,
        _fs_path(video.poster_storage_key),
        media_type="image/jpeg",
        etag=f"{video.etag}:poster",
        last_modified=video.updated_at,
        immutable=True,
    )
