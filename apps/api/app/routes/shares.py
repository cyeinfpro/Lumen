"""Shares 路由（V1.0 收尾）：发布/撤销分享链接 + 公开访问。"""

from __future__ import annotations

import os
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import ImageSource
from lumen_core.models import Generation, Image, ImageVariant, Share
from lumen_core.runtime_settings import get_spec, parse_value
from lumen_core.schemas import PublicShareImageOut, PublicShareOut, ShareOut

from ..audit import hash_email, request_ip_hash, write_audit
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, ensure_utc, verify_csrf
from ..public_urls import resolve_public_base_url
from ..ratelimit import (
    PUBLIC_IMAGE_LIMITER,
    PUBLIC_PREVIEW_LIMITER,
    require_client_ip,
)
from ..redis_client import get_redis
from ..runtime_settings import get_setting


router_authed = APIRouter(tags=["shares"])
router_public = APIRouter(tags=["shares-public"])
SHARE_EXPIRATION_DAYS_KEY = "site.share_expiration_days"
MAX_MULTI_SHARE_IMAGES = 100
DISPLAY_VARIANT = "display2048"
PREVIEW_VARIANT = "preview1024"
THUMB_VARIANT = "thumb256"
ALLOWED_SHARE_VARIANTS = {DISPLAY_VARIANT, PREVIEW_VARIANT, THUMB_VARIANT}
VARIANT_MEDIA_TYPE = {
    DISPLAY_VARIANT: "image/webp",
    PREVIEW_VARIANT: "image/webp",
    THUMB_VARIANT: "image/jpeg",
}


def _http(code: str, msg: str, http: int) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _share_url(token: str, public_base_url: str) -> str:
    # Share URL 是要复制粘贴给外部用户的分享链接，必须绝对 URL（含 host）。
    return f"{public_base_url.rstrip('/')}/share/{token}"


def _share_image_url(token: str) -> str:
    # 图片二进制走 API —— 相对同源路径，由前端 /api 反代到后端 /share/{token}/image。
    return f"/api/share/{token}/image"


def _share_image_item_url(token: str, image_id: str) -> str:
    return f"/api/share/{token}/images/{image_id}"


def _share_image_variant_url(token: str, image_id: str, kind: str) -> str:
    return f"/api/share/{token}/images/{image_id}/variants/{kind}"


def _share_image_ids(s: Share) -> list[str]:
    raw = getattr(s, "image_ids", None)
    seen: set[str] = set()
    ids: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                continue
            clean = item.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            ids.append(clean)
            if len(ids) >= MAX_MULTI_SHARE_IMAGES:
                break
        if ids:
            return ids
    image_id = getattr(s, "image_id", None)
    return [image_id] if isinstance(image_id, str) and image_id else []


def _to_share_out(s: Share, public_base_url: str) -> ShareOut:
    image_ids = _share_image_ids(s)
    return ShareOut(
        id=s.id,
        image_id=s.image_id,
        image_ids=image_ids,
        token=s.token,
        url=_share_url(s.token, public_base_url),
        image_url=_share_image_url(s.token),
        show_prompt=s.show_prompt,
        expires_at=s.expires_at,
        revoked_at=s.revoked_at,
        created_at=s.created_at,
    )


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


def _open_storage_file_safe(storage_key: str) -> tuple[BinaryIO, int]:
    path = _fs_path(storage_key)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.stat()
        fd = os.open(path, flags)
        try:
            after = os.fstat(fd)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or not stat.S_ISREG(after.st_mode)
            ):
                raise _http("invalid_path", "storage path changed while opening", 400)
            return os.fdopen(fd, "rb"), after.st_size
        except Exception:
            os.close(fd)
            raise
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        raise _http("invalid_path", "invalid storage path", 400) from exc


def _storage_key_exists(storage_key: str) -> bool:
    try:
        return _fs_path(storage_key).is_file()
    except HTTPException:
        return False


def _iter_open_file_and_close(f: BinaryIO):
    try:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        f.close()


def _share_image_response(
    opened: BinaryIO,
    size: int,
    *,
    media_type: str,
    etag: str,
) -> StreamingResponse:
    headers = {
        "Cache-Control": "public, max-age=3600",
        "Content-Length": str(size),
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        _iter_open_file_and_close(opened),
        media_type=media_type,
        headers=headers,
    )


def _image_etag(img: Image) -> str:
    sha = getattr(img, "sha256", None)
    return f'"{sha}"' if isinstance(sha, str) and sha else f'"{img.id}-orig"'


async def _check_share_image_rate_limit(request: Request) -> None:
    # Public unauthenticated route: refuse to share a single "unknown" bucket.
    await PUBLIC_IMAGE_LIMITER.check(
        get_redis(), f"rl:share_image:{require_client_ip(request)}"
    )


def _is_share_visible(s: Share, now: datetime) -> bool:
    if s.revoked_at is not None:
        return False
    if s.expires_at is not None and ensure_utc(s.expires_at) <= now:
        return False
    return True


def _visible_share_filters(now: datetime) -> tuple:
    return (
        Share.revoked_at.is_(None),
        or_(Share.expires_at.is_(None), Share.expires_at > now),
        Image.deleted_at.is_(None),
    )


def _select_public_share(token: str, now: datetime):
    return (
        select(Share, Image)
        .join(Image, Image.id == Share.image_id)
        .where(Share.token == token, *_visible_share_filters(now))
    )


async def _default_share_expires_at(
    db: AsyncSession,
    now: datetime,
) -> datetime | None:
    spec = get_spec(SHARE_EXPIRATION_DAYS_KEY)
    if spec is None:
        return None
    raw = await get_setting(db, spec)
    if raw is None:
        return None
    days = parse_value(spec, raw)
    if not isinstance(days, int) or days <= 0:
        return None
    return now + timedelta(days=days)


def _dedupe_image_ids(image_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for image_id in image_ids:
        clean = image_id.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _public_image_out(
    token: str,
    img: Image,
    prompt: str | None = None,
    variant_kinds: set[str] | None = None,
) -> PublicShareImageOut:
    kinds = variant_kinds or set()
    return PublicShareImageOut(
        id=img.id,
        image_url=_share_image_item_url(token, img.id),
        display_url=(
            _share_image_variant_url(token, img.id, DISPLAY_VARIANT)
            if DISPLAY_VARIANT in kinds
            else None
        ),
        preview_url=(
            _share_image_variant_url(token, img.id, PREVIEW_VARIANT)
            if PREVIEW_VARIANT in kinds
            else None
        ),
        thumb_url=(
            _share_image_variant_url(token, img.id, THUMB_VARIANT)
            if THUMB_VARIANT in kinds
            else None
        ),
        width=img.width,
        height=img.height,
        mime=img.mime,
        prompt=prompt,
    )


async def _load_share_images(
    db: AsyncSession,
    share: Share,
    primary_img: Image,
) -> list[Image]:
    image_ids = _share_image_ids(share)
    if not image_ids:
        return [primary_img]
    if len(image_ids) == 1 and image_ids[0] == primary_img.id:
        return [primary_img]

    rows = (
        await db.execute(
            select(Image).where(
                Image.id.in_(image_ids),
                Image.user_id == primary_img.user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    by_id = {img.id: img for img in rows}
    return [by_id[image_id] for image_id in image_ids if image_id in by_id]


async def _variant_kinds_for_images(
    db: AsyncSession,
    images: list[Image],
) -> dict[str, set[str]]:
    image_ids = [img.id for img in images]
    if not image_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ImageVariant.image_id,
                ImageVariant.kind,
                ImageVariant.storage_key,
            ).where(
                ImageVariant.image_id.in_(image_ids),
                ImageVariant.kind.in_(ALLOWED_SHARE_VARIANTS),
            )
        )
    ).all()
    out: dict[str, set[str]] = {}
    for row in rows:
        image_id = row[0]
        kind = row[1]
        storage_key = row[2] if len(row) > 2 else None
        if storage_key and not _storage_key_exists(storage_key):
            continue
        out.setdefault(image_id, set()).add(kind)
    return out


async def _prompt_map_for_images(
    db: AsyncSession,
    share: Share,
    images: list[Image],
) -> dict[str, str]:
    if not share.show_prompt:
        return {}
    generated_ids = [
        img.id
        for img in images
        if img.source == ImageSource.GENERATED.value and img.owner_generation_id
    ]
    if not generated_ids:
        return {}
    if len(generated_ids) == 1:
        img = next(i for i in images if i.id == generated_ids[0])
        gen = (
            await db.execute(
                select(Generation.prompt)
                .join(Image, Image.owner_generation_id == Generation.id)
                .where(
                    Generation.id == img.owner_generation_id,
                    Generation.user_id == img.user_id,
                    Image.id == img.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return {img.id: gen} if gen else {}

    rows = (
        await db.execute(
            select(Image.id, Generation.prompt)
            .join(Generation, Image.owner_generation_id == Generation.id)
            .where(
                Image.id.in_(generated_ids),
                Image.deleted_at.is_(None),
                Generation.user_id == Image.user_id,
            )
        )
    ).all()
    return {image_id: prompt for image_id, prompt in rows if prompt}


# ---------- Authed ----------

class _CreateShareIn(BaseModel):
    show_prompt: bool = False
    expires_at: datetime | None = None


class _CreateMultiShareIn(_CreateShareIn):
    image_ids: list[str] = Field(min_length=1, max_length=MAX_MULTI_SHARE_IMAGES)


@router_authed.post(
    "/images/{image_id}/share",
    response_model=ShareOut,
    status_code=201,
    dependencies=[Depends(verify_csrf)],
)
async def create_share(
    image_id: str,
    body: _CreateShareIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShareOut:
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

    now = datetime.now(timezone.utc)
    expires_at = body.expires_at
    if expires_at is None:
        expires_at = await _default_share_expires_at(db, now)

    token = secrets.token_urlsafe(32)
    share = Share(
        image_id=img.id,
        image_ids=[img.id],
        token=token,
        show_prompt=body.show_prompt,
        expires_at=expires_at,
    )
    db.add(share)
    await db.flush()
    await write_audit(
        db,
        event_type="share.create",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "share_id": share.id,
            "image_id": img.id,
            "image_ids": [img.id],
            "image_count": 1,
            "show_prompt": share.show_prompt,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        },
    )
    await db.commit()
    await db.refresh(share)
    public_base_url = await resolve_public_base_url(request, db)
    return _to_share_out(share, public_base_url)


@router_authed.post(
    "/images/share",
    response_model=ShareOut,
    status_code=201,
    dependencies=[Depends(verify_csrf)],
)
async def create_multi_image_share(
    body: _CreateMultiShareIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShareOut:
    image_ids = _dedupe_image_ids(body.image_ids)
    if not image_ids:
        raise _http("invalid_request", "image_ids is required", 422)
    if len(image_ids) > MAX_MULTI_SHARE_IMAGES:
        raise _http("too_many_images", "too many images in share", 422)

    rows = (
        await db.execute(
            select(Image).where(
                Image.id.in_(image_ids),
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    by_id = {img.id: img for img in rows}
    if len(by_id) != len(image_ids):
        raise _http("not_found", "one or more images not found", 404)

    now = datetime.now(timezone.utc)
    expires_at = body.expires_at
    if expires_at is None:
        expires_at = await _default_share_expires_at(db, now)

    token = secrets.token_urlsafe(32)
    share = Share(
        image_id=image_ids[0],
        image_ids=image_ids,
        token=token,
        show_prompt=body.show_prompt,
        expires_at=expires_at,
    )
    db.add(share)
    await db.flush()
    await write_audit(
        db,
        event_type="share.create",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "share_id": share.id,
            "image_id": share.image_id,
            "image_ids": image_ids,
            "image_count": len(image_ids),
            "show_prompt": share.show_prompt,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        },
    )
    await db.commit()
    await db.refresh(share)
    public_base_url = await resolve_public_base_url(request, db)
    return _to_share_out(share, public_base_url)


@router_authed.delete(
    "/shares/{share_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def revoke_share(
    share_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    row = (
        await db.execute(
            select(Share)
            .join(Image, Image.id == Share.image_id)
            .where(
                Share.id == share_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
            .with_for_update(of=Share)
        )
    ).scalar_one_or_none()
    if not row:
        raise _http("not_found", "share not found", 404)
    share = row
    if share.revoked_at is None:
        share.revoked_at = datetime.now(timezone.utc)
        await write_audit(
            db,
            event_type="share.revoke",
            user_id=user.id,
            actor_email_hash=hash_email(user.email),
            actor_ip_hash=request_ip_hash(request),
            details={
                "share_id": share.id,
                "image_id": share.image_id,
                "image_ids": _share_image_ids(share),
            },
        )
        await db.commit()
    return None


@router_authed.get("/me/shares")
async def list_my_shares(
    user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    public_base_url = await resolve_public_base_url(request, db)
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Share)
            .join(Image, Image.id == Share.image_id)
            .where(
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
                Share.revoked_at.is_(None),
            )
            .order_by(Share.created_at.desc())
        )
    ).scalars().all()
    # Filter out expired in Python (keeps SQL simple across naive/aware tz)
    items = [
        _to_share_out(s, public_base_url) for s in rows if _is_share_visible(s, now)
    ]
    return {"items": items}


# ---------- Public ----------

@router_public.get("/share/{token}", response_model=PublicShareOut)
async def get_public_share(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PublicShareOut:
    await PUBLIC_PREVIEW_LIMITER.check(
        get_redis(), f"rl:share_meta:{require_client_ip(request)}"
    )
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(_select_public_share(token, now))
    ).first()
    if not row:
        raise _http("not_found", "share not found", 404)
    share, img = row

    images = await _load_share_images(db, share, img)
    if not images:
        raise _http("not_found", "share not found", 404)
    variant_kinds = await _variant_kinds_for_images(db, images)
    prompts = await _prompt_map_for_images(db, share, images)
    public_images = [
        _public_image_out(
            share.token,
            image,
            prompts.get(image.id),
            variant_kinds.get(image.id),
        )
        for image in images
    ]
    first = public_images[0]

    return PublicShareOut(
        token=share.token,
        image_url=_share_image_url(share.token),
        images=public_images,
        width=first.width,
        height=first.height,
        mime=first.mime,
        show_prompt=share.show_prompt,
        prompt=first.prompt,
        created_at=share.created_at,
        expires_at=share.expires_at,
    )


@router_public.get("/share/{token}/image")
async def get_public_share_image(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await _check_share_image_rate_limit(request)
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(_select_public_share(token, now))
    ).first()
    if not row:
        raise _http("not_found", "share not found", 404)
    share, img = row
    images = await _load_share_images(db, share, img)
    if not images:
        raise _http("not_found", "share not found", 404)
    img = images[0]

    opened, size = _open_storage_file_safe(img.storage_key)

    return _share_image_response(
        opened,
        size,
        media_type=img.mime,
        etag=_image_etag(img),
    )


@router_public.get("/share/{token}/images/{image_id}/variants/{kind}")
async def get_public_share_image_variant_by_id(
    token: str,
    image_id: str,
    kind: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    if kind not in ALLOWED_SHARE_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    await _check_share_image_rate_limit(request)
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(_select_public_share(token, now))
    ).first()
    if not row:
        raise _http("not_found", "share not found", 404)
    share, primary_img = row
    if image_id not in set(_share_image_ids(share)):
        raise _http("not_found", "image not found", 404)

    variant = (
        await db.execute(
            select(ImageVariant)
            .join(Image, Image.id == ImageVariant.image_id)
            .where(
                ImageVariant.image_id == image_id,
                ImageVariant.kind == kind,
                Image.user_id == primary_img.user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not variant:
        raise _http("not_found", "variant not found", 404)

    opened, size = _open_storage_file_safe(variant.storage_key)

    return _share_image_response(
        opened,
        size,
        media_type=VARIANT_MEDIA_TYPE[kind],
        etag=f'"{variant.image_id}-{variant.kind}"',
    )


@router_public.get("/share/{token}/images/{image_id}")
async def get_public_share_image_by_id(
    token: str,
    image_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await _check_share_image_rate_limit(request)
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(_select_public_share(token, now))
    ).first()
    if not row:
        raise _http("not_found", "share not found", 404)
    share, primary_img = row
    # Why: defense-in-depth — guard against empty / falsy image_id slipping
    # through the membership check via JSONB quirks; also explicitly require
    # the id to appear in the canonical share.image_ids snapshot.
    if not image_id:
        raise _http("not_found", "image not found", 404)
    allowed_ids = set(_share_image_ids(share))
    if image_id not in allowed_ids:
        raise _http("not_found", "image not found", 404)

    if image_id == primary_img.id:
        img = primary_img
    else:
        img = (
            await db.execute(
                select(Image).where(
                    Image.id == image_id,
                    Image.user_id == primary_img.user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    # Redundant tenancy assertion: even though primary_img.user_id was used in
    # the WHERE clause above, re-verify to catch any future refactor that
    # widens the lookup. This is a no-op on the happy path.
    if img.user_id != primary_img.user_id:
        raise _http("not_found", "image not found", 404)

    opened, size = _open_storage_file_safe(img.storage_key)

    return _share_image_response(
        opened,
        size,
        media_type=img.mime,
        etag=_image_etag(img),
    )
