"""Video generation API and media endpoints."""

from __future__ import annotations

import hashlib
import logging
import os  # noqa: F401 - test patch surface
import shutil  # noqa: F401 - test patch surface
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, BinaryIO

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core  # noqa: F401 - test patch surface
from lumen_core.constants import (  # noqa: F401 - compatibility patch surface
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import (
    Image,
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

from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services import asset_ref_service
from ..canvas_services.task_guard import reject_canvas_retry
from ..config import settings  # noqa: F401 - compatibility patch surface
from ..db import SessionLocal, get_db
from ..deps import CurrentUser, durable_session_id, verify_csrf
from ..public_urls import resolve_public_base_url
from ..services.video import options as video_options_service
from ..services.video import presentation as video_presentation
from ..services.video import reference_media as video_reference_service
from ..services.video import submission as video_submission_service
from ..services.active_user import ActiveUserSnapshot
from ..services.video.errors import video_http_error
from ..services.video_file_durability import (
    fsync_directory as _durability_fsync_directory,
    mkdir_parents_durable as _durability_mkdir_parents,
    write_new_file_atomic as _durability_write_new_file_atomic,
)
from ..services.video_publish import publish_video_queued
from ..services.video_storage_capacity import (
    VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
    build_video_storage_capacity_manager,
)
from ..services.video_storage_lifecycle import VideoStorageLifecycle
from ..services.video_upload_adoption import probe_reference_video_adoption
from ..sse_publish import publish_sse_event  # noqa: F401 - test patch surface
from ..video_reference_videos import (
    ensure_video_reference_video_variant,
)
from . import video_media_routes as _video_media_routes
from . import video_generation_routes as _video_generation_routes
from . import video_upload_routes as _video_upload_routes


router = APIRouter()
logger = logging.getLogger(__name__)

_VIDEO_LIST_LIMIT_MAX = 100
_VIDEO_REFERENCE_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
_VIDEO_REFERENCE_UPLOAD_MAX_COUNT = 20
_VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES = VIDEO_REFERENCE_STORAGE_QUOTA_BYTES
_VIDEO_REFERENCE_MIME_EXT = MappingProxyType(
    {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
    }
)
_VIDEO_TERMINAL_STATUSES = frozenset(
    {
        VideoGenerationStatus.SUCCEEDED.value,
        VideoGenerationStatus.FAILED.value,
        VideoGenerationStatus.CANCELED.value,
        VideoGenerationStatus.EXPIRED.value,
    }
)


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
_fs_path = _video_media_routes.fs_path
_open_regular_file_no_symlink = _video_media_routes.open_regular_file_no_symlink
_iter_file_and_close = _video_media_routes.iter_file_and_close
_quote_etag = _video_media_routes.quote_etag
_etag_matches = _video_media_routes.etag_matches
_parse_range = _video_media_routes.parse_range
_media_response = _video_media_routes.media_response
_owned_video = _video_media_routes.owned_video


def _video_storage_lifecycle() -> VideoStorageLifecycle:
    return VideoStorageLifecycle(settings.storage_root)


async def _probe_reference_video_adoption(
    *,
    video_id: str,
    user_id: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
) -> _video_upload_routes.VideoUploadAdoptionProbe:
    return await probe_reference_video_adoption(
        video_id=video_id,
        user_id=user_id,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size_bytes,
        session_factory=SessionLocal,
        video_model=Video,
        lifecycle_factory=_video_storage_lifecycle,
        logger=logger,
        adoption_type=_video_upload_routes.VideoUploadAdoption,
        probe_type=_video_upload_routes.VideoUploadAdoptionProbe,
    )


def _generation_route_dependencies() -> (
    _video_generation_routes.GenerationRouteDependencies
):
    return _video_generation_routes.GenerationRouteDependencies(
        http_error=_http,
        generation_out=_generation_out,
        decode_cursor=_decode_cursor,
        encode_cursor=_encode_cursor,
        terminal_statuses=_VIDEO_TERMINAL_STATUSES,
        invalidate_balance_cache=invalidate_balance_cache,
        allow_negative_balance=_allow_negative_balance,
        reject_canvas_retry=reject_canvas_retry,
        create_record=_create_video_generation_record,
        ensure_not_canvas_referenced=asset_ref_service.ensure_asset_not_canvas_referenced,
        storage_lifecycle=_video_storage_lifecycle(),
        logger=logger,
        list_limit_max=_VIDEO_LIST_LIMIT_MAX,
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
    _durability_write_new_file_atomic(
        path,
        source,
        mkdir_parents=_mkdir_parents_durable,
        fsync=_fsync_directory,
    )


def _fsync_directory(path: Path) -> None:
    _durability_fsync_directory(path)


def _mkdir_parents_durable(path: Path) -> None:
    _durability_mkdir_parents(path, fsync=_fsync_directory)


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


def _upload_dependencies() -> _video_upload_routes.UploadDependencies:
    return _video_upload_routes.UploadDependencies(
        reference_upload_ext=_reference_upload_ext,
        normalize_filename=_video_upload_routes.normalize_reference_filename,
        inspect_upload=_inspect_reference_video_upload,
        looks_like_video=_looks_like_reference_video,
        http_error=_http,
        fs_path=_fs_path,
        write_new_file_atomic=_write_new_file_atomic,
        unlink_file_if_exists=_unlink_file_if_exists,
        upload_key=_reference_video_upload_key,
        ensure_access_token=_ensure_reference_video_access_token,
        token_expiry=_reference_token_expiry,
        upload_out=_video_upload_out,
        storage_capacity=build_video_storage_capacity_manager(),
        storage_lifecycle=_video_storage_lifecycle(),
        probe_adoption=_probe_reference_video_adoption,
        logger=logger,
        max_count=_VIDEO_REFERENCE_UPLOAD_MAX_COUNT,
        total_max_bytes=_VIDEO_REFERENCE_UPLOAD_TOTAL_MAX_BYTES,
    )


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
    return await _video_upload_routes.upload_reference_video(
        user=user,
        db=db,
        file=file,
        deps=_upload_dependencies(),
    )


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
    active_user_snapshot: ActiveUserSnapshot | None = None,
    idempotency_serialized: bool = False,
) -> VideoGenerationOut:
    return await video_submission_service.create_video_generation_record(
        db,
        body,
        user,
        context=video_submission_service.VideoSubmissionContext(
            request=request,
            session_id=durable_session_id(request),
            active_user_snapshot=active_user_snapshot,
            idempotency_serialized=idempotency_serialized,
            input_image_snapshot=input_image_snapshot,
            reference_media_snapshot=reference_media_snapshot,
            workflow_metadata=workflow_metadata,
            defer_commit=defer_commit,
            deferred_publish_payload=deferred_publish_payload,
        ),
        services=video_submission_service.VideoSubmissionServices(
            require_ready=_require_video_create_ready,
            public_base_loader=_reference_public_base_url,
            input_snapshot_loader=_input_image_snapshot,
            reference_snapshot_loader=_reference_media_snapshots,
            reference_validator=_validate_provider_reference_media,
            allow_negative_loader=_allow_negative_balance,
            generation_renderer=_generation_out,
            balance_invalidator=invalidate_balance_cache,
            queued_publisher=publish_video_queued,
        ),
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
    return await _video_generation_routes.list_video_generations(
        user=user,
        db=db,
        limit=limit,
        cursor=cursor,
        status=status,
        deps=_generation_route_dependencies(),
    )


@router.get("/generations/{generation_id}", response_model=VideoGenerationOut)
async def get_video_generation(
    generation_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoGenerationOut:
    return await _video_generation_routes.get_video_generation(
        generation_id=generation_id,
        user=user,
        db=db,
        deps=_generation_route_dependencies(),
    )


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
    return await _video_generation_routes.cancel_video_generation(
        generation_id=generation_id,
        user=user,
        db=db,
        deps=_generation_route_dependencies(),
    )


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
    return await _video_generation_routes.retry_video_generation(
        generation_id=generation_id,
        request=request,
        user=user,
        db=db,
        deps=_generation_route_dependencies(),
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
    return await _video_generation_routes.delete_video(
        video_id=video_id,
        user=user,
        db=db,
        deps=_generation_route_dependencies(),
    )


@router.get("/reference/{video_id}/binary")
async def reference_video_binary(
    video_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=80),
) -> Response:
    return await _video_media_routes.reference_video_binary(
        video_id,
        request,
        db,
        token=token,
        variant=variant,
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
    return await _video_media_routes.reference_video_binary_named(
        video_id,
        filename,
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
    return await _video_media_routes.video_binary(
        video_id,
        request,
        user.id,
        db,
        download=download,
    )


@router.get("/{video_id}/poster")
async def video_poster(
    video_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await _video_media_routes.video_poster(
        video_id,
        request,
        user.id,
        db,
    )


video_out = _video_out
create_video_generation_record = _create_video_generation_record
video_provider_state = _video_provider_state
