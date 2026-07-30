"""Admin 路由（V1.0 收尾）：邮箱白名单管理 + 用户列表与聚合统计。

所有端点需要 role=admin（AdminUser 依赖）。写操作使用 verify_csrf。
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime, timedelta, timezone  # noqa: F401
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    AuthSession,
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    User,
)
from lumen_core.schemas import AdminUserOut, AllowedEmailOut
from lumen_core.utils import ensure_utc
from lumen_core.byok_retention import retention_state as byok_retention_state

from ..audit import hash_email
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..db import affected_rows, get_db
from ..deps import AdminUser, verify_csrf
from ..images.application.create_variant import CreateVariantService, VariantError
from ..images.composition import get_variant_service
from ..images.domain.variants import (
    ALLOWED_VARIANTS,
    DISPLAY_VARIANT,
    VARIANT_MEDIA_TYPE,
)
from ..redis_client import get_redis
from ..security import hash_password
from ..services.admin import request_events as _request_events
from ._admin_common import admin_http as _http, write_admin_audit
from .media_delivery import (
    image_storage_path,
    image_storage_streaming_response,
)
from .me import cancel_account_active_tasks, post_commit_account_task_cleanup
from . import admin_allowed_email_routes as _admin_allowed_email_routes
from . import admin_context_routes as _admin_context_routes
from . import admin_dlq_routes as _admin_dlq_routes


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)

_CONTEXT_METRIC_FIELDS = (
    "summary_attempts",
    "summary_successes",
    "summary_failures",
    "manual_compact_calls",
    "cold_start_count",
)
_CONTEXT_CIRCUIT_STATE_KEY = _admin_context_routes.CONTEXT_CIRCUIT_STATE_KEY
_CONTEXT_CIRCUIT_UNTIL_KEY = _admin_context_routes.CONTEXT_CIRCUIT_UNTIL_KEY
_context_health_zero = _admin_context_routes.context_health_zero
_hourly_context_metric_keys = _admin_context_routes.hourly_context_metric_keys

# Request-event symbols remain exported from this route for compatibility.
_RequestEventImageOut = _request_events.RequestEventImageOut
_RequestEventLiveLane = _request_events.RequestEventLiveLane
_RequestEventOut = _request_events.RequestEventOut
_RequestEventModelStatOut = _request_events.RequestEventModelStatOut
_RequestEventsOut = _request_events.RequestEventsOut

# Request-event compatibility facade.  Keep the old private names available
# because operators and tests import them directly.
_REQUEST_EVENT_STATUSES = _request_events.REQUEST_EVENT_STATUSES
_REQUEST_EVENT_RANGE_HOURS = _request_events.REQUEST_EVENT_RANGE_HOURS
_request_provider = _request_events.request_provider
_request_provider_from_attempts = _request_events.request_provider_from_attempts
_request_route = _request_events.request_route
_image_inflight_key = _request_events.image_inflight_key
_is_inflight_status = _request_events.is_inflight_status
_decode_inflight_value = _request_events.decode_inflight_value
_decode_inflight_hash = _request_events.decode_inflight_hash
_fetch_image_inflight = _request_events.fetch_image_inflight
_build_live_lanes_from_snapshot = _request_events.build_live_lanes_from_snapshot


def _request_actual_route(request: dict[str, Any] | None) -> str | None:
    return _request_events.json_str(
        request, "actual_route", "actual_source", "actual_endpoint"
    )


_short_model = _request_events.short_model
_responses_model_from_request = _request_events.responses_model_from_request
_generation_model_label_from_request = (
    _request_events.generation_model_label_from_request
)
_generation_model_label = _request_events.generation_model_label
_request_event_model_stat_label = _request_events.request_event_model_stat_label
_generation_endpoint = _request_events.generation_endpoint
_safe_upstream_details = _request_events.safe_upstream_details


def _duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    *,
    now: datetime,
) -> int | None:
    return _request_events.duration_ms(started_at, finished_at, now)


def _normalize_request_event_status(status: str | None) -> str | None:
    return _request_events.normalize_request_event_status(status, http_error=_http)


def _request_event_since(
    range: Literal["24h", "7d", "30d"],
    now: datetime,
) -> datetime:
    return _request_events.request_event_since(range, now)


_request_event_sort_key = _request_events.request_event_sort_key
_request_event_time_filter = _request_events.request_event_time_filter
_message_output_image_refs = _request_events.message_output_image_refs
_request_event_model_stats_from_counts = (
    _request_events.request_event_model_stats_from_counts
)
_request_event_prompt = _request_events.request_event_prompt


def _admin_image_binary_url(image_id: str) -> str:
    return f"/api/admin/images/{image_id}/binary"


def _admin_image_variant_url(image_id: str, kind: str) -> str:
    return f"/api/admin/images/{image_id}/variants/{kind}"


def _event_image_out(
    img: Image,
    roles: set[Literal["input", "output"]],
    variant_kinds: set[str],
) -> _RequestEventImageOut:
    return _request_events.event_image_out(
        img,
        roles,
        variant_kinds,
        image_binary_url=_admin_image_binary_url,
        image_variant_url=_admin_image_variant_url,
    )


async def _request_event_model_stats_for_filters(
    db: AsyncSession,
    *,
    since: datetime,
    kind: Literal["all", "generation", "completion"],
    status: str | None,
) -> list[_RequestEventModelStatOut]:
    return await _request_events.request_event_model_stats_for_filters(
        db,
        since=since,
        kind=kind,
        status=status,
    )


async def _read_context_circuit(redis: Any, now: datetime) -> tuple[str, str | None]:
    return await _admin_context_routes.read_context_circuit(redis, now)


@router.get("/context/health")
async def context_health(_admin: AdminUser) -> dict:
    return await _admin_context_routes.context_health(
        deps=_admin_context_routes.ContextHealthDependencies(
            get_redis=get_redis,
            now=lambda: datetime.now(timezone.utc),
            logger=logger,
        )
    )


# ---------- AllowedEmails ----------


_AllowedEmailIn = _admin_allowed_email_routes.AllowedEmailIn


@router.get("/allowed_emails")
async def list_allowed_emails(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _admin_allowed_email_routes.list_allowed_emails(db)


def _allowed_email_dependencies() -> (
    _admin_allowed_email_routes.AllowedEmailDependencies
):
    return _admin_allowed_email_routes.AllowedEmailDependencies(
        http_error=_http,
        write_admin_audit=write_admin_audit,
        hash_email=hash_email,
    )


@router.post(
    "/allowed_emails",
    response_model=AllowedEmailOut,
    status_code=201,
    dependencies=[Depends(verify_csrf)],
)
async def add_allowed_email(
    body: _AllowedEmailIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AllowedEmailOut:
    return await _admin_allowed_email_routes.add_allowed_email(
        body=body,
        request=request,
        admin=admin,
        db=db,
        deps=_allowed_email_dependencies(),
    )


@router.delete(
    "/allowed_emails/{ae_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def delete_allowed_email(
    ae_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _admin_allowed_email_routes.delete_allowed_email(
        allowed_email_id=ae_id,
        request=request,
        admin=admin,
        db=db,
        deps=_allowed_email_dependencies(),
    )


# ---------- Users ----------


def _encode_cursor(created_at: datetime, user_id: str) -> str:
    raw = f"{ensure_utc(created_at).isoformat()}|{user_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    if "|" not in raw:
        raise _http("invalid_cursor", "invalid cursor", 400)
    ts, uid = raw.split("|", 1)
    if not ts or not uid:
        raise _http("invalid_cursor", "invalid cursor", 400)
    try:
        created_at = ensure_utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    except ValueError as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    return created_at, uid


class _AdminSetUserPasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class _AdminUserHistoryImageOut(BaseModel):
    id: str
    url: str
    display_url: str
    preview_url: str | None = None
    thumb_url: str | None = None
    width: int
    height: int
    mime: str


class _AdminUserHistoryItemOut(BaseModel):
    id: str
    kind: Literal["generation"]
    created_at: datetime
    status: str
    prompt: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    message_id: str | None = None
    retention_state: Literal["active", "hidden", "deleted"] = "active"
    images: list[_AdminUserHistoryImageOut] = Field(default_factory=list)


class _AdminUserHistoryOut(BaseModel):
    user: AdminUserOut
    items: list[_AdminUserHistoryItemOut]


def _admin_history_image_out(
    img: Image,
    variant_kinds: set[str],
) -> _AdminUserHistoryImageOut:
    return _AdminUserHistoryImageOut(
        id=img.id,
        url=_admin_image_binary_url(img.id),
        display_url=_admin_image_variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            _admin_image_variant_url(img.id, "preview1024")
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            _admin_image_variant_url(img.id, "thumb256")
            if "thumb256" in variant_kinds
            else None
        ),
        width=img.width,
        height=img.height,
        mime=img.mime,
    )


@router.get("/users")
async def list_users(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict:
    # scalar subqueries for per-user counts
    gen_count = (
        select(func.count(Generation.id))
        .where(Generation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    comp_count = (
        select(func.count(Completion.id))
        .where(Completion.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    # messages owned by user = messages in user's conversations with role='user'
    msg_count = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )

    stmt = (
        select(
            User.id,
            User.email,
            User.role,
            User.account_mode,
            User.display_name,
            User.created_at,
            gen_count.label("generations_count"),
            comp_count.label("completions_count"),
            msg_count.label("messages_count"),
        )
        .where(User.deleted_at.is_(None))
        .order_by(User.created_at.desc(), User.id.desc())
    )

    if cursor:
        ts, uid = _decode_cursor(cursor)
        # keyset pagination (created_at, id) desc
        stmt = stmt.where(
            (User.created_at < ts) | ((User.created_at == ts) & (User.id < uid))
        )

    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AdminUserOut(
            id=r.id,
            email=r.email,
            role=r.role,
            account_mode=r.account_mode,
            display_name=r.display_name or None,
            created_at=r.created_at,
            generations_count=int(r.generations_count or 0),
            completions_count=int(r.completions_count or 0),
            messages_count=int(r.messages_count or 0),
        )
        for r in rows
    ]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return {"items": items, "next_cursor": next_cursor}


async def _admin_user_out(db: AsyncSession, user_id: str) -> AdminUserOut:
    gen_count = (
        select(func.count(Generation.id))
        .where(Generation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    comp_count = (
        select(func.count(Completion.id))
        .where(Completion.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    msg_count = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    row = (
        await db.execute(
            select(
                User.id,
                User.email,
                User.role,
                User.account_mode,
                User.display_name,
                User.created_at,
                gen_count.label("generations_count"),
                comp_count.label("completions_count"),
                msg_count.label("messages_count"),
            ).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise _http("not_found", "user not found", 404)
    return AdminUserOut(
        id=row.id,
        email=row.email,
        role=row.role,
        account_mode=row.account_mode,
        display_name=row.display_name or None,
        created_at=row.created_at,
        generations_count=int(row.generations_count or 0),
        completions_count=int(row.completions_count or 0),
        messages_count=int(row.messages_count or 0),
    )


@router.get("/users/{user_id}/history", response_model=_AdminUserHistoryOut)
async def get_user_history(
    user_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
) -> _AdminUserHistoryOut:
    user_out = await _admin_user_out(db, user_id)
    rows = (
        await db.execute(
            select(
                Generation,
                Conversation.id.label("conversation_id"),
                Conversation.title.label("conversation_title"),
            )
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Message.deleted_at.is_(None),
                Conversation.deleted_at.is_(None),
            )
            .order_by(desc(Generation.created_at), desc(Generation.id))
            .limit(limit)
        )
    ).all()
    generations = [row[0] for row in rows]
    gen_ids = [gen.id for gen in generations]
    images_by_gen: dict[str, list[Image]] = {}
    variant_map: dict[str, set[str]] = {}
    if gen_ids:
        images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_(gen_ids),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars()
        )
        for img in images:
            if img.owner_generation_id:
                images_by_gen.setdefault(img.owner_generation_id, []).append(img)
        if images:
            variant_rows = (
                await db.execute(
                    select(ImageVariant.image_id, ImageVariant.kind).where(
                        ImageVariant.image_id.in_([img.id for img in images])
                    )
                )
            ).all()
            for image_id, kind in variant_rows:
                variant_map.setdefault(image_id, set()).add(kind)

    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    items: list[_AdminUserHistoryItemOut] = []
    for gen, conversation_id, conversation_title in rows:
        item_images = [
            _admin_history_image_out(img, variant_map.get(img.id, set()))
            for img in images_by_gen.get(gen.id, [])
        ]
        items.append(
            _AdminUserHistoryItemOut(
                id=gen.id,
                kind="generation",
                created_at=gen.created_at,
                status=gen.status,
                prompt=gen.prompt,
                conversation_id=conversation_id,
                conversation_title=conversation_title or None,
                message_id=gen.message_id,
                retention_state=byok_retention_state(
                    account_mode=user_out.account_mode,
                    created_at=gen.created_at,
                    policy=policy,
                ),
                images=item_images,
            )
        )
    return _AdminUserHistoryOut(user=user_out, items=items)


@router.patch(
    "/users/{user_id}/password",
    dependencies=[Depends(verify_csrf)],
)
async def set_user_password(
    user_id: str,
    body: _AdminSetUserPasswordIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise _http("not_found", "user not found", 404)
    target.password_hash = hash_password(body.password)
    now = datetime.now(timezone.utc)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.user.password_set",
        target_user_id=target.id,
        details={"target_email_hash": hash_email(target.email)},
        autocommit=False,
    )
    await db.commit()
    return {"ok": True}


@router.delete(
    "/users/{user_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_user(
    user_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    if user_id == admin.id:
        raise _http("cannot_delete_self", "admin cannot delete own account", 400)
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise _http("not_found", "user not found", 404)

    now = datetime.now(timezone.utc)
    target.deleted_at = now
    sessions_result = await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    conversations_result = await db.execute(
        update(Conversation)
        .where(Conversation.user_id == target.id, Conversation.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    images_result = await db.execute(
        update(Image)
        .where(Image.user_id == target.id, Image.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    task_cleanup = await cancel_account_active_tasks(
        db,
        user_id=target.id,
        canceled_at=now,
        account_mode=getattr(target, "account_mode", "wallet"),
    )
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.user.delete",
        target_user_id=target.id,
        details={
            "target_email_hash": hash_email(target.email),
            "sessions_revoked": affected_rows(sessions_result),
            "conversations_deleted": affected_rows(conversations_result),
            "images_deleted": affected_rows(images_result),
            "generations_canceled": task_cleanup["generations_canceled"],
            "completions_canceled": task_cleanup["completions_canceled"],
        },
        autocommit=False,
    )
    await db.commit()
    await post_commit_account_task_cleanup(user_id=target.id, cleanup=task_cleanup)
    return {"ok": True}


@router.get("/request_events", response_model=_RequestEventsOut)
async def list_request_events(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=200),
    kind: Literal["all", "generation", "completion"] = Query(default="all"),
    status: str | None = Query(default=None, max_length=32),
    range: Literal["24h", "7d", "30d"] = Query(default="24h"),
) -> _RequestEventsOut:
    runtime = _request_events.RequestEventsRuntime(
        http_error=_http,
        get_redis=get_redis,
        image_binary_url=_admin_image_binary_url,
        image_variant_url=_admin_image_variant_url,
    )
    return await _request_events.list_request_events(
        db,
        limit=limit,
        kind=kind,
        status=status,
        request_range=range,
        runtime=runtime,
    )


@router.get("/images/{image_id}/binary")
async def get_admin_image_binary(
    image_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    img = (
        await db.execute(
            select(Image).where(Image.id == image_id, Image.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    return image_storage_streaming_response(
        image_storage_path(img.storage_key),
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/images/{image_id}/variants/{kind}")
async def get_admin_image_variant(
    image_id: str,
    kind: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    variant_service: Annotated[CreateVariantService, Depends(get_variant_service)],
) -> Response:
    if kind not in ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    img = (
        await db.execute(
            select(Image).where(Image.id == image_id, Image.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    if kind == DISPLAY_VARIANT:
        await db.rollback()
        try:
            variant = await variant_service.ensure_display_variant(image_id)
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
    return image_storage_streaming_response(
        image_storage_path(variant.storage_key),
        media_type=VARIANT_MEDIA_TYPE.get(kind, "application/octet-stream"),
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
    )


@router.get("/dlq")
async def list_dlq(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    include_resolved: bool = Query(default=False),
) -> dict:
    return await _admin_dlq_routes.list_dlq(
        db=db,
        limit=limit,
        include_resolved=include_resolved,
    )


def _dlq_route_dependencies() -> _admin_dlq_routes.DlqRouteDependencies:
    return _admin_dlq_routes.DlqRouteDependencies(
        http_error=_http,
        write_admin_audit=write_admin_audit,
        logger=logger,
    )


@router.post("/dlq/{dlq_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_dlq(
    dlq_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _admin_dlq_routes.retry_dlq(
        dlq_id=dlq_id,
        request=request,
        admin=admin,
        db=db,
        deps=_dlq_route_dependencies(),
    )


@router.post("/dlq/sweep-deleted-users", dependencies=[Depends(verify_csrf)])
async def sweep_dlq_for_deleted_users(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    return await _admin_dlq_routes.sweep_dlq_for_deleted_users(
        request=request,
        admin=admin,
        db=db,
        limit=limit,
        deps=_dlq_route_dependencies(),
    )
