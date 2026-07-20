"""Video generation API and media endpoints."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import secrets
import shutil
import stat
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterator, Literal, cast

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
    User,
    Video,
    VideoGeneration,
)
from lumen_core.schemas import (
    VideoCreateIn,
    VideoGenerationOut,
    VideoGenerationsOut,
    VideoOptionsOut,
    VideoReferenceMediaIn,
    VideoUploadOut,
)
from lumen_core.url_security import resolve_public_http_target
from lumen_core.volcano_assets import volcano_asset_safe_filename

from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services import asset_ref_service
from ..canvas_services.task_guard import reject_canvas_retry
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..public_urls import resolve_public_base_url
from ..services.video import options as video_options_service
from ..services.video import presentation as video_presentation
from ..services.video import reference_media as video_reference_service
from ..services.video import submission as video_submission_service
from ..services.video.errors import video_http_error
from ..services.video_publish import publish_video_queued
from ..sse_publish import publish_sse_event  # noqa: F401 - test patch surface
from ..volcano_asset_media import (
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_MIME,
    volcano_asset_video_variant_metadata,
)
from ..video_reference_images import (
    ensure_video_reference_image_variant,
)
from ..video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_KIND,
    VIDEO_REFERENCE_VIDEO_MIME,
    ensure_video_reference_video_variant,
    video_reference_variant_metadata,
)


router = APIRouter()
logger = logging.getLogger(__name__)

_VIDEO_LIST_LIMIT_MAX = 100
_VIDEO_REFERENCE_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
_VIDEO_REFERENCE_UPLOAD_MAX_COUNT = 20
_VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES = 1024 * 1024 * 1024
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
    return video_http_error(code, msg, http, **details)


_money = video_presentation.money
_video_binary_url = video_presentation.video_binary_url
_video_poster_url = video_presentation.video_poster_url
_temporary_video_download_out = video_presentation.temporary_video_download_out
_generation_elapsed_ms = video_presentation.generation_elapsed_ms
_video_out = video_presentation.video_out
_reference_media_out = video_presentation.reference_media_out
_is_internal_reference_url = video_presentation.is_internal_reference_url
_public_video_diagnostics = video_presentation.public_video_diagnostics
_generation_reference_media = video_presentation.generation_reference_media
_video_for_generation = video_presentation.video_for_generation
_generation_out = video_presentation.generation_out


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


_reference_token_expiry = video_reference_service.reference_token_expiry
_parse_reference_token_expiry = video_reference_service.parse_reference_token_expiry
_reference_token_is_valid = video_reference_service.reference_token_is_valid
_ensure_reference_access_token = video_reference_service.ensure_reference_access_token


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


_ensure_reference_video_access_token = (
    video_reference_service.ensure_reference_video_access_token
)
_reference_video_public_url = video_reference_service.reference_video_public_url
_ensure_reference_image_access_token = (
    video_reference_service.ensure_reference_image_access_token
)
_reference_image_public_url = video_reference_service.reference_image_public_url


async def _reference_image_upstream_public_url(
    db: AsyncSession,
    image: Image,
    public_base_url: str,
    *,
    required: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    return await video_reference_service.reference_image_upstream_public_url(
        db,
        image,
        public_base_url,
        required=required,
        variant_loader=ensure_video_reference_image_variant,
    )


async def _reference_video_upstream_public_url(
    db: AsyncSession,
    video: Video,
    public_base_url: str,
) -> tuple[str, dict[str, Any]]:
    return await video_reference_service.reference_video_upstream_public_url(
        db,
        video,
        public_base_url,
        variant_loader=ensure_video_reference_video_variant,
    )


_provider_requires_public_media = video_reference_service.provider_requires_public_media
_provider_prefers_public_media_url = (
    video_reference_service.provider_prefers_public_media_url
)


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


_reference_snapshot_ref_id = video_reference_service.reference_snapshot_ref_id


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    return await video_options_service.setting_raw(db, key)


async def _video_enabled(db: AsyncSession) -> bool:
    return await video_options_service.video_enabled(db)


async def _billing_enabled(db: AsyncSession) -> bool:
    return await video_options_service.billing_enabled(db)


async def _allow_negative_balance(db: AsyncSession) -> bool:
    return await video_options_service.allow_negative_balance(db)


async def _video_provider_state(db: AsyncSession):
    return await video_options_service.video_provider_state(db)


async def _video_hold_estimates(db: AsyncSession) -> dict[str, Any]:
    return await video_options_service.video_hold_estimates(db)


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


_estimate_pairs = video_options_service.estimate_pairs
_duration_options = video_options_service.duration_options
_duration_options_for_model = video_options_service.duration_options_for_model
_estimate_duration_options_for_model_action = (
    video_options_service.estimate_duration_options_for_model_action
)
_parse_video_action = video_options_service.parse_video_action
_video_price_action_for_provider = video_options_service.video_price_action_for_provider
_duration_options_for_provider_action = (
    video_options_service.duration_options_for_provider_action
)
_ordered_video_resolutions = video_options_service.ordered_video_resolutions
_is_seedance_20_fast_model = video_options_service.is_seedance_20_fast_model
_is_seedance_20_mini_model = video_options_service.is_seedance_20_mini_model
_is_seedance_20_standard_model = video_options_service.is_seedance_20_standard_model
_is_seedance_20_model = video_options_service.is_seedance_20_model
_is_happyhorse_model = video_options_service.is_happyhorse_model
_is_omni_flash_model = video_options_service.is_omni_flash_model
_video_resolution_options_for_model = (
    video_options_service.video_resolution_options_for_model
)
_video_resolution_options_for_provider = (
    video_options_service.video_resolution_options_for_provider
)


_request_fingerprint = video_submission_service.request_fingerprint
_generation_request_fingerprint = (
    video_submission_service.generation_request_fingerprint
)
_needs_reference_public_base_url = (
    video_reference_service.needs_reference_public_base_url
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
    return await video_reference_service.reference_public_base_url(
        request,
        db,
        body,
        fallback_snapshots,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
        resolver=resolve_public_base_url,
    )


_ensure_idempotent_replay_matches = (
    video_submission_service.ensure_idempotent_replay_matches
)


_video_price_options = video_options_service.video_price_options
_has_video_price = video_options_service.has_video_price
_public_video_hold_estimates = video_options_service.public_video_hold_estimates
_forbidden_video_options = video_options_service.forbidden_video_options


@router.get("/options", response_model=VideoOptionsOut)
async def video_options(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoOptionsOut:
    return await video_options_service.get_video_options(
        _user,
        db,
        enabled_loader=_video_enabled,
        estimates_loader=_video_hold_estimates,
        provider_loader=_video_provider_state,
        price_loader=_video_price_options,
    )


async def _wallet_video_options(db: AsyncSession) -> VideoOptionsOut:
    return await video_options_service.get_wallet_video_options(
        db,
        enabled_loader=_video_enabled,
        estimates_loader=_video_hold_estimates,
        provider_loader=_video_provider_state,
        price_loader=_video_price_options,
    )


async def _require_video_create_ready(
    db: AsyncSession,
    body: VideoCreateIn,
) -> tuple[Any, dict[str, Any]]:
    return await video_options_service.require_video_create_ready(
        db,
        body,
        video_enabled_loader=_video_enabled,
        billing_enabled_loader=_billing_enabled,
        estimates_loader=_video_hold_estimates,
        provider_loader=_video_provider_state,
    )


async def _input_image_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str | None,
    fallback_snapshot: tuple[str | None, str | None, str | None] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
) -> tuple[str | None, str | None, str | None]:
    return await video_reference_service.input_image_snapshot(
        db,
        user_id=user_id,
        image_id=image_id,
        fallback_snapshot=fallback_snapshot,
        reference_public_base_url=reference_public_base_url,
        required_public_media=required_public_media,
        image_public_url=_reference_image_upstream_public_url,
    )


_validate_reference_url = video_reference_service.validate_reference_url


async def _resolve_reference_url(raw_url: str) -> str:
    return await video_reference_service.resolve_reference_url(
        raw_url,
        resolver=resolve_public_http_target,
    )


async def _reference_media_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    items: list[VideoReferenceMediaIn],
    fallback_snapshots: list[dict[str, Any]] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
) -> list[dict[str, Any]]:
    return await video_reference_service.reference_media_snapshots(
        db,
        user_id=user_id,
        items=items,
        fallback_snapshots=fallback_snapshots,
        reference_public_base_url=reference_public_base_url,
        required_public_media=required_public_media,
        resolve_url=_resolve_reference_url,
        image_public_url=_reference_image_upstream_public_url,
        video_public_url=_reference_video_upstream_public_url,
    )


_validate_provider_reference_media = (
    video_reference_service.validate_provider_reference_media
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
    return await video_submission_service.create_video_generation_record(
        db,
        body,
        user,
        request=request,
        input_image_snapshot=input_image_snapshot,
        reference_media_snapshot=reference_media_snapshot,
        workflow_metadata=workflow_metadata,
        defer_commit=defer_commit,
        deferred_publish_payload=deferred_publish_payload,
        require_ready=_require_video_create_ready,
        public_base_loader=_reference_public_base_url,
        input_snapshot_loader=_input_image_snapshot,
        reference_snapshot_loader=_reference_media_snapshots,
        reference_validator=_validate_provider_reference_media,
        allow_negative_loader=_allow_negative_balance,
        generation_renderer=_generation_out,
        balance_invalidator=invalidate_balance_cache,
        queued_publisher=publish_video_queued,
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
