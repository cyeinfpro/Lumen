"""User-owned system prompt library."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Conversation, SystemPrompt
from lumen_core.schemas import (
    SystemPromptCreateIn,
    SystemPromptListOut,
    SystemPromptOut,
    SystemPromptPatchIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import CurrentUser, verify_csrf


router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])


_UNIQUE_NAME_CONSTRAINT = "uq_system_prompts_user_name"


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http,
        detail={"error": {"code": code, "message": msg}},
    )


def _classify_integrity(exc: IntegrityError) -> HTTPException:
    text = f"{exc.orig!r}" if exc.orig is not None else str(exc)
    if _UNIQUE_NAME_CONSTRAINT in text:
        return _http("duplicate_name", "system prompt name already exists", 409)
    return _http("integrity_error", "could not persist system prompt", 409)


def _to_out(prompt: SystemPrompt, default_id: str | None) -> SystemPromptOut:
    out = SystemPromptOut.model_validate(prompt)
    out.is_default = prompt.id == default_id
    return out


async def _get_owned_prompt(
    db: AsyncSession, *, user_id: str, prompt_id: str
) -> SystemPrompt:
    prompt = (
        await db.execute(
            select(SystemPrompt).where(
                SystemPrompt.id == prompt_id,
                SystemPrompt.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if prompt is None:
        raise _http("not_found", "system prompt not found", 404)
    return prompt


@router.get("", response_model=SystemPromptListOut)
async def list_system_prompts(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemPromptListOut:
    rows = (
        await db.execute(
            select(SystemPrompt)
            .where(SystemPrompt.user_id == user.id)
            .order_by(desc(SystemPrompt.updated_at), desc(SystemPrompt.id))
        )
    ).scalars().all()
    return SystemPromptListOut(
        items=[_to_out(p, user.default_system_prompt_id) for p in rows],
        default_id=user.default_system_prompt_id,
    )


@router.post("", response_model=SystemPromptOut, dependencies=[Depends(verify_csrf)])
async def create_system_prompt(
    body: SystemPromptCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemPromptOut:
    prompt = SystemPrompt(
        user_id=user.id,
        name=body.name.strip(),
        content=body.content,
    )
    db.add(prompt)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _classify_integrity(exc) from exc
    if body.make_default:
        user.default_system_prompt_id = prompt.id
    await write_audit(
        db,
        event_type="system_prompt.create",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={"prompt_id": prompt.id, "name": prompt.name},
    )
    await db.commit()
    await db.refresh(prompt)
    return _to_out(prompt, user.default_system_prompt_id)


@router.patch("/{prompt_id}", response_model=SystemPromptOut, dependencies=[Depends(verify_csrf)])
async def patch_system_prompt(
    prompt_id: str,
    body: SystemPromptPatchIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemPromptOut:
    prompt = await _get_owned_prompt(db, user_id=user.id, prompt_id=prompt_id)
    if body.name is not None:
        prompt.name = body.name.strip()
    if body.content is not None:
        prompt.content = body.content
    if body.make_default is True:
        user.default_system_prompt_id = prompt.id
    elif body.make_default is False and user.default_system_prompt_id == prompt.id:
        user.default_system_prompt_id = None
    try:
        await write_audit(
            db,
            event_type="system_prompt.update",
            user_id=user.id,
            actor_email_hash=hash_email(user.email),
            actor_ip_hash=request_ip_hash(request),
            details={"prompt_id": prompt.id, "name": prompt.name},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _classify_integrity(exc) from exc
    await db.refresh(prompt)
    return _to_out(prompt, user.default_system_prompt_id)


@router.delete("/{prompt_id}", status_code=204, dependencies=[Depends(verify_csrf)])
async def delete_system_prompt(
    prompt_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    prompt = await _get_owned_prompt(db, user_id=user.id, prompt_id=prompt_id)
    if user.default_system_prompt_id == prompt.id:
        user.default_system_prompt_id = None
    await db.execute(
        update(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.default_system_prompt_id == prompt.id,
        )
        .values(default_system_prompt_id=None)
    )
    await write_audit(
        db,
        event_type="system_prompt.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={"prompt_id": prompt.id, "name": prompt.name},
    )
    await db.delete(prompt)
    await db.commit()


@router.post("/{prompt_id}/default", response_model=SystemPromptOut, dependencies=[Depends(verify_csrf)])
async def set_default_system_prompt(
    prompt_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemPromptOut:
    prompt = await _get_owned_prompt(db, user_id=user.id, prompt_id=prompt_id)
    user.default_system_prompt_id = prompt.id
    prompt.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(prompt)
    return _to_out(prompt, user.default_system_prompt_id)
