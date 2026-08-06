"""Images 路由（DESIGN §5.6 简化版：仅上传、查看、反代、软删）。

V1 不实现：variations、share、shares/*（V1.1+）。

本地文件系统存储：`settings.storage_root + /u/{uid}/uploads/{image_id}.{ext}`。
"""

from __future__ import annotations

import logging
import os  # noqa: F401
import shutil  # noqa: F401
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

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
    Image,
    ImageVariant,
    Share,
    User,
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
from ...deps import CurrentUser, durable_session_id, verify_csrf
from ...ratelimit import (
    PUBLIC_IMAGE_LIMITER,
    UPLOADS_LIMITER,
    client_ip,
    require_client_ip,
)
from ...redis_client import get_redis
from ...services.active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    ensure_active_user,
)
from ..composition import (
    create_image_route_lifespan,
    get_upload_command_service,
    get_variant_service,
)
from ._file_delivery import OWNER_PROTECTED_MEDIA_CACHE_CONTROL
from ..domain.artifact import ArtifactStatus
from ..domain.variants import (
    ALLOWED_VARIANTS,
    DISPLAY_VARIANT,
    VARIANT_MEDIA_TYPE,
    VIDEO_REFERENCE_VARIANT,
)
from .create_variant import (
    CreateVariantService,
    VariantError,
)
from .http_route_parts import (
    presentation as route_presentation,
    reference_access,
    storage as route_storage,
    upload as route_upload,
)
from .upload import UploadCommandService
from .visibility_batch import ImageVisibilityCandidate, visible_image_ids


router = APIRouter(lifespan=create_image_route_lifespan())
logger = logging.getLogger(__name__)

MAX_BYTES = route_upload.MAX_BYTES
MAX_LONG_SIDE = route_upload.MAX_LONG_SIDE
VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE = route_upload.VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
MAX_IMAGE_PIXELS = route_upload.MAX_IMAGE_PIXELS
ALLOWED_MIME = route_upload.ALLOWED_MIME
EXT_BY_MIME = route_upload.EXT_BY_MIME
NORMALIZABLE_UPLOAD_MIME = route_upload.NORMALIZABLE_UPLOAD_MIME
MIN_STORAGE_FREE_BYTES = route_storage.MIN_STORAGE_FREE_BYTES
PILImage = route_upload.PILImage
_etag_matches_if_none_match = route_storage.etag_matches_if_none_match
_open_regular_file_no_symlink = route_storage.open_regular_file_no_symlink

VIDEO_REFERENCE_IMAGE_KIND = VIDEO_REFERENCE_VARIANT
VIDEO_REFERENCE_IMAGE_MIME = "image/jpeg"


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


async def _image_referenced_by_visible_user_history(
    db: AsyncSession,
    img: Image,
    user: Any,
    policy: Any,
) -> bool:
    visible_after = byok_retention_cutoffs(policy=policy).visible_after
    candidate = ImageVisibilityCandidate(
        image_id=img.id,
        owner_generation_id=getattr(img, "owner_generation_id", None),
        created_at=img.created_at,
    )
    return img.id in await visible_image_ids(
        db,
        [candidate],
        user_id=user.id,
        visible_after=visible_after,
    )


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


_too_many_pixels = route_upload.too_many_pixels
_enforce_pixel_limit = route_upload.enforce_pixel_limit
_upload_requests_mask_preflight = route_upload.upload_requests_mask_preflight
_upload_allows_large_dimensions = route_upload.upload_allows_large_dimensions
_key_for_upload = route_upload.key_for_upload
_key_for_normalized_ref = route_upload.key_for_normalized_ref


def _fs_path(storage_key: str) -> Path:
    return route_storage.storage_path(storage_key, error_factory=_http)


_storage_usage_path = route_storage.storage_usage_path


def _minimum_storage_free_bytes() -> int:
    return route_storage.minimum_storage_free_bytes(logger)


def _ensure_storage_free_space(incoming_bytes: int) -> None:
    route_storage.ensure_storage_free_space(
        incoming_bytes,
        error_factory=_http,
        logger=logger,
    )


_image_url = route_presentation.image_url
_variant_url = route_presentation.variant_url
_variant_key_for_image = route_presentation.variant_key_for_image
_make_display_variant = route_upload.make_route_display_variant
_image_out = route_presentation.image_out


async def _check_upload_rate_limit(user_id: str) -> None:
    redis = get_redis()
    await UPLOADS_LIMITER.check(redis, f"rl:upload:{user_id}")


_upload_metadata_finalizer = route_upload.upload_metadata_finalizer


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


async def _check_reference_image_rate_limit(request: Request) -> None:
    """Public unauthenticated video-reference image route: reject when client
    IP is unknown so anonymous clients can't share one bucket and DoS the
    rest. 每请求消耗一个令牌，同时约束按需变体渲染频率与二进制流输出。"""
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(
        redis, f"rl:image-ref:{require_client_ip(request)}"
    )


def _unlink_file_if_exists(path: Path) -> None:
    route_storage.unlink_file_if_exists(path, logger=logger)


_fsync_directory = route_storage.fsync_directory
_write_new_file_atomic = route_storage.write_new_file_atomic
_write_new_file_exclusive = route_storage.write_new_file_exclusive


def _iter_open_file_and_close(f: Any):
    yield from route_storage.iter_open_file_and_close(f)


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
    return route_storage.storage_streaming_response(
        path,
        media_type=media_type,
        etag=etag,
        cache_control=cache_control,
        validate_storage_key=_fs_path,
        storage_key=storage_key,
        request=request,
        inline_filename=inline_filename,
    )


upload_image_impl = route_upload.upload_image_impl


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
    request: Request = None,
) -> ImageOut:
    session_id = durable_session_id(request)
    try:
        if session_id:
            await ensure_active_user(db, user.id, session_id=session_id)
        else:
            await ensure_active_user(db, user.id)
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
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
        session_id=session_id,
    )


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
        cache_control=OWNER_PROTECTED_MEDIA_CACHE_CONTROL,
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
        expected_user_id = user.id
        await db.rollback()
        try:
            variant = await variant_service.ensure_display_variant(
                image_id,
                expected_user_id=expected_user_id,
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
        cache_control=OWNER_PROTECTED_MEDIA_CACHE_CONTROL,
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
    #
    # The join to the share's primary image only enforces ownership. It must
    # NOT gate on the primary's deleted_at: in a multi-image share the primary
    # can be soft-deleted while other members stay alive, and the requested
    # image's own liveness was already checked by the lookup above — a dead
    # primary must not block the remaining share members.
    now = datetime.now(timezone.utc)
    share_primary = aliased(Image)
    share_hit = (
        await db.execute(
            select(Share.id)
            .join(share_primary, share_primary.id == Share.image_id)
            .where(
                share_primary.user_id == img.user_id,
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


_parse_video_reference_token_expiry = (
    reference_access.parse_video_reference_token_expiry
)
_video_reference_token_is_valid = reference_access.video_reference_token_is_valid


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
        check_reference_image_rate_limit=_check_reference_image_rate_limit,
    )


async def reference_image_binary_impl(
    image_id: str,
    request: Request,
    db: AsyncSession,
    *,
    token: str,
    variant: str | None,
    variant_service: CreateVariantService,
    check_reference_image_rate_limit: Any,
) -> Response:
    # 未认证端点：先消耗 per-IP 限流令牌，约束按需变体渲染频率与二进制流
    # 输出，再触达 DB / 渲染管线。
    await check_reference_image_rate_limit(request)
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
        check_reference_image_rate_limit=_check_reference_image_rate_limit,
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
        cache_control=OWNER_PROTECTED_MEDIA_CACHE_CONTROL,
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
    """软删图片。

    分享语义(刻意行为,勿改):删除分享中的图片**不会**撤销分享 row,也不会
    收回整组分享——该图片自身经签名/分享通道立即 404,分享集合内其余存活
    成员仍可访问(见 get_image_signed_impl 的 share 校验注释);需要整体收回
    分享由 shares 路由的 revoke 完成。
    """
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
        autocommit=False,
    )
    await db.commit()
    return {"ok": True}
