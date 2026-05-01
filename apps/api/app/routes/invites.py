"""邀请链接路由（V1.0 收尾）。

- /admin/invite_links（admin only + CSRF）：生成 / 列出 / 撤销
- /invite/{token}（公开）：前端注册前预览邀请有效性
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.models import InviteLink, User
from lumen_core.schemas import InviteLinkOut, InviteLinkPublicOut

from ..audit import request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, ensure_utc, verify_csrf_session
from ..public_urls import resolve_public_base_url
from ..ratelimit import PUBLIC_PREVIEW_LIMITER, require_client_ip
from ..redis_client import get_redis


router_authed = APIRouter(prefix="/admin/invite_links", tags=["invites-admin"])
router_public = APIRouter(tags=["invites-public"])

logger = logging.getLogger(__name__)


def _email_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _invite_url(token: str, public_base_url: str) -> str:
    # 邀请链接要发给外部用户复制，必须绝对 URL（web 根）。
    return f"{public_base_url.rstrip('/')}/invite/{token}"


def _to_out(
    inv: InviteLink,
    used_by_email: str | None,
    *,
    public_base_url: str,
    reveal_token: bool = True,
) -> InviteLinkOut:
    token = inv.token if reveal_token else "redacted"
    return InviteLinkOut(
        id=inv.id,
        token=token,
        url=_invite_url(token, public_base_url),
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
        used_at=inv.used_at,
        used_by_email=used_by_email,
        revoked_at=inv.revoked_at,
        created_at=inv.created_at,
    )


# ---------- Admin: create ----------

class _CreateInviteIn(BaseModel):
    email: EmailStr | None = None
    expires_in_days: int = Field(default=7, ge=1, le=365)
    role: Literal["admin", "member"] = "member"


@router_authed.post(
    "",
    response_model=InviteLinkOut,
    status_code=201,
    dependencies=[Depends(verify_csrf_session)],
)
async def create_invite_link(
    body: _CreateInviteIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteLinkOut:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    email_norm = str(body.email).lower().strip() if body.email else None
    inv = InviteLink(
        token=token,
        email=email_norm,
        role=body.role,
        created_by=admin.id,
        expires_at=expires_at,
    )
    db.add(inv)
    await db.flush()
    await write_audit(
        db,
        event_type="invite.create",
        user_id=admin.id,
        actor_email_hash=_email_hash(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "invite_id": inv.id,
            "role": body.role,
            "email_hash": _email_hash(email_norm),
            "expires_at": expires_at.isoformat(),
        },
    )
    await db.commit()
    await db.refresh(inv)
    public_base_url = await resolve_public_base_url(request, db)
    logger.info(
        "invite_created",
        extra={
            "invite_id": inv.id,
            "admin_id": admin.id,
            "role": body.role,
            "email_hash": _email_hash(email_norm),
            "expires_at": expires_at.isoformat(),
        },
    )
    return _to_out(inv, used_by_email=None, public_base_url=public_base_url)


# ---------- Admin: list ----------

@router_authed.get("")
async def list_invite_links(
    _admin: AdminUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    public_base_url = await resolve_public_base_url(request, db)
    UsedBy = aliased(User)
    rows = (
        await db.execute(
            select(InviteLink, UsedBy.email)
            .join(UsedBy, UsedBy.id == InviteLink.used_by, isouter=True)
            .order_by(desc(InviteLink.created_at))
        )
    ).all()
    items = [
        _to_out(
            inv,
            used_by_email=email,
            public_base_url=public_base_url,
            reveal_token=False,
        )
        for inv, email in rows
    ]
    return {"items": items}


# ---------- Admin: revoke ----------

@router_authed.delete(
    "/{invite_id}", status_code=204, dependencies=[Depends(verify_csrf_session)]
)
async def revoke_invite_link(
    invite_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    inv = (
        await db.execute(
            select(InviteLink).where(InviteLink.id == invite_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not inv:
        raise _http("not_found", "invite not found", 404)
    if inv.revoked_at is None:
        inv.revoked_at = datetime.now(timezone.utc)
        await write_audit(
            db,
            event_type="invite.revoke",
            user_id=admin.id,
            actor_email_hash=_email_hash(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={"invite_id": inv.id},
        )
        await db.commit()
        logger.info(
            "invite_revoked",
            extra={"invite_id": inv.id, "admin_id": admin.id},
        )
    return None


# ---------- Public: preview ----------

def _validity(inv: InviteLink, now: datetime) -> tuple[bool, str | None]:
    if inv.revoked_at is not None:
        return False, "revoked"
    if inv.used_at is not None:
        return False, "used"
    if inv.expires_at is not None and ensure_utc(inv.expires_at) <= now:
        return False, "expired"
    return True, None


def _creator_deleted(creator: User | None) -> bool:
    return creator is None or creator.deleted_at is not None


@router_public.get("/invite/{token}", response_model=InviteLinkPublicOut)
async def preview_invite(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteLinkPublicOut:
    await PUBLIC_PREVIEW_LIMITER.check(
        get_redis(), f"rl:invite_preview:{require_client_ip(request)}"
    )
    row = (
        await db.execute(
            select(InviteLink, User)
            .join(User, User.id == InviteLink.created_by, isouter=True)
            .where(InviteLink.token == token)
        )
    ).first()
    inv = row[0] if row is not None else None
    creator = row[1] if row is not None else None
    if inv is None:
        return InviteLinkPublicOut(
            token=token,
            email=None,
            role="member",
            expires_at=None,
            used=False,
            valid=False,
            invalid_reason="not_found",
        )
    now = datetime.now(timezone.utc)
    valid, reason = _validity(inv, now)
    if valid and _creator_deleted(creator):
        valid = False
        reason = "creator_deleted"
    return InviteLinkPublicOut(
        token=inv.token,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
        used=inv.used_at is not None,
        valid=valid,
        invalid_reason=reason,
    )


__all__ = ["router_authed", "router_public"]
