"""Administrator allowed-email routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.model_entities import (
    AllowedEmail,
    User,
)
from lumen_core.schema_models import AllowedEmailOut


class AllowedEmailIn(BaseModel):
    email: EmailStr


@dataclass(frozen=True)
class AllowedEmailDependencies:
    http_error: Callable[..., Exception]
    write_admin_audit: Callable[..., Awaitable[Any]]
    hash_email: Callable[[str | None], str | None]


async def list_allowed_emails(db: AsyncSession) -> dict[str, Any]:
    inviter = aliased(User)
    rows = (
        await db.execute(
            select(AllowedEmail, inviter.email)
            .join(
                inviter,
                and_(
                    inviter.id == AllowedEmail.invited_by,
                    inviter.deleted_at.is_(None),
                ),
                isouter=True,
            )
            .order_by(AllowedEmail.created_at.desc())
        )
    ).all()
    return {
        "items": [
            AllowedEmailOut(
                id=allowed.id,
                email=allowed.email,
                invited_by_email=inviter_email,
                created_at=allowed.created_at,
            )
            for allowed, inviter_email in rows
        ]
    }


async def add_allowed_email(
    *,
    body: AllowedEmailIn,
    request: Request,
    admin: Any,
    db: AsyncSession,
    deps: AllowedEmailDependencies,
) -> AllowedEmailOut:
    email = str(body.email).lower().strip()
    exists = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if exists:
        raise deps.http_error("already_exists", "email already allowed", 409)
    allowed = AllowedEmail(email=email, invited_by=admin.id)
    db.add(allowed)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Race: the pre-check above is not atomic; a concurrent duplicate
        # insert trips the unique constraint. Report the same 409 instead of
        # leaking a 500.
        await db.rollback()
        raise deps.http_error("already_exists", "email already allowed", 409) from exc
    try:
        await deps.write_admin_audit(
            db,
            request,
            admin,
            event_type="admin.allowed_email.add",
            details={"email_hash": deps.hash_email(email), "id": allowed.id},
            autocommit=False,
        )
        await db.commit()
    except IntegrityError:
        # Audit/commit 阶段的完整性失败与重复邮箱无关,不做 409 误分类,
        # 交由全局兜底处理。
        await db.rollback()
        raise
    await db.refresh(allowed)
    return AllowedEmailOut(
        id=allowed.id,
        email=allowed.email,
        invited_by_email=admin.email,
        created_at=allowed.created_at,
    )


async def delete_allowed_email(
    *,
    allowed_email_id: str,
    request: Request,
    admin: Any,
    db: AsyncSession,
    deps: AllowedEmailDependencies,
) -> None:
    allowed = (
        await db.execute(
            select(AllowedEmail).where(AllowedEmail.id == allowed_email_id)
        )
    ).scalar_one_or_none()
    if not allowed:
        raise deps.http_error("not_found", "allowed email not found", 404)
    await deps.write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.allowed_email.delete",
        details={
            "email_hash": deps.hash_email(allowed.email),
            "id": allowed.id,
        },
        autocommit=False,
    )
    await db.delete(allowed)
    await db.commit()
