"""Video generation API and media endpoints."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterable, Iterator, Literal, cast
from urllib.parse import parse_qs, urlencode, urlsplit

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
from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    VideoGenerationStage,
    VideoGenerationStatus,
    task_channel,
)
from lumen_core.models import (
    Image,
    OutboxEvent,
    PricingRule,
    User,
    Video,
    VideoGeneration,
)
from lumen_core.models import new_uuid7
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    MoneyOut,
    VideoAction,
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoModelOptionOut,
    VideoOptionsOut,
    VideoOut,
    VideoPriceOptionOut,
    VideoPricingVariant,
    VideoReferenceMediaIn,
    VideoReferenceMediaOut,
    VideoResolution,
    VideoTemporaryDownloadOut,
    VideoUploadOut,
    normalize_asset_reference_url,
)
from lumen_core.url_security import is_private_host, resolve_public_http_target
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
    split_video_resolution_pricing_variant,
    video_billing_model,
    video_pricing_variant,
)
from lumen_core.video_providers import (
    VIDEO_ACTIONS,
    parse_video_provider_config_json,
    seedance_20_allowed_resolutions,
    seedance_20_variant,
    select_video_provider,
)
from lumen_core.volcano_assets import (
    volcano_asset_reference_url,
    volcano_asset_safe_filename,
)

from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services import asset_ref_service
from ..canvas_services.task_guard import reject_canvas_retry
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..public_urls import resolve_public_base_url
from ..runtime_settings import get_setting
from ..services.video_publish import publish_video_queued
from ..sse_publish import publish_sse_event  # noqa: F401 - test patch surface
from ..volcano_asset_media import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_MIME,
    volcano_asset_video_variant_metadata,
)
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
from ..video_options import reference_media_limits_for_model
from ._video_reference_media import build_reference_media_snapshots


router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_VIDEO_DURATIONS = list(SUPPORTED_VIDEO_DURATIONS_S)
_DEFAULT_VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"]
_VIDEO_RESOLUTION_ORDER = {
    value: index for index, value in enumerate(_DEFAULT_VIDEO_RESOLUTIONS)
}
_VOLCANO_NEWAPI_RESOLUTIONS = ("720p",)
_HAPPYHORSE_RESOLUTIONS = ("720p", "1080p")
_OMNI_FLASH_RESOLUTIONS = ("720p", "1080p", "4k")
_OMNI_FLASH_DURATIONS = tuple(range(6, 11))
_HAPPYHORSE_ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
_OMNI_FLASH_ASPECT_RATIOS = ("adaptive", "16:9", "9:16", "1:1")
_VIDEO_ACTION_VALUES = cast(tuple[VideoAction, ...], VIDEO_ACTIONS)
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
_TEMPORARY_DOWNLOAD_MIN_REMAINING = timedelta(seconds=60)
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


def _temporary_video_download_out(
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


def _generation_elapsed_ms(
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


async def _inspect_reference_video_upload(file: UploadFile) -> tuple[int, str, bytes]:
    size = 0
    digest = hashlib.sha256()
    header = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > _VIDEO_REFERENCE_UPLOAD_MAX_BYTES:
            raise _http(
                "too_large",
                f"file exceeds {_VIDEO_REFERENCE_UPLOAD_MAX_BYTES // (1024 * 1024)}MB",
                413,
            )
        digest.update(chunk)
        if len(header) < 12:
            header.extend(chunk[: 12 - len(header)])
    if size == 0:
        raise _http("empty_file", "empty file", 400)
    await file.seek(0)
    return size, digest.hexdigest(), bytes(header)


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
    if variant == VOLCANO_ASSET_VIDEO_KIND:
        return volcano_asset_reference_url(
            public_base_url,
            resource_id=video.id,
            asset_type="Video",
            token=token,
        )
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
    if variant == VOLCANO_ASSET_IMAGE_KIND:
        return volcano_asset_reference_url(
            public_base_url,
            resource_id=image.id,
            asset_type="Image",
            token=token,
        )
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
    return getattr(provider, "kind", None) in {"dashscope", "volcano_newapi"}


def _provider_prefers_public_media_url(provider: Any) -> bool:
    return _provider_requires_public_media(provider) or getattr(
        provider, "kind", None
    ) in {"volcano_third_party", "volcano_newapi", "omni_flash"}


def _write_new_file_atomic(path: Path, source: BinaryIO) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            source.seek(0)
            shutil.copyfileobj(source, f, length=1024 * 1024)
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
    if kind not in {"image", "video", "audio"}:
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
        ref_id=snapshot.get("ref_id")
        if isinstance(snapshot.get("ref_id"), str)
        else None,
        mime=snapshot.get("mime") if isinstance(snapshot.get("mime"), str) else None,
    )


def _reference_snapshot_ref_id(
    kind: str,
    index: int,
    raw: Any,
    *,
    strict: bool = True,
) -> str:
    default = f"ref:{kind}:{index}"
    if raw is None or isinstance(raw, str) and not raw.strip():
        return default
    if not isinstance(raw, str):
        if not strict:
            return default
        raise _http(
            "invalid_reference_ref_id",
            "reference media ref_id is invalid",
            409,
        )
    value = raw.strip().lower()
    parts = value.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and 1 <= int(parts[2]) <= 999
    ):
        return value
    if not strict:
        return default
    raise _http(
        "invalid_reference_ref_id",
        "reference media ref_id is invalid",
        409,
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
        submission_epoch=int(getattr(row, "submission_epoch", 0) or 0),
        provider_name=row.provider_name,
        provider_kind=row.provider_kind,
        est_token_upper=row.est_token_upper,
        est_cost=_money(row.est_cost_micro),
        billed_tokens=row.billed_tokens,
        billed_cost=_money(row.billed_cost_micro)
        if row.billed_cost_micro is not None
        else None,
        video=_video_out(video) if video is not None else None,
        temporary_download=_temporary_video_download_out(row),
        elapsed_ms=_generation_elapsed_ms(row),
        error_code=row.error_code,
        error_message=row.error_message,
        diagnostics=_public_video_diagnostics(row.diagnostics),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        submit_started_at=getattr(row, "submit_started_at", None),
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


def _video_upload_out(video: Video, *, created: bool) -> VideoUploadOut:
    return VideoUploadOut(**_video_out(video).model_dump(), created=created)


@router.post(
    "/upload",
    response_model=VideoUploadOut,
    dependencies=[Depends(verify_csrf)],
)
async def upload_reference_video(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
) -> VideoUploadOut:
    mime, ext = _reference_upload_ext(file)
    size, sha, header = await _inspect_reference_video_upload(file)
    if not _looks_like_reference_video(header):
        raise _http(
            "invalid_video_file",
            "reference video must be a valid mp4 or mov file",
            415,
        )

    # Serialize dedupe/quota accounting for each user. The file body has
    # already been validated and rewound, so this lock covers only DB checks
    # plus the final atomic filesystem copy.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
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
        existing_path = _fs_path(existing.storage_key)
        if not existing_path.is_file():
            repaired_key = _reference_video_upload_key(user.id, existing.id, ext)
            repaired_path = _fs_path(repaired_key)
            try:
                await asyncio.to_thread(
                    _write_new_file_atomic,
                    repaired_path,
                    file.file,
                )
                existing.storage_key = repaired_key
                existing.mime = mime
                existing.size_bytes = size
                existing.sha256 = sha
                existing.etag = sha
                metadata = dict(existing.metadata_jsonb or {})
                metadata["filename"] = file.filename or ""
                metadata["source"] = "uploaded_reference"
                existing.metadata_jsonb = metadata
                _ensure_reference_video_access_token(existing)
                await db.commit()
            except Exception:
                await db.rollback()
                await asyncio.to_thread(_unlink_file_if_exists, repaired_path)
                raise
            await db.refresh(existing)
            return _video_upload_out(existing, created=False)
        _ensure_reference_video_access_token(existing)
        await db.commit()
        await db.refresh(existing)
        return _video_upload_out(existing, created=False)
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
    if int(total_bytes or 0) + size > _VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES:
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
        size_bytes=size,
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
        await asyncio.to_thread(_write_new_file_atomic, path, file.file)
        await db.commit()
    except Exception:
        await db.rollback()
        await asyncio.to_thread(_unlink_file_if_exists, path)
        raise
    await db.refresh(video)
    return _video_upload_out(video, created=True)


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
    if _is_seedance_20_model(model, upstream_model):
        positive_durations = [item for item in positive_durations if item >= 4]
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


def _parse_video_action(value: str) -> VideoAction | None:
    if value not in _VIDEO_ACTION_VALUES:
        return None
    return cast(VideoAction, value)


def _video_price_action_for_provider(
    provider_kind: str,
    action: VideoAction,
) -> VideoPricingVariant:
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
    action: VideoAction,
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
    return seedance_20_variant(*identifiers) == "fast"


def _is_seedance_20_mini_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) == "mini"


def _is_seedance_20_standard_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) == "standard"


def _is_seedance_20_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) is not None


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
    seedance_resolutions = seedance_20_allowed_resolutions(model, upstream_model)
    if seedance_resolutions is not None:
        allowed = set(seedance_resolutions)
        return [resolution for resolution in available if resolution in allowed]
    return [resolution for resolution in available if resolution != "4k"]


def _video_resolution_options_for_provider(
    provider_kind: str,
    model: str,
    *,
    upstream_model: str | None = None,
    available_resolutions: Iterable[str] | None = None,
) -> list[str]:
    available = _ordered_video_resolutions(
        available_resolutions or _DEFAULT_VIDEO_RESOLUTIONS
    )
    if provider_kind == "volcano_newapi":
        allowed = set(_VOLCANO_NEWAPI_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    return _video_resolution_options_for_model(
        model,
        upstream_model=upstream_model,
        available_resolutions=available,
    )


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
        raw_action, resolution = split_video_resolution_pricing_variant(row.variant)
        if raw_action not in VIDEO_PRICING_VARIANTS:
            continue
        action = cast(VideoPricingVariant, raw_action)
        out.append(
            VideoPriceOptionOut(
                model=row.key,
                action=action,
                resolution=resolution,
                variant=row.variant,
                price=_money(row.price_micro),
                enabled=row.enabled,
                note=row.note,
            )
        )
    return out


def _has_video_price(
    price_pairs: Collection[tuple[str, VideoPricingVariant, str | None]],
    *,
    model: str,
    action: VideoPricingVariant,
    resolutions: list[str] | tuple[str, ...] | None = None,
) -> bool:
    def has(action_name: VideoPricingVariant, resolution: str | None) -> bool:
        return (model, action_name, resolution) in price_pairs or (
            model,
            action_name,
            None,
        ) in price_pairs

    resolution_options: Iterable[str | None] = resolutions if resolutions else (None,)
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


def _forbidden_video_options() -> VideoOptionsOut:
    return VideoOptionsOut(
        enabled=False,
        models=[],
        durations_s=[],
        resolutions=[],
        aspect_ratios=list(_DEFAULT_VIDEO_ASPECT_RATIOS),
        generate_audio=False,
        pricing=[],
        hold_estimates={},
        unavailable_reason="account_mode_forbidden",
    )


@router.get("/options", response_model=VideoOptionsOut)
async def video_options(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoOptionsOut:
    if getattr(_user, "account_mode", "wallet") != "wallet":
        return _forbidden_video_options()
    return await _wallet_video_options(db)


async def _wallet_video_options(db: AsyncSession) -> VideoOptionsOut:
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
    price_pairs: set[tuple[str, VideoPricingVariant, str | None]] = {
        (item.model, item.action, item.resolution) for item in prices
    }
    durations, resolutions = _estimate_pairs(estimates)
    global_durations = _duration_options(estimates)

    model_actions: dict[str, set[VideoAction]] = {}
    model_durations: dict[str, set[int]] = {}
    model_action_durations: dict[str, dict[VideoAction, set[int]]] = {}
    model_action_resolution_durations: dict[
        str, dict[VideoAction, dict[str, set[int]]]
    ] = {}
    model_resolutions: dict[str, set[str]] = {}
    model_billing_models: dict[str, dict[str, str]] = {}
    for provider in providers:
        mapping = provider.models or {}
        for key in mapping:
            if ":" not in key:
                for action in _VIDEO_ACTION_VALUES:
                    if not provider.supports(key, action):
                        continue
                    upstream_model = provider.upstream_model_for(key, action)
                    billing_model = video_billing_model(key, upstream_model)
                    allowed_resolutions = _video_resolution_options_for_provider(
                        provider.kind,
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
            model, raw_action = key.rsplit(":", 1)
            parsed_action = _parse_video_action(raw_action)
            if parsed_action is None or not provider.supports(model, parsed_action):
                continue
            upstream_model = provider.upstream_model_for(model, parsed_action)
            billing_model = video_billing_model(model, upstream_model)
            allowed_resolutions = _video_resolution_options_for_provider(
                provider.kind,
                model,
                upstream_model=upstream_model,
                available_resolutions=resolutions,
            )
            price_action = _video_price_action_for_provider(
                provider.kind, parsed_action
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
                model=model,
                upstream_model=upstream_model,
                provider_kind=provider.kind,
                action=parsed_action,
                resolutions=action_resolutions,
                fallback_durations=durations or global_durations,
            )
            if action_resolutions:
                model_actions.setdefault(model, set()).add(parsed_action)
                model_durations.setdefault(model, set()).update(action_durations)
                model_action_durations.setdefault(model, {}).setdefault(
                    parsed_action, set()
                ).update(action_durations)
                action_resolution_durations = (
                    model_action_resolution_durations.setdefault(model, {}).setdefault(
                        parsed_action, {}
                    )
                )
                for resolution in action_resolutions:
                    resolution_durations = _duration_options_for_provider_action(
                        estimates,
                        model=model,
                        upstream_model=upstream_model,
                        provider_kind=provider.kind,
                        action=parsed_action,
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
                model_billing_models.setdefault(model, {})[parsed_action] = (
                    billing_model
                )
    if enabled and not model_actions and unavailable_reason is None:
        unavailable_reason = "video_provider_or_pricing_missing"
    model_options: list[VideoModelOptionOut] = []
    for model, actions in sorted(model_actions.items()):
        sorted_actions = sorted(actions)
        billing_models: dict[str, str] = {
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
                actions=sorted_actions,
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
                resolutions=cast(
                    list[VideoResolution],
                    _ordered_video_resolutions(model_resolutions.get(model, set())),
                ),
                reference_media_limits=reference_media_limits_for_model(
                    providers, model, actions
                ),
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
    model_resolutions = _video_resolution_options_for_provider(
        provider.kind,
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
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise _http(
            "invalid_reference_url",
            "reference URL must be https or asset://",
            422,
        )
    if parts.username or parts.password:
        raise _http(
            "invalid_reference_url",
            "reference URL must not include credentials",
            422,
        )
    try:
        parts.port
    except ValueError as exc:
        raise _http(
            "invalid_reference_url",
            "reference URL port is invalid",
            422,
        ) from exc
    if is_private_host(parts.hostname):
        raise _http(
            "invalid_reference_url",
            "reference URL host is not allowed",
            422,
        )
    return value


async def _resolve_reference_url(raw_url: str) -> str:
    value = _validate_reference_url(raw_url)
    if normalize_asset_reference_url(value) is not None:
        return value
    try:
        target = await resolve_public_http_target(
            value,
            allow_http=False,
            strip_trailing_slash=False,
        )
    except ValueError as exc:
        raise _http(
            "invalid_reference_url",
            "reference URL host is not allowed",
            422,
        ) from exc
    return target.url


async def _reference_media_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    items: list[VideoReferenceMediaIn],
    fallback_snapshots: list[dict[str, Any]] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
) -> list[dict[str, Any]]:
    return await build_reference_media_snapshots(
        db,
        user_id=user_id,
        items=items,
        fallback_snapshots=fallback_snapshots,
        reference_public_base_url=reference_public_base_url,
        required_public_media=required_public_media,
        reference_id=_reference_snapshot_ref_id,
        resolve_url=_resolve_reference_url,
        image_public_url=_reference_image_upstream_public_url,
        video_public_url=_reference_video_upstream_public_url,
        http_error=_http,
    )


def _validate_provider_reference_media(
    provider_kind: str,
    reference_snapshots: list[dict[str, Any]],
) -> None:
    if provider_kind == "dashscope" and any(
        item.get("kind") == "video"
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise _http(
            "unsupported_reference_media",
            "HappyHorse reference-to-video supports image references only",
            422,
        )
    if provider_kind == "omni_flash" and any(
        item.get("kind") in {"video", "audio"}
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise _http(
            "unsupported_reference_media",
            "Omni Flash unified video create supports image references only",
            422,
        )
    image_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "image"
    )
    video_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "video"
    )
    audio_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "audio"
    )
    if provider_kind not in {"volcano", "volcano_newapi"} and audio_count:
        raise _http(
            "unsupported_reference_media",
            "reference audio is only available for Volcano Seedance providers",
            422,
        )
    if provider_kind not in {"volcano", "volcano_newapi"}:
        return

    image_limit = 9 if provider_kind == "volcano" else 4
    audio_limit = 3 if provider_kind == "volcano" else 1
    provider_label = (
        "Volcano Seedance" if provider_kind == "volcano" else "Volcano New API"
    )
    if image_count > image_limit:
        raise _http(
            "too_many_reference_images",
            f"{provider_label} supports at most {image_limit} reference images",
            422,
        )
    if video_count > 3:
        raise _http(
            "too_many_reference_videos",
            f"{provider_label} supports at most 3 reference videos",
            422,
        )
    if audio_count > audio_limit:
        raise _http(
            "too_many_reference_audios",
            f"{provider_label} supports at most {audio_limit} reference audio"
            + (" references" if audio_limit != 1 else ""),
            422,
        )
    if audio_count and not (image_count or video_count):
        raise _http(
            "reference_audio_requires_visual",
            "reference audio must be combined with an image or video",
            422,
        )


async def _create_video_generation_record(
    db: AsyncSession,
    body: VideoCreateIn,
    user: CurrentUser,
    *,
    request: Request | None = None,
    input_image_snapshot: tuple[str | None, str | None, str | None] | None = None,
    reference_media_snapshot: list[dict[str, Any]] | None = None,
    workflow_metadata: dict[str, Any] | None = None,
    defer_commit: bool = False,
    deferred_publish_payload: dict[str, Any] | None = None,
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
    _validate_provider_reference_media(provider.kind, reference_snapshots)
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
    try:
        db.add(vg)
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
        payload = {
            "task_id": vg.id,
            "user_id": user.id,
            "kind": "video_generation",
        }
        outbox = OutboxEvent(
            kind="video_generation", payload=payload, published_at=None
        )
        db.add(outbox)
        await db.flush()
        payload["outbox_id"] = str(outbox.id)
        outbox.payload = dict(payload)
        if deferred_publish_payload is not None:
            deferred_publish_payload.update(payload)
        if not defer_commit:
            await db.commit()
    except billing_core.BillingError as exc:
        if not defer_commit:
            await db.rollback()
        raise _http(exc.code, exc.message, exc.status_code) from exc
    except IntegrityError as exc:
        if defer_commit:
            raise _http(
                "idempotency_conflict",
                "idempotency_key conflict",
                409,
            ) from exc
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
    if not defer_commit:
        await db.refresh(vg)
        await invalidate_balance_cache(user.id)
        await publish_video_queued(payload)
    return await _generation_out(db, vg)


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
        videos_by_generation_id = {
            video.owner_generation_id: video
            for video in videos
            if video.owner_generation_id is not None
        }
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
    balance_changed = False
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
            tx = await billing_core.release(
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
            if tx is None:
                await db.rollback()
                raise _http(
                    "video_hold_release_missing",
                    "video hold release transaction was not created",
                    409,
                )
            balance_changed = True
            db.add(
                OutboxEvent(
                    kind="sse",
                    payload={
                        "user_id": user.id,
                        "channel": task_channel(row.id),
                        "event_name": EV_VIDEO_CANCELED,
                        "data": {
                            "video_generation_id": row.id,
                            "kind": "video_generation",
                            "status": row.status,
                            "stage": row.progress_stage,
                            "progress_pct": row.progress_pct,
                            "submission_epoch": int(
                                getattr(row, "submission_epoch", 0) or 0
                            ),
                            "video_id": None,
                            "error_code": row.error_code,
                            "error_message": row.error_message,
                        },
                    },
                    published_at=None,
                )
            )
    await db.commit()
    await db.refresh(row)
    if balance_changed:
        await invalidate_balance_cache(user.id)
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
            select(VideoGeneration)
            .where(
                VideoGeneration.id == generation_id,
                VideoGeneration.user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "video generation not found", 404)
    reject_canvas_retry(row)
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
        raw_kind = item.get("kind")
        if raw_kind not in {"image", "video", "audio"}:
            continue
        kind = cast(Literal["image", "video", "audio"], raw_kind)
        try:
            reference_input = VideoReferenceMediaIn(
                kind=kind,
                image_id=item.get("image_id")
                if isinstance(item.get("image_id"), str)
                else None,
                video_id=item.get("video_id")
                if isinstance(item.get("video_id"), str)
                else None,
                url=item.get("url") if isinstance(item.get("url"), str) else None,
                label=item.get("label") if isinstance(item.get("label"), str) else None,
                ref_id=item.get("ref_id")
                if isinstance(item.get("ref_id"), str)
                else None,
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
        body = VideoCreateIn.model_validate(
            {
                "action": row.action,
                "model": row.model,
                "prompt": row.prompt,
                "input_image_id": row.input_image_id,
                "reference_media": reference_inputs,
                "duration_s": row.duration_s,
                "resolution": row.resolution,
                "aspect_ratio": row.aspect_ratio,
                "generate_audio": row.generate_audio,
                "seed": row.seed,
                "watermark": row.watermark,
                "idempotency_key": f"retry:{row.id}:{row.updated_at.isoformat()}",
            }
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
    await asset_ref_service.ensure_asset_not_canvas_referenced(db, video_id=video.id)
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
    inline_filename: str | None = None,
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
    elif inline_filename:
        headers["Content-Disposition"] = f'inline; filename="{inline_filename}"'
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
        if variant == VIDEO_REFERENCE_VIDEO_KIND:
            variant_meta = video_reference_variant_metadata(video)
            media_type = VIDEO_REFERENCE_VIDEO_MIME
        elif variant == VOLCANO_ASSET_VIDEO_KIND:
            variant_meta = volcano_asset_video_variant_metadata(video)
            media_type = VOLCANO_ASSET_VIDEO_MIME
        else:
            raise _http("not_found", "video not found", 404)
        if variant_meta is None:
            raise _http("not_found", "video not found", 404)
        return _media_response(
            request,
            _fs_path(str(variant_meta["storage_key"])),
            media_type=media_type,
            etag=str(variant_meta["sha256"]),
            last_modified=video.updated_at,
            immutable=True,
            inline_filename=(
                volcano_asset_safe_filename(video.id, asset_type="Video")
                if variant == VOLCANO_ASSET_VIDEO_KIND
                else None
            ),
        )
    return _media_response(
        request,
        _fs_path(video.storage_key),
        media_type=video.mime,
        etag=video.etag or video.sha256,
        last_modified=video.updated_at,
        immutable=True,
    )


@router.get("/reference/{video_id}/binary/{filename}")
async def reference_video_binary_named(
    video_id: str,
    filename: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=80),
) -> Response:
    expected = volcano_asset_safe_filename(video_id, asset_type="Video")
    if filename != expected or variant != VOLCANO_ASSET_VIDEO_KIND:
        raise _http("not_found", "video not found", 404)
    return await reference_video_binary(
        video_id,
        request,
        db,
        token=token,
        variant=variant,
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
