"""Images 路由（DESIGN §5.6 简化版：仅上传、查看、反代、软删）。

V1 不实现：variations、share、shares/*（V1.1+）。

本地文件系统存储：`settings.storage_root + /u/{uid}/uploads/{image_id}.{ext}`。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image as PILImage, UnidentifiedImageError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.image_signing import (
    ALLOWED_VARIANTS as SIGNED_ALLOWED_VARIANTS,
    verify_image_sig,
)
from lumen_core.models import Image, ImageVariant, Share
from lumen_core.schemas import ImageOut

from ..audit import hash_email, request_ip_hash, write_audit
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..ratelimit import PUBLIC_IMAGE_LIMITER, UPLOADS_LIMITER, client_ip
from ..redis_client import get_redis


router = APIRouter()

_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}


MAX_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_LONG_SIDE = 4096
MAX_IMAGE_PIXELS = MAX_LONG_SIDE * MAX_LONG_SIDE * 4
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

DISPLAY_VARIANT = "display2048"
PREVIEW_VARIANT = "preview1024"
THUMB_VARIANT = "thumb256"
ALLOWED_VARIANTS = {DISPLAY_VARIANT, PREVIEW_VARIANT, THUMB_VARIANT}
VARIANT_MEDIA_TYPE = {
    DISPLAY_VARIANT: "image/webp",
    PREVIEW_VARIANT: "image/webp",
    THUMB_VARIANT: "image/jpeg",
}


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _too_many_pixels() -> HTTPException:
    return _http(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
        413,
    )


def _enforce_pixel_limit(size: tuple[int, int]) -> None:
    width, height = size
    if width * height > MAX_IMAGE_PIXELS:
        raise _too_many_pixels()


def _open_image_bytes(data: bytes, *, verify: bool = False) -> PILImage.Image:
    try:
        im = PILImage.open(io.BytesIO(data))
        if verify:
            im.verify()
        return im
    except PILImage.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except UnidentifiedImageError as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc
    except Exception as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc


def _key_for_upload(user_id: str, image_id: str, ext: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.{ext}"


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
    except ValueError:
        raise _http("invalid_path", "storage path escapes root", 400)
    return p


def _image_url(image_id: str) -> str:
    # 相对路径：前端同源请求，反代 /api/* → 后端 /*。
    # 不拼 public_base_url：避免把 dev/prod host 焊进 API 响应，导致
    # HTTPS 前端拿到 http://IP:8000/... 触发 Mixed Content。
    return f"/api/images/{image_id}/binary"


def _variant_url(image_id: str, kind: str) -> str:
    return f"/api/images/{image_id}/variants/{kind}"


def _variant_key_for_image(img: Image, kind: str) -> str:
    src = Path(img.storage_key)
    return str(src.with_name(f"{src.stem}.{kind}.webp"))


def _make_display_variant(
    path: Path, max_side: int = 2048
) -> tuple[bytes, tuple[int, int]]:
    try:
        im_ctx = PILImage.open(path)
    except PILImage.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except UnidentifiedImageError as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc

    with im_ctx as im:
        _enforce_pixel_limit(im.size)
        im.load()
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with im.convert("RGB") as rgb:
            rgb.save(buf, format="WEBP", quality=86, method=4)
        return buf.getvalue(), im.size


async def _ensure_display_variant(
    db: AsyncSession,
    img: Image,
) -> ImageVariant:
    locked_img = (
        await db.execute(
            select(Image).where(
                Image.id == img.id,
                Image.user_id == img.user_id,
                Image.deleted_at.is_(None),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if locked_img is None:
        raise _http("not_found", "image not found", 404)
    img = locked_img

    existing = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == DISPLAY_VARIANT,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    src_path = _fs_path(img.storage_key)
    if not src_path.is_file():
        raise _http("not_found", "binary missing", 404)

    data, size = await asyncio.to_thread(_make_display_variant, src_path)
    key = _variant_key_for_image(img, DISPLAY_VARIANT)

    variant = ImageVariant(
        image_id=img.id,
        kind=DISPLAY_VARIANT,
        storage_key=key,
        width=size[0],
        height=size[1],
    )
    db.add(variant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == img.id,
                    ImageVariant.kind == DISPLAY_VARIANT,
                )
            )
        ).scalar_one_or_none()
        if winner:
            return winner
        raise

    # DB record committed, now write file.
    # If file write fails, delete the DB record to avoid orphan rows.
    dst_path = _fs_path(key)
    try:
        await asyncio.to_thread(_write_new_file_atomic, dst_path, data)
    except FileExistsError:
        pass  # concurrent writer already created the file
    except Exception:
        await db.delete(variant)
        await db.commit()
        raise

    await db.refresh(variant)
    return variant


async def _image_out(db: AsyncSession, img: Image) -> ImageOut:
    variants = (
        await db.execute(
            select(ImageVariant).where(ImageVariant.image_id == img.id)
        )
    ).scalars().all()
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
        thumb_url=_variant_url(img.id, THUMB_VARIANT) if THUMB_VARIANT in kinds else None,
        metadata_jsonb=img.metadata_jsonb or {},
    )


async def _check_upload_rate_limit(user_id: str) -> None:
    redis = get_redis()
    await UPLOADS_LIMITER.check(redis, f"rl:upload:{user_id}")


async def _check_public_image_lookup_rate_limit(request: Request) -> None:
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(
        redis, f"rl:image-key:{client_ip(request)}"
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


def _open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise _http("not_found", "binary missing", 404) from exc
        if exc.errno == errno.ELOOP:
            raise _http("invalid_path", "symlink storage paths are not allowed", 400) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(st.st_size)
    except Exception:
        os.close(fd)
        raise


def _iter_open_file_and_close(f: BinaryIO) -> Iterator[bytes]:
    try:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        f.close()


def _storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
) -> StreamingResponse:
    f, size = _open_regular_file_no_symlink(path)
    headers = {
        "Cache-Control": cache_control,
        "Content-Length": str(size),
        "ETag": etag,
    }
    return StreamingResponse(
        _iter_open_file_and_close(f),
        media_type=media_type,
        headers=headers,
    )


@router.post("/upload", response_model=ImageOut, dependencies=[Depends(verify_csrf)])
async def upload_image(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
) -> ImageOut:
    await _check_upload_rate_limit(user.id)

    # Read all bytes with size cap.
    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_BYTES:
            raise _http("too_large", f"file exceeds {MAX_BYTES // (1024*1024)}MB", 413)

    if not buf:
        raise _http("empty_file", "empty file", 400)

    data = bytes(buf)
    _open_image_bytes(data, verify=True)

    # Re-open after verify() (PIL requires this).
    im = _open_image_bytes(data)
    mime = (im.get_format_mimetype() or "").lower()
    if mime not in ALLOWED_MIME:
        raise _http("unsupported_mime", f"mime not allowed: {mime}", 400)

    width, height = im.size
    _enforce_pixel_limit((width, height))
    if max(width, height) > MAX_LONG_SIDE:
        ratio = MAX_LONG_SIDE / max(width, height)
        new_w, new_h = int(width * ratio), int(height * ratio)
        im = im.resize((new_w, new_h), PILImage.LANCZOS)
        width, height = new_w, new_h
        out = io.BytesIO()
        fmt = "WEBP" if mime == "image/webp" else ("PNG" if mime == "image/png" else "JPEG")
        save_kw: dict[str, Any] = {}
        if fmt == "JPEG":
            save_kw["quality"] = 92
        elif fmt == "WEBP":
            save_kw["quality"] = 90
        im.save(out, format=fmt, **save_kw)
        buf = bytearray(out.getvalue())

    # hash
    sha = hashlib.sha256(bytes(buf)).hexdigest()

    # blurhash (best effort; lib is in worker only — we skip here and leave None)
    blurhash: str | None = None

    # Build image row.
    img = Image(
        user_id=user.id,
        source=ImageSource.UPLOADED.value,
        storage_key="",  # filled after we know image_id
        mime=mime,
        width=width,
        height=height,
        size_bytes=len(buf),
        sha256=sha,
        blurhash=blurhash,
        visibility=ImageVisibility.PRIVATE.value,
    )
    db.add(img)
    await db.flush()

    ext = EXT_BY_MIME[mime]
    key = _key_for_upload(user.id, img.id, ext)
    img.storage_key = key

    # Write to disk.
    path = _fs_path(key)
    try:
        _write_new_file_atomic(path, bytes(buf))
    except FileExistsError as exc:
        raise _http("storage_conflict", "image storage key already exists", 409) from exc

    await db.commit()
    await db.refresh(img)

    return await _image_out(db, img)


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
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    return await _image_out(db, img)


@router.get("/{image_id}/binary")
async def get_image_binary(
    image_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)

    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/{image_id}/variants/{kind}")
async def get_image_variant(
    image_id: str,
    kind: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    if kind not in ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    variant = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if not variant:
        if kind != DISPLAY_VARIANT:
            raise _http("not_found", "variant not found", 404)
        try:
            variant = await _ensure_display_variant(db, img)
        except PILImage.DecompressionBombError as exc:
            raise _too_many_pixels() from exc
    path = _fs_path(variant.storage_key)

    media_type = VARIANT_MEDIA_TYPE[kind]
    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/_/sig/{image_id}/{variant}")
async def get_image_signed(
    image_id: str,
    variant: str,
    exp: int,
    sig: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
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
    await _check_public_image_lookup_rate_limit(request)
    if not verify_image_sig(
        image_id, variant, exp, sig, secret_str.encode("utf-8")
    ):
        # 不区分"签名错"和"过期"——攻击者无需区分，错误码统一收敛
        raise _http("forbidden", "invalid or expired image signature", 403)

    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)

    # Why: defense-in-depth. The HMAC signature is the primary authorization,
    # but if the signing secret leaks (e.g. compromised worker) an attacker
    # could forge sigs for arbitrary image_ids. Require the image to be
    # exposed via at least one non-revoked, non-expired Share so that
    # private images that were never publicly shared cannot be served by
    # this endpoint even with a valid signature.
    now = datetime.now(timezone.utc)
    share_hit = (
        await db.execute(
            select(Share.id).where(
                or_(
                    Share.image_id == img.id,
                    Share.image_ids.contains([img.id]),
                ),
                Share.revoked_at.is_(None),
                or_(Share.expires_at.is_(None), Share.expires_at > now),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not share_hit:
        raise _http("not_found", "image not found", 404)

    if variant == "orig":
        path = _fs_path(img.storage_key)
        media_type = img.mime
        etag = f'"{img.sha256}"'
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

    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=etag,
        # 客户端 / 反代缓存 1h，远小于 sig 默认 TTL；既不浪费带宽也避免长缓存暴露失效签名。
        cache_control="public, max-age=3600",
    )


@router.get("/_/by-key/{key:path}")
async def get_image_by_key(
    key: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Proxy lookup by `storage_key`. Used when Worker writes a `public_url` that
    references our key space. Owner check is enforced."""
    await _check_public_image_lookup_rate_limit(request)
    img = (
        await db.execute(
            select(Image).where(
                Image.storage_key == key,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.delete("/{image_id}", dependencies=[Depends(verify_csrf)])
async def delete_image(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    img.deleted_at = datetime.now(timezone.utc)
    await write_audit(
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
