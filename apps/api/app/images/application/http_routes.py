"""Images 路由（DESIGN §5.6 简化版：仅上传、查看、反代、软删）。

V1 不实现：variations、share、shares/*（V1.1+）。

本地文件系统存储：`settings.storage_root + /u/{uid}/uploads/{image_id}.{ext}`。
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, BinaryIO, Iterator

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from PIL import Image as PILImage
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    cutoffs as byok_retention_cutoffs,
    is_user_visible as byok_retention_is_user_visible,
)
from lumen_core.image_signing import (
    ALLOWED_VARIANTS as SIGNED_ALLOWED_VARIANTS,
    verify_image_sig,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    Share,
    User,
)
from lumen_core.model_image_metadata import (
    model_image_filename,
)
from lumen_core.schemas import ImageOut
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_IMAGE_MIME,
)
from lumen_core.volcano_assets import volcano_asset_safe_filename

from ...audit import hash_email, request_ip_hash, write_audit
from ...byok_service import read_byok_settings_cached, retention_policy_from_settings
from ...canvas_services import asset_ref_service
from ...config import settings
from ...db import get_db
from ...deps import CurrentUser, verify_csrf
from ...ratelimit import (
    PUBLIC_IMAGE_LIMITER,
    UPLOADS_LIMITER,
    client_ip,
    require_client_ip,
)
from ...redis_client import get_redis
from ._file_delivery import (
    etag_matches_if_none_match as _etag_matches_if_none_match,
    internal_redirect_enabled as _internal_redirect_enabled,
    iter_open_file_and_close as _iter_delivery_file_and_close,
    open_regular_file_no_symlink as _open_regular_file_no_symlink,
    storage_streaming_response as _build_storage_streaming_response,
)
from ...services import storage_files
from ..composition import (
    create_image_route_lifespan,
    get_upload_command_service,
    get_variant_service,
)
from ..domain.artifact import ArtifactStatus
from ..domain.variants import (
    ALLOWED_VARIANTS,
    DISPLAY_VARIANT,
    PREVIEW_VARIANT,
    THUMB_VARIANT,
    VARIANT_MEDIA_TYPE,
    VIDEO_REFERENCE_VARIANT,
    deterministic_variant_key,
)
from ..processing.model_metadata import MODEL_LIBRARY_METADATA_PROFILE
from .create_variant import (
    CreateVariantService,
    VariantError,
    make_display_variant,
)
from .deliver import DeliverySpec, deliver_artifact
from .upload import (
    UploadCommandError,
    UploadCommandService,
    UploadPolicy,
)


router = APIRouter(lifespan=create_image_route_lifespan())
logger = logging.getLogger(__name__)

_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)

_LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.EACCES,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)


MAX_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_LONG_SIDE = 4096
VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE = 8192
MAX_IMAGE_PIXELS = 64_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/webp"})
EXT_BY_MIME = MappingProxyType(
    {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
)
NORMALIZABLE_UPLOAD_MIME = frozenset({"image/mpo", "image/x-mpo"})
MIN_STORAGE_FREE_BYTES = 512 * 1024 * 1024

VIDEO_REFERENCE_IMAGE_KIND = VIDEO_REFERENCE_VARIANT
VIDEO_REFERENCE_IMAGE_MIME = "image/jpeg"


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _content_references_image(value: Any, image_id: str) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key in {"image_id", "source_image_id", "mask_image_id"}
                and item == image_id
            ):
                return True
            if _content_references_image(item, image_id):
                return True
        return False
    if isinstance(value, list):
        return any(_content_references_image(item, image_id) for item in value)
    return False


async def _image_referenced_by_visible_user_history(
    db: AsyncSession,
    img: Image,
    user: Any,
    policy: Any,
) -> bool:
    visible_after = byok_retention_cutoffs(policy=policy).visible_after
    gen_row = (
        await db.execute(
            select(Generation.id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user.id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                Message.deleted_at.is_(None),
                Message.created_at >= visible_after,
                or_(
                    Generation.id == getattr(img, "owner_generation_id", None),
                    Generation.primary_input_image_id == img.id,
                    Generation.mask_image_id == img.id,
                ),
            )
            .limit(1)
        )
    ).first()
    if gen_row is not None:
        return True

    gen_inputs = (
        (
            await db.execute(
                select(Generation.input_image_ids)
                .join(Message, Message.id == Generation.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Generation.user_id == user.id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                    Message.deleted_at.is_(None),
                    Message.created_at >= visible_after,
                )
            )
        )
        .scalars()
        .all()
    )
    if any(img.id in (input_ids or []) for input_ids in gen_inputs):
        return True

    contents = (
        (
            await db.execute(
                select(Message.content)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                    Message.deleted_at.is_(None),
                    Message.created_at >= visible_after,
                )
            )
        )
        .scalars()
        .all()
    )
    return any(_content_references_image(content, img.id) for content in contents)


async def _ensure_image_visible_to_user(
    db: AsyncSession,
    img: Image,
    user: Any,
) -> None:
    if not byok_retention_applies_to_user(user):
        return
    created_at = getattr(img, "created_at", None)
    if created_at is None:
        return
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not byok_retention_is_user_visible(
        account_mode=getattr(user, "account_mode", None),
        created_at=created_at,
        policy=policy,
    ):
        if await _image_referenced_by_visible_user_history(db, img, user, policy):
            return
        raise _http("not_found", "image not found", 404)


async def _ensure_public_image_visible(db: AsyncSession, img: Image) -> None:
    user_id = getattr(img, "user_id", None)
    created_at = getattr(img, "created_at", None)
    if not user_id or created_at is None:
        return
    account_mode = (
        await db.execute(select(User.account_mode).where(User.id == user_id))
    ).scalar_one_or_none()
    if account_mode != "byok":
        return
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not byok_retention_is_user_visible(
        account_mode=account_mode,
        created_at=created_at,
        policy=policy,
    ):
        raise _http("not_found", "image not found", 404)


def _too_many_pixels() -> HTTPException:
    return _http(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
        413,
    )


def _enforce_pixel_limit(
    size: tuple[int, int],
    *,
    max_long_side: int | None = MAX_LONG_SIDE,
) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise _http("invalid_image", "invalid image size", 400)
    if width * height > MAX_IMAGE_PIXELS:
        raise _too_many_pixels()
    if max_long_side is not None and max(width, height) > max_long_side:
        raise _http(
            "too_large",
            f"image long side exceeds {max_long_side}px",
            413,
        )


def _upload_requests_mask_preflight(purpose: str | None, filename: str | None) -> bool:
    purpose_norm = (purpose or "").strip().lower()
    if purpose_norm in {"mask", "inpaint_mask", "inpaint-mask"}:
        return True
    name = Path(filename or "").name.lower()
    stem = Path(name).stem
    return stem == "mask" or stem.startswith("mask_")


def _upload_allows_large_dimensions(purpose: str | None) -> bool:
    return (purpose or "").strip().lower() == "volcano_asset"


def _key_for_upload(user_id: str, image_id: str, ext: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.{ext}"


def _key_for_normalized_ref(user_id: str, image_id: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.ref.webp"


def _fs_path(storage_key: str) -> Path:
    return storage_files.resolve_storage_path(
        settings.storage_root,
        storage_key,
        error_factory=_http,
    )


def _storage_usage_path(root: Path) -> Path:
    current = root
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _minimum_storage_free_bytes() -> int:
    raw = os.environ.get("LUMEN_MIN_STORAGE_FREE_BYTES", "").strip()
    if not raw:
        return MIN_STORAGE_FREE_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("invalid LUMEN_MIN_STORAGE_FREE_BYTES=%r; using default", raw)
        return MIN_STORAGE_FREE_BYTES


def _ensure_storage_free_space(incoming_bytes: int) -> None:
    root = Path(settings.storage_root).resolve()
    usage = shutil.disk_usage(_storage_usage_path(root))
    required = max(0, incoming_bytes) + _minimum_storage_free_bytes()
    if usage.free < required:
        raise _http(
            "storage_insufficient_space",
            "not enough free storage to accept this upload",
            507,
        )


def _image_url(image_id: str) -> str:
    # 相对路径：前端同源请求，反代 /api/* → 后端 /*。
    # 不拼 public_base_url：避免把 dev/prod host 焊进 API 响应，导致
    # HTTPS 前端拿到 http://IP:8000/... 触发 Mixed Content。
    return f"/api/images/{image_id}/binary"


def _variant_url(image_id: str, kind: str) -> str:
    return f"/api/images/{image_id}/variants/{kind}"


def _variant_key_for_image(img: Image, kind: str) -> str:
    return deterministic_variant_key(
        image_id=img.id,
        source_key=img.storage_key,
        kind=kind,
    )


def _make_display_variant(
    path: Path, max_side: int = 2048
) -> tuple[bytes, tuple[int, int]]:
    try:
        return make_display_variant(
            path,
            max_pixels=MAX_IMAGE_PIXELS,
            max_side=max_side,
        )
    except VariantError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc


async def _image_out(db: AsyncSession, img: Image) -> ImageOut:
    variants = (
        (await db.execute(select(ImageVariant).where(ImageVariant.image_id == img.id)))
        .scalars()
        .all()
    )
    kinds = {v.kind for v in variants}
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=_image_url(img.id),
        display_url=_variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            _variant_url(img.id, PREVIEW_VARIANT) if PREVIEW_VARIANT in kinds else None
        ),
        thumb_url=_variant_url(img.id, THUMB_VARIANT)
        if THUMB_VARIANT in kinds
        else None,
        metadata_jsonb=img.metadata_jsonb or {},
    )


async def _check_upload_rate_limit(user_id: str) -> None:
    redis = get_redis()
    await UPLOADS_LIMITER.check(redis, f"rl:upload:{user_id}")


def _upload_metadata_finalizer(
    image_id: str,
    extension: str,
    metadata: dict[str, Any],
) -> None:
    model_payload = metadata.get("model_library")
    if not isinstance(model_payload, dict):
        return
    metadata["suggested_filename"] = model_image_filename(
        image_id=image_id,
        ext=extension,
        age_segment=model_payload.get("age_segment"),
        gender=model_payload.get("gender"),
        appearance_direction=model_payload.get("appearance_direction"),
        style_tags=model_payload.get("style_tags") or [],
    )


async def _check_public_image_lookup_rate_limit(request: Request) -> None:
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(redis, f"rl:image-key:{client_ip(request)}")


async def _check_signed_image_rate_limit(request: Request) -> None:
    """Public unauthenticated signed-image route: reject when client IP is
    unknown so anonymous clients can't share one bucket and DoS the rest."""
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(
        redis, f"rl:image-sig:{require_client_ip(request)}"
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            _write_new_file_exclusive(path, data)
    finally:
        tmp.unlink(missing_ok=True)


def _write_new_file_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _unlink_file_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError:
        logger.warning(
            "failed to remove orphan upload file path=%s", path, exc_info=True
        )


def _iter_open_file_and_close(f: BinaryIO) -> Iterator[bytes]:
    yield from _iter_delivery_file_and_close(f)


def _storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
    storage_key: str | None = None,
    request: Request | None = None,
    inline_filename: str | None = None,
) -> Response:
    return deliver_artifact(
        DeliverySpec(
            path=path,
            storage_key=storage_key,
            media_type=media_type,
            etag=etag,
            cache_control=cache_control,
            inline_filename=inline_filename,
        ),
        request=request,
        response_builder=lambda delivery_path, **kwargs: (
            _build_storage_streaming_response(
                delivery_path,
                **kwargs,
                etag_matches=_etag_matches_if_none_match,
                validate_storage_key=_fs_path,
                open_file=_open_regular_file_no_symlink,
                iter_file=_iter_open_file_and_close,
                redirect_enabled=_internal_redirect_enabled,
            )
        ),
    )


@router.post("/upload", response_model=ImageOut, dependencies=[Depends(verify_csrf)])
async def upload_image(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    upload_command_service: Annotated[
        UploadCommandService,
        Depends(get_upload_command_service),
    ],
    file: UploadFile = File(...),
    purpose: str | None = Form(default=None),
    reference_width: int | None = Form(default=None),
    reference_height: int | None = Form(default=None),
) -> ImageOut:
    return await upload_image_impl(
        user,
        db,
        file=file,
        purpose=purpose,
        reference_width=reference_width,
        reference_height=reference_height,
        check_upload_rate_limit=_check_upload_rate_limit,
        ensure_storage_free_space=_ensure_storage_free_space,
        upload_command_service=upload_command_service,
        image_out=_image_out,
    )


async def upload_image_impl(
    user: Any,
    db: AsyncSession,
    *,
    file: UploadFile,
    purpose: str | None,
    reference_width: int | None,
    reference_height: int | None,
    check_upload_rate_limit: Any,
    ensure_storage_free_space: Any,
    upload_command_service: UploadCommandService,
    image_out: Any,
) -> ImageOut:
    await check_upload_rate_limit(user.id)
    try:
        ensure_storage_free_space(0)
        reference_size = (
            (reference_width, reference_height)
            if isinstance(reference_width, int) and isinstance(reference_height, int)
            else None
        )
        img = await upload_command_service.execute(
            user_id=user.id,
            upload_file=file,
            filename=file.filename,
            policy=UploadPolicy(
                allowed_mime=ALLOWED_MIME,
                normalizable_mime=NORMALIZABLE_UPLOAD_MIME,
                extensions=EXT_BY_MIME,
                max_bytes=MAX_BYTES,
                max_pixels=MAX_IMAGE_PIXELS,
                max_long_side=(
                    VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
                    if _upload_allows_large_dimensions(purpose)
                    else MAX_LONG_SIDE
                ),
                mask_requested=_upload_requests_mask_preflight(
                    purpose,
                    file.filename,
                ),
                reference_size=reference_size,
            ),
            metadata_profile=MODEL_LIBRARY_METADATA_PROFILE,
            metadata_finalizer=_upload_metadata_finalizer,
            storage_guard=ensure_storage_free_space,
        )
    except UploadCommandError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc

    return await image_out(db, img)


@router.get("/{image_id}", response_model=ImageOut)
async def get_image_meta(
    image_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImageOut:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    return await _image_out(db, img)


@router.get("/{image_id}/binary")
async def get_image_binary(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)

    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=img.storage_key,
        request=request,
    )


@router.get("/{image_id}/variants/{kind}")
async def get_image_variant(
    image_id: str,
    kind: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    variant_service: Annotated[CreateVariantService, Depends(get_variant_service)],
) -> Response:
    if kind not in ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    if kind == DISPLAY_VARIANT:
        await db.rollback()
        try:
            variant = await variant_service.ensure_display_variant(
                image_id,
                expected_user_id=user.id,
            )
        except VariantError as exc:
            raise _http(exc.code, exc.message, exc.status_code) from exc
    else:
        variant = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == img.id,
                    ImageVariant.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise _http("not_found", "variant not found", 404)
    path = _fs_path(variant.storage_key)

    media_type = VARIANT_MEDIA_TYPE[kind]
    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=variant.storage_key,
        request=request,
    )


@router.get("/_/sig/{image_id}/{variant}")
async def get_image_signed(
    image_id: str,
    variant: str,
    exp: int,
    sig: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await get_image_signed_impl(
        image_id,
        variant,
        exp,
        sig,
        request,
        db,
        check_signed_image_rate_limit=_check_signed_image_rate_limit,
    )


async def get_image_signed_impl(
    image_id: str,
    variant: str,
    exp: int,
    sig: str,
    request: Request,
    db: AsyncSession,
    *,
    check_signed_image_rate_limit: Any,
) -> Response:
    """无登录、签名授权的图片端点。

    流程：
    1. settings.image_proxy_secret 未配置 → 503（功能未启用）
    2. variant / 签名 / 过期校验失败 → 403
    3. 通过则定位到 Image (variant=orig) 或 ImageVariant
    4. 流式回写 binary，缓存头允许 1h（远低于 sig TTL 默认 24h）

    Owner 检查在这里**不**做——签名本身就是授权凭证。
    """
    secret_str = settings.image_proxy_secret.strip()
    if not secret_str:
        raise _http("signed_proxy_disabled", "image signing not configured", 503)
    if variant not in SIGNED_ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    await check_signed_image_rate_limit(request)
    if not verify_image_sig(image_id, variant, exp, sig, secret_str.encode("utf-8")):
        # 不区分"签名错"和"过期"——攻击者无需区分，错误码统一收敛
        raise _http("forbidden", "invalid or expired image signature", 403)

    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_public_image_visible(db, img)

    # Why: defense-in-depth. The HMAC signature is the primary authorization,
    # but if the signing secret leaks (e.g. compromised worker) an attacker
    # could forge sigs for arbitrary image_ids. Require the image to be
    # exposed via at least one non-revoked, non-expired Share so that
    # private images that were never publicly shared cannot be served by
    # this endpoint even with a valid signature.
    now = datetime.now(timezone.utc)
    share_primary = aliased(Image)
    share_hit = (
        await db.execute(
            select(Share.id)
            .join(share_primary, share_primary.id == Share.image_id)
            .where(
                share_primary.user_id == img.user_id,
                share_primary.deleted_at.is_(None),
                or_(
                    Share.image_id == img.id,
                    Share.image_ids.contains([img.id]),
                ),
                Share.revoked_at.is_(None),
                or_(Share.expires_at.is_(None), Share.expires_at > now),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not share_hit:
        raise _http("not_found", "image not found", 404)

    if variant == "orig":
        path = _fs_path(img.storage_key)
        media_type = img.mime
        etag = f'"{img.sha256}"'
        storage_key = img.storage_key
    else:
        v = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == img.id,
                    ImageVariant.kind == variant,
                )
            )
        ).scalar_one_or_none()
        if not v:
            raise _http("not_found", "variant not found", 404)
        path = _fs_path(v.storage_key)
        media_type = VARIANT_MEDIA_TYPE.get(variant, "application/octet-stream")
        etag = f'"{v.image_id}-{v.kind}"'
        storage_key = v.storage_key

    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=etag,
        cache_control="private, max-age=300",
        storage_key=storage_key,
        request=request,
    )


def _parse_video_reference_token_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _video_reference_token_is_valid(
    metadata: dict[str, Any],
    *,
    token: str,
    updated_at: datetime | None,
) -> bool:
    expected = metadata.get("video_reference_access_token")
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        return False
    expires_at = _parse_video_reference_token_expiry(
        metadata.get("video_reference_access_token_expires_at")
    )
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
    return fallback_updated_at + _VIDEO_REFERENCE_ACCESS_TOKEN_TTL > now


@router.get("/reference/{image_id}/binary")
async def reference_image_binary(
    image_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    variant_service: Annotated[CreateVariantService, Depends(get_variant_service)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=32),
) -> Response:
    return await reference_image_binary_impl(
        image_id,
        request,
        db,
        token=token,
        variant=variant,
        variant_service=variant_service,
    )


async def reference_image_binary_impl(
    image_id: str,
    request: Request,
    db: AsyncSession,
    *,
    token: str,
    variant: str | None,
    variant_service: CreateVariantService,
) -> Response:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if img is None:
        raise _http("not_found", "image not found", 404)
    await _ensure_public_image_visible(db, img)
    metadata = img.metadata_jsonb or {}
    if not _video_reference_token_is_valid(
        metadata,
        token=token,
        updated_at=getattr(img, "updated_at", None),
    ):
        raise _http("not_found", "image not found", 404)
    if variant:
        if variant == VIDEO_REFERENCE_IMAGE_KIND:
            await db.rollback()
            try:
                ref_variant = await variant_service.ensure_video_reference_variant(
                    image_id
                )
            except VariantError as exc:
                raise _http(exc.code, exc.message, exc.status_code) from exc
            return _storage_streaming_response(
                _fs_path(ref_variant.storage_key),
                media_type=VIDEO_REFERENCE_IMAGE_MIME,
                etag=f'"{ref_variant.image_id}-{ref_variant.kind}"',
                cache_control="private, max-age=3600",
                storage_key=ref_variant.storage_key,
                request=request,
            )
        if variant == VOLCANO_ASSET_IMAGE_KIND:
            asset_variant = (
                await db.execute(
                    select(ImageVariant).where(
                        ImageVariant.image_id == img.id,
                        ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                    )
                )
            ).scalar_one_or_none()
            if asset_variant is None:
                raise _http("not_found", "image not found", 404)
            return _storage_streaming_response(
                _fs_path(asset_variant.storage_key),
                media_type=VOLCANO_ASSET_IMAGE_MIME,
                etag=f'"{asset_variant.image_id}-{asset_variant.kind}"',
                cache_control="private, max-age=3600",
                storage_key=asset_variant.storage_key,
                request=request,
                inline_filename=volcano_asset_safe_filename(
                    img.id,
                    asset_type="Image",
                ),
            )
        else:
            raise _http("invalid_variant", "unsupported image reference variant", 400)
    return _storage_streaming_response(
        _fs_path(img.storage_key),
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=3600",
        storage_key=img.storage_key,
        request=request,
    )


@router.get("/reference/{image_id}/binary/{filename}")
async def reference_image_binary_named(
    image_id: str,
    filename: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    variant_service: Annotated[CreateVariantService, Depends(get_variant_service)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=32),
) -> Response:
    expected = volcano_asset_safe_filename(image_id, asset_type="Image")
    if filename != expected or variant != VOLCANO_ASSET_IMAGE_KIND:
        raise _http("not_found", "image not found", 404)
    return await reference_image_binary_impl(
        image_id,
        request,
        db,
        token=token,
        variant=variant,
        variant_service=variant_service,
    )


@router.get("/_/by-key/{key:path}")
async def get_image_by_key(
    key: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await get_image_by_key_impl(
        key,
        request,
        user,
        db,
        check_public_image_lookup_rate_limit=_check_public_image_lookup_rate_limit,
    )


async def get_image_by_key_impl(
    key: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    *,
    check_public_image_lookup_rate_limit: Any,
) -> Response:
    """Proxy lookup by `storage_key`. Used when Worker writes a `public_url` that
    references our key space. Owner check is enforced."""
    await check_public_image_lookup_rate_limit(request)
    img = (
        await db.execute(
            select(Image).where(
                Image.storage_key == key,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=img.storage_key,
        request=request,
    )


@router.delete("/{image_id}", dependencies=[Depends(verify_csrf)])
async def delete_image(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await delete_image_impl(
        image_id,
        request,
        user,
        db,
        write_audit_event=write_audit,
    )


async def delete_image_impl(
    image_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    *,
    write_audit_event: Any,
) -> dict[str, bool]:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Image.artifact_status == ArtifactStatus.READY.value,
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await asset_ref_service.ensure_asset_not_canvas_referenced(db, image_id=img.id)
    img.deleted_at = datetime.now(timezone.utc)
    await write_audit_event(
        db,
        event_type="image.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "image_id": img.id,
            "source": img.source,
            "owner_generation_id": img.owner_generation_id,
        },
    )
    await db.commit()
    return {"ok": True}
