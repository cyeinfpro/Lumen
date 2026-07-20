"""Admin 路由（V1.0 收尾）：邮箱白名单管理 + 用户列表与聚合统计。

所有端点需要 role=admin（AdminUser 依赖）。写操作使用 verify_csrf。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.models import (
    AllowedEmail,
    AuthSession,
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    OutboxDeadLetter,
    OutboxEvent,
    User,
    VideoGeneration,
    WorkflowRun,
)
from lumen_core.schemas import AdminUserOut, AllowedEmailOut
from lumen_core.utils import ensure_utc
from lumen_core.byok_retention import retention_state as byok_retention_state

from ..audit import hash_email
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..db import affected_rows, get_db
from ..deps import AdminUser, verify_csrf
from ..redis_client import get_redis
from ..security import hash_password
from ..services.admin import request_events as _request_events
from ._admin_common import admin_http as _http, write_admin_audit
from .images import (
    ALLOWED_VARIANTS,
    DISPLAY_VARIANT,
    VARIANT_MEDIA_TYPE,
    _ensure_display_variant,
    _fs_path,
    _storage_streaming_response,
)
from .me import _cancel_account_active_tasks, _post_commit_account_task_cleanup


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


_CONTEXT_METRIC_FIELDS = (
    "summary_attempts",
    "summary_successes",
    "summary_failures",
    "manual_compact_calls",
    "cold_start_count",
)
_CONTEXT_CIRCUIT_STATE_KEY = "context:circuit:breaker:state"
_CONTEXT_CIRCUIT_UNTIL_KEY = "context:circuit:breaker:until"

# Request-event symbols remain exported from this route for compatibility.
_RequestEventImageOut = _request_events.RequestEventImageOut
_RequestEventLiveLane = _request_events.RequestEventLiveLane
_RequestEventOut = _request_events.RequestEventOut
_RequestEventModelStatOut = _request_events.RequestEventModelStatOut
_RequestEventsOut = _request_events.RequestEventsOut


def _context_health_zero(
    *,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> dict:
    return {
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "circuit_breaker_state": "closed",
        "circuit_breaker_until": None,
        "last_24h": {
            "summary_attempts": 0,
            "summary_successes": 0,
            "summary_failures": 0,
            "summary_success_rate": 0.0,
            "summary_p50_latency_ms": 0,
            "summary_p95_latency_ms": 0,
            "manual_compact_calls": 0,
            "cold_start_count": 0,
            "fallback_reasons": {},
        },
    }


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redis_int(value: Any) -> int:
    text = _redis_text(value)
    if text is None or not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)
    return int(round(interpolated))


def _extend_latency_samples(samples: list[int], raw: Any) -> None:
    text = _redis_text(raw)
    if not text:
        return
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            samples.extend(_redis_int(item) for item in parsed if _redis_int(item) >= 0)
            return
    except Exception:
        pass
    for part in text.split(","):
        value = _redis_int(part.strip())
        if value >= 0:
            samples.append(value)


def _fold_context_metrics(rows: list[dict[Any, Any]]) -> dict:
    totals = {field: 0 for field in _CONTEXT_METRIC_FIELDS}
    fallback_reasons: dict[str, int] = {}
    latency_samples: list[int] = []
    p50_values: list[int] = []
    p95_values: list[int] = []

    for row in rows:
        normalized = {str(_redis_text(k) or ""): v for k, v in row.items()}
        for field in _CONTEXT_METRIC_FIELDS:
            totals[field] += _redis_int(normalized.get(field))

        for key, value in normalized.items():
            reason: str | None = None
            for prefix in (
                "fallback_reasons:",
                "fallback_reason:",
                "fallback:",
                "fallback_reasons.",
                "fallback_reason.",
            ):
                if key.startswith(prefix):
                    reason = key[len(prefix) :]
                    break
            if reason:
                fallback_reasons[reason] = fallback_reasons.get(reason, 0) + _redis_int(
                    value
                )

        _extend_latency_samples(
            latency_samples, normalized.get("summary_latency_ms_samples")
        )
        _extend_latency_samples(
            latency_samples, normalized.get("summary_latency_samples")
        )
        p50 = _redis_int(normalized.get("summary_p50_latency_ms"))
        p95 = _redis_int(normalized.get("summary_p95_latency_ms"))
        if p50:
            p50_values.append(p50)
        if p95:
            p95_values.append(p95)

    attempts = totals["summary_attempts"]
    successes = totals["summary_successes"]
    success_rate = round(successes / attempts, 3) if attempts > 0 else 0.0
    return {
        **totals,
        "summary_success_rate": success_rate,
        "summary_p50_latency_ms": _percentile(latency_samples, 0.50)
        if latency_samples
        else _percentile(p50_values, 0.50),
        "summary_p95_latency_ms": _percentile(latency_samples, 0.95)
        if latency_samples
        else _percentile(p95_values, 0.95),
        "fallback_reasons": fallback_reasons,
    }


def _hourly_context_metric_keys(now: datetime) -> list[str]:
    current_hour = now.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    return [
        f"context:metrics:hourly:{(current_hour - timedelta(hours=offset)).strftime('%Y%m%d%H')}"
        for offset in range(24)
    ]


def _iso_z(dt: datetime) -> str:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


# Request-event compatibility facade.  Keep the old private names available
# because operators and tests import them directly.
_REQUEST_EVENT_STATUSES = _request_events._REQUEST_EVENT_STATUSES
_REQUEST_EVENT_RANGE_HOURS = _request_events._REQUEST_EVENT_RANGE_HOURS
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
    return _request_events._duration_ms(started_at, finished_at, now)


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
    raw_state = await redis.get(_CONTEXT_CIRCUIT_STATE_KEY)
    state_text = (_redis_text(raw_state) or "closed").strip()
    until: str | None = None
    if state_text.startswith("{"):
        try:
            parsed = json.loads(state_text)
            if isinstance(parsed, dict):
                state_text = str(parsed.get("state") or "closed")
                until = _redis_text(parsed.get("until"))
        except Exception:
            state_text = "closed"
    if state_text not in {"closed", "open", "half_open"}:
        state_text = "closed"

    if until is None:
        raw_until = await redis.get(_CONTEXT_CIRCUIT_UNTIL_KEY)
        until = _redis_text(raw_until)
    if until is None and state_text == "open":
        try:
            ttl_ms = await redis.pttl(_CONTEXT_CIRCUIT_STATE_KEY)
        except Exception:
            ttl_ms = -1
        if ttl_ms and ttl_ms > 0:
            until = _iso_z(now + timedelta(milliseconds=ttl_ms))
    if state_text != "open":
        until = None
    return state_text, until


@router.get("/context/health")
async def context_health(_admin: AdminUser) -> dict:
    out = _context_health_zero()
    redis = get_redis()
    now = datetime.now(timezone.utc)
    try:
        state, until = await _read_context_circuit(redis, now)
        metric_rows = []
        for key in _hourly_context_metric_keys(now):
            metric_rows.append(
                await cast(
                    Awaitable[dict[str, str]],
                    redis.hgetall(key),
                )
            )
        out["circuit_breaker_state"] = state
        out["circuit_breaker_until"] = until
        out["last_24h"] = _fold_context_metrics(metric_rows)
        return out
    except Exception:
        logger.warning("context health degraded", exc_info=True)
        return _context_health_zero(
            degraded=True,
            degrade_reason="redis_unavailable",
        )


# ---------- AllowedEmails ----------


class _AllowedEmailIn(BaseModel):
    email: EmailStr


@router.get("/allowed_emails")
async def list_allowed_emails(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    Inviter = aliased(User)
    rows = (
        await db.execute(
            select(AllowedEmail, Inviter.email)
            .join(
                Inviter,
                and_(
                    Inviter.id == AllowedEmail.invited_by,
                    Inviter.deleted_at.is_(None),
                ),
                isouter=True,
            )
            .order_by(AllowedEmail.created_at.desc())
        )
    ).all()
    items = [
        AllowedEmailOut(
            id=ae.id,
            email=ae.email,
            invited_by_email=inviter_email,
            created_at=ae.created_at,
        )
        for ae, inviter_email in rows
    ]
    return {"items": items}


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
    email = str(body.email).lower().strip()
    exists = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if exists:
        raise _http("already_exists", "email already allowed", 409)

    ae = AllowedEmail(email=email, invited_by=admin.id)
    db.add(ae)
    await db.flush()
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.allowed_email.add",
        details={"email_hash": hash_email(email), "id": ae.id},
    )
    await db.commit()
    await db.refresh(ae)
    return AllowedEmailOut(
        id=ae.id,
        email=ae.email,
        invited_by_email=admin.email,
        created_at=ae.created_at,
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
    ae = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.id == ae_id))
    ).scalar_one_or_none()
    if not ae:
        raise _http("not_found", "allowed email not found", 404)
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.allowed_email.delete",
        details={"email_hash": hash_email(ae.email), "id": ae.id},
    )
    await db.delete(ae)
    await db.commit()
    return None


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
    task_cleanup = await _cancel_account_active_tasks(
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
    await _post_commit_account_task_cleanup(user_id=target.id, cleanup=task_cleanup)
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
    return _storage_streaming_response(
        _fs_path(img.storage_key),
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
    variant = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if variant is None:
        if kind != DISPLAY_VARIANT:
            raise _http("not_found", "variant not found", 404)
        variant = await _ensure_display_variant(db, img)
        await db.commit()
    return _storage_streaming_response(
        _fs_path(variant.storage_key),
        media_type=VARIANT_MEDIA_TYPE.get(kind, "application/octet-stream"),
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
    )


# ---------- DLQ (Outbox dead-letter management) ----------


class _DlqItemOut(BaseModel):
    id: str
    outbox_id: str | None
    event_type: str
    payload: dict[str, Any]
    error_class: str | None
    error_message: str | None
    retry_count: int
    failed_at: datetime
    resolved_at: datetime | None


DlqTaskKind = Literal[
    "generation",
    "completion",
    "video_generation",
    "storyboard_assembly",
]

DlqKind = DlqTaskKind | Literal["sse"]

_DLQ_KIND_BY_EVENT_TYPE: dict[str, DlqKind] = {
    "outbox.generation": "generation",
    "outbox.completion": "completion",
    "outbox.video_generation": "video_generation",
    "outbox.storyboard_assembly": "storyboard_assembly",
    "outbox.sse": "sse",
}


async def _dlq_task_exists(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_id: str,
) -> bool:
    if kind == "generation":
        stmt = select(Generation.id).join(User, User.id == Generation.user_id)
    elif kind == "completion":
        stmt = select(Completion.id).join(User, User.id == Completion.user_id)
    elif kind == "video_generation":
        stmt = select(VideoGeneration.id).join(
            User,
            User.id == VideoGeneration.user_id,
        )
    else:
        stmt = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(WorkflowRun.type == "storyboard")
        )
    exists = (
        await db.execute(
            stmt.where(
                stmt.selected_columns[0] == task_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return exists is not None


async def _soft_deleted_dlq_task_ids(
    db: AsyncSession,
    *,
    kind: DlqTaskKind,
    task_ids: set[str],
) -> set[str]:
    if not task_ids:
        return set()
    if kind == "generation":
        stmt = (
            select(Generation.id)
            .join(User, User.id == Generation.user_id)
            .where(
                Generation.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "completion":
        stmt = (
            select(Completion.id)
            .join(User, User.id == Completion.user_id)
            .where(
                Completion.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    elif kind == "video_generation":
        stmt = (
            select(VideoGeneration.id)
            .join(User, User.id == VideoGeneration.user_id)
            .where(
                VideoGeneration.id.in_(task_ids),
                User.deleted_at.is_not(None),
            )
        )
    else:
        stmt = (
            select(WorkflowRun.id)
            .join(User, User.id == WorkflowRun.user_id)
            .where(
                WorkflowRun.id.in_(task_ids),
                WorkflowRun.type == "storyboard",
                User.deleted_at.is_not(None),
            )
        )
    return set((await db.execute(stmt)).scalars())


async def _soft_deleted_dlq_row_ids(
    db: AsyncSession,
    rows: list[OutboxDeadLetter],
) -> set[str]:
    task_rows_by_kind: dict[DlqTaskKind, dict[str, set[str]]] = {}
    sse_rows_by_user: dict[str, set[str]] = {}

    for row in rows:
        kind = _DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
        if kind is None:
            continue
        payload = dict(row.payload or {})
        if kind == "sse":
            user_id = payload.get("user_id")
            if isinstance(user_id, str) and user_id:
                sse_rows_by_user.setdefault(user_id, set()).add(row.id)
            continue
        task_id = payload.get("task_id") or payload.get("id")
        if isinstance(task_id, str) and task_id:
            task_rows_by_kind.setdefault(kind, {}).setdefault(task_id, set()).add(
                row.id
            )

    row_ids: set[str] = set()
    for kind, rows_by_task in task_rows_by_kind.items():
        deleted_task_ids = await _soft_deleted_dlq_task_ids(
            db,
            kind=kind,
            task_ids=set(rows_by_task),
        )
        for task_id in deleted_task_ids:
            row_ids.update(rows_by_task[task_id])

    if sse_rows_by_user:
        deleted_user_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(sse_rows_by_user),
                        User.deleted_at.is_not(None),
                    )
                )
            ).scalars()
        )
        for user_id in deleted_user_ids:
            row_ids.update(sse_rows_by_user[user_id])

    return row_ids


@router.get("/dlq")
async def list_dlq(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    include_resolved: bool = Query(default=False),
) -> dict:
    stmt = select(OutboxDeadLetter)
    if not include_resolved:
        stmt = stmt.where(OutboxDeadLetter.resolved_at.is_(None))
    stmt = stmt.order_by(desc(OutboxDeadLetter.failed_at)).limit(limit)
    rows = list((await db.execute(stmt)).scalars())
    items = [
        _DlqItemOut(
            id=r.id,
            outbox_id=r.outbox_id,
            event_type=r.event_type,
            payload=dict(r.payload or {}),
            error_class=r.error_class,
            error_message=r.error_message,
            retry_count=r.retry_count,
            failed_at=r.failed_at,
            resolved_at=r.resolved_at,
        )
        for r in rows
    ]
    return {"items": items, "total": len(items)}


async def _load_dlq_retry_row(
    db: AsyncSession,
    dlq_id: str,
) -> OutboxDeadLetter:
    row = (
        await db.execute(
            select(OutboxDeadLetter)
            .where(OutboxDeadLetter.id == dlq_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not row:
        raise _http("not_found", "dlq item not found", 404)
    if row.resolved_at is not None:
        raise _http("already_resolved", "dlq item already resolved", 409)
    return row


def _validate_dlq_retry_row(row: OutboxDeadLetter) -> tuple[DlqKind, dict[str, Any]]:
    kind = _DLQ_KIND_BY_EVENT_TYPE.get(row.event_type)
    if kind is None:
        raise _http(
            "unsupported_event_type",
            f"DLQ retry does not support {row.event_type}",
            422,
        )
    if row.error_class not in {"OutboxEnqueueFailed", "OutboxPublishFailed"}:
        raise _http(
            "unrepairable_dlq_payload",
            "malformed or invalid outbox payload must be repaired before retry",
            422,
        )
    return kind, dict(row.payload or {})


async def _validate_dlq_retry_owner(
    db: AsyncSession,
    *,
    row: OutboxDeadLetter,
    kind: DlqKind,
    payload: dict[str, Any],
    dlq_id: str,
) -> str | None:
    task_id = payload.get("task_id") or payload.get("id")
    if kind == "sse":
        user_id = payload.get("user_id")
        valid = (
            isinstance(user_id, str)
            and bool(user_id)
            and isinstance(payload.get("channel"), str)
            and bool(payload.get("channel"))
            and isinstance(payload.get("event_name"), str)
            and bool(payload.get("event_name"))
            and isinstance(payload.get("data"), dict)
        )
        if not valid:
            raise _http("invalid_payload", "DLQ SSE payload is invalid", 400)
        exists = (
            await db.execute(
                select(User.id).where(
                    User.id == user_id,
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        task_id = user_id
    else:
        if not isinstance(task_id, str) or not task_id:
            raise _http("invalid_task_id", "dlq payload task_id is invalid", 400)
        exists = await _dlq_task_exists(db, kind=kind, task_id=task_id)
    if exists:
        return str(task_id)
    logger.info(
        "dlq retry skipped: task_or_user_missing dlq_id=%s task_id=%s event_type=%s",
        dlq_id,
        task_id,
        row.event_type,
    )
    raise _http(
        "task_not_found",
        "dlq payload references an unknown task or deleted user",
        404,
    )


async def _prepare_dlq_outbox(
    db: AsyncSession,
    *,
    row: OutboxDeadLetter,
    kind: DlqKind,
    payload: dict[str, Any],
) -> OutboxEvent:
    outbox = None
    if row.outbox_id:
        outbox = (
            await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id == row.outbox_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if outbox is None:
        outbox = OutboxEvent(kind=kind, payload={}, published_at=None)
        db.add(outbox)
        await db.flush()
        row.outbox_id = outbox.id
    elif outbox.kind != kind:
        raise _http(
            "outbox_kind_mismatch",
            "DLQ event type does not match its outbox row",
            409,
        )
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = payload
    outbox.published_at = None
    row.retry_count = (row.retry_count or 0) + 1
    row.error_message = "retry scheduled via durable outbox"
    return outbox


@router.post("/dlq/{dlq_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_dlq(
    dlq_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await _load_dlq_retry_row(db, dlq_id)
    kind, payload = _validate_dlq_retry_row(row)
    task_id = await _validate_dlq_retry_owner(
        db,
        row=row,
        kind=kind,
        payload=payload,
        dlq_id=dlq_id,
    )
    outbox = await _prepare_dlq_outbox(
        db,
        row=row,
        kind=kind,
        payload=payload,
    )
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.retry",
        details={
            "dlq_id": dlq_id,
            "event_type": row.event_type,
            "requeued": True,
            "task_id": task_id,
            "outbox_id": outbox.id,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "dlq_id": dlq_id,
        "requeued": True,
        "resolved": False,
        "outbox_id": outbox.id,
    }


@router.post("/dlq/sweep-deleted-users", dependencies=[Depends(verify_csrf)])
async def sweep_dlq_for_deleted_users(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """Mark DLQ rows whose owning user was soft-deleted as resolved.

    Why: ``retry_dlq`` joins ``User.deleted_at IS NULL`` for safety, which
    means dead letters owned by soft-deleted users can never be retried and
    silently accumulate. This sweeper closes them out as ``resolved`` (not
    physically deleted, so the audit/forensics trail is preserved) and
    writes an admin audit row capturing the sweep size.
    """
    swept_ids: list[str] = []
    scanned = 0
    now = datetime.now(timezone.utc)
    cursor: tuple[datetime, str] | None = None
    while True:
        stmt = select(OutboxDeadLetter).where(
            OutboxDeadLetter.resolved_at.is_(None),
            OutboxDeadLetter.event_type.in_(tuple(_DLQ_KIND_BY_EVENT_TYPE)),
        )
        if cursor is not None:
            failed_at, dlq_id = cursor
            stmt = stmt.where(
                or_(
                    OutboxDeadLetter.failed_at > failed_at,
                    and_(
                        OutboxDeadLetter.failed_at == failed_at,
                        OutboxDeadLetter.id > dlq_id,
                    ),
                )
            )
        rows = list(
            (
                await db.execute(
                    stmt.order_by(
                        OutboxDeadLetter.failed_at.asc(),
                        OutboxDeadLetter.id.asc(),
                    ).limit(limit)
                )
            ).scalars()
        )
        if not rows:
            break

        scanned += len(rows)
        deleted_owner_row_ids = await _soft_deleted_dlq_row_ids(db, rows)
        for row in rows:
            if row.id not in deleted_owner_row_ids:
                continue
            row.resolved_at = now
            row.error_message = (
                (row.error_message or "") + " | swept: owner soft-deleted"
            ).strip(" |")
            swept_ids.append(row.id)

        cursor = (rows[-1].failed_at, rows[-1].id)
        if len(rows) < limit:
            break

    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.dlq.sweep_deleted_users",
        details={"swept": len(swept_ids), "scanned": scanned},
    )
    await db.commit()
    logger.info(
        "dlq sweep deleted-users admin=%s swept=%d scanned=%d",
        admin.id,
        len(swept_ids),
        scanned,
    )
    return {"ok": True, "swept": len(swept_ids), "scanned": scanned}
