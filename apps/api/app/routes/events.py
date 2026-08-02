"""Authenticated SSE transport and realtime runtime composition."""

from __future__ import annotations
from collections.abc import AsyncIterator
import logging
import time
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from lumen_core.constants import EVENTS_REPLAY_MAX_SCAN, EVENTS_STREAM_PREFIX

from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import CurrentUser, is_active_session
from ..realtime import (
    ACQUIRE_SSE_SLOT_LUA,
    MAX_SSE_CHANNELS,
    REFRESH_SSE_SLOT_LUA,
    ChannelPolicyError,
    ConnectionEventDeduper,
    ConnectionLimitError,
    EventStreamState,
    acquire_sse_connection_slot,
    compaction_bridge_channels,
    decode_replay_fields,
    event_channels_from_payload,
    format_compaction_sse,
    iter_replay_events,
    normalize_event_id,
    normalize_recoverable_sse_id,
    payload_with_sse_id,
    redis_time_ms,
    refresh_sse_connection_slot,
    release_sse_connection_slot,
    remember_replayed_event,
    replay_channel_selection,
    replay_connection_events,
    replay_payload_matches_channels,
    sanitize_last_event_id,
    select_channels,
    standard_pubsub_events,
    stream_events,
    stream_high_water_id,
    stream_id_for_pubsub_event,
    stream_id_parts,
    task_ids_from_payload,
    validate_channel_limit,
    validate_channels,
)
from ..redis_client import get_redis


__all__ = ["MAX_SSE_CHANNELS", "time"]
router = APIRouter()
logger = logging.getLogger(__name__)
_REPLAY_BATCH_SIZE = 500
_REPLAY_MAX_EVENTS = EVENTS_REPLAY_MAX_SCAN
_SSE_SESSION_REVALIDATION_INTERVAL_SECONDS = 15
_SSE_LIVENESS_EVENTS = frozenset({"keepalive", "idle"})

# Compatibility exports for the existing route contract tests. New application
# code imports these capabilities through app.realtime's public index.
_ACQUIRE_SSE_SLOT_LUA = ACQUIRE_SSE_SLOT_LUA
_REFRESH_SSE_SLOT_LUA = REFRESH_SSE_SLOT_LUA
_ConnectionEventDeduper = ConnectionEventDeduper
_EventStreamState = EventStreamState
_compaction_bridge_channels = compaction_bridge_channels
_decode_replay_fields = decode_replay_fields
_event_channels_from_payload = event_channels_from_payload
_format_compaction_sse = format_compaction_sse
_normalize_event_id = normalize_event_id
_normalize_recoverable_sse_id = normalize_recoverable_sse_id
_payload_with_sse_id = payload_with_sse_id
_remember_replayed_event = remember_replayed_event
_replay_payload_matches_channels = replay_payload_matches_channels
_sanitize_last_event_id = sanitize_last_event_id
_standard_pubsub_events = standard_pubsub_events
_stream_high_water_id = stream_high_water_id
_stream_id_for_pubsub_event = stream_id_for_pubsub_event
_stream_id_parts = stream_id_parts
_task_ids_from_payload = task_ids_from_payload


def _http(
    code: str,
    msg: str,
    http: int = 400,
    extra: dict[str, int] | None = None,
) -> HTTPException:
    error: dict[str, int | str] = {"code": code, "message": msg}
    if extra:
        error.update(extra)
    return HTTPException(status_code=http, detail={"error": error})


def _policy_http_error(
    error: ChannelPolicyError | ConnectionLimitError,
) -> HTTPException:
    return _http(
        error.code,
        error.message,
        error.status_code,
        error.extra,
    )


async def _validate_channels(
    channels: list[str],
    user_id: str,
    db: AsyncSession,
) -> list[str]:
    try:
        return await validate_channels(channels, user_id, db)
    except ChannelPolicyError as exc:
        raise _policy_http_error(exc) from None


async def _acquire_sse_connection_slot(
    redis: Any,
    user_id: str,
    *,
    limit: int = 8,
    ttl_seconds: int = 90,
):
    try:
        return await acquire_sse_connection_slot(
            redis,
            user_id,
            environment=settings.app_env,
            limit=limit,
            ttl_seconds=ttl_seconds,
        )
    except ConnectionLimitError as exc:
        raise _policy_http_error(exc) from None


async def _refresh_sse_connection_slot(
    redis: Any,
    slot,
    *,
    ttl_seconds: int = 90,
) -> bool:
    return await refresh_sse_connection_slot(
        redis,
        slot,
        ttl_seconds=ttl_seconds,
    )


async def _release_sse_connection_slot(redis: Any, slot) -> None:
    await release_sse_connection_slot(redis, slot)


async def _iter_replay_events(
    redis: object,
    *,
    stream_key: str,
    last_event_id: str,
    replay_until_id: str | None = None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
) -> AsyncIterator[dict]:
    async for event in iter_replay_events(
        redis,
        stream_key=stream_key,
        last_event_id=last_event_id,
        replay_until_id=replay_until_id,
        requested_channels=requested_channels,
        include_user_channel=include_user_channel,
        user_channel=user_channel,
        batch_size=_REPLAY_BATCH_SIZE,
        max_events=_REPLAY_MAX_EVENTS,
    ):
        yield event


async def _replay_connection_events(*args: Any, **kwargs: Any) -> AsyncIterator[dict]:
    async for event in replay_connection_events(
        *args,
        **kwargs,
        iter_events=_iter_replay_events,
    ):
        yield event


async def _resolved_last_event_id(
    redis: Any,
    request: Request,
    last_event_id_query: str | None,
) -> str | None:
    raw = request.headers.get("Last-Event-ID") or last_event_id_query
    now_ms = await redis_time_ms(redis) if raw is not None else None
    return sanitize_last_event_id(raw, now_ms=now_ms)


async def _event_stream_state(
    request: Request,
    user_id: str,
    db: AsyncSession,
    channels: str,
    last_event_id_query: str | None,
) -> EventStreamState:
    selection = select_channels(channels, user_id)
    try:
        validate_channel_limit(selection)
    except ChannelPolicyError as exc:
        raise _policy_http_error(exc) from None
    valid_channels = await _validate_channels(selection.requested, user_id, db)
    replay_channels = replay_channel_selection(valid_channels, selection)
    redis = get_redis()
    last_event_id = await _resolved_last_event_id(
        redis,
        request,
        last_event_id_query,
    )
    connection_slot = await _acquire_sse_connection_slot(redis, user_id)
    return EventStreamState(
        is_disconnected=request.is_disconnected,
        redis=redis,
        user_id=user_id,
        valid_channels=valid_channels,
        replay_channels=replay_channels,
        include_user_channel=selection.user_channel in replay_channels,
        user_channel=selection.user_channel,
        stream_key=f"{EVENTS_STREAM_PREFIX}{user_id}",
        last_event_id=last_event_id,
        connection_slot=connection_slot,
    )


async def _session_is_active(session_id: str) -> bool:
    try:
        async with SessionLocal() as session:
            return await is_active_session(session, session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "sse session revalidation failed session_id=%s",
            session_id,
            exc_info=True,
        )
        return False


async def _event_stream(
    state: EventStreamState,
    *,
    db: AsyncSession | None = None,
    session_id: str | None = None,
) -> AsyncIterator[dict]:
    _ = db  # Compatibility argument; request sessions must never back the stream.
    next_session_check = 0.0
    source = stream_events(
        state,
        replay_events=_replay_connection_events,
        release_slot=_release_sse_connection_slot,
    )
    try:
        async for event in source:
            if session_id:
                now = time.monotonic()
                # The periodic check keeps idle connections bounded, but every
                # non-liveness frame must pass a fresh durable session check.
                if (
                    event.get("event") not in _SSE_LIVENESS_EVENTS
                    or now >= next_session_check
                ):
                    if not await _session_is_active(session_id):
                        yield {"event": "auth_invalidated", "data": "{}"}
                        return
                    next_session_check = (
                        now + _SSE_SESSION_REVALIDATION_INTERVAL_SECONDS
                    )
            yield event
    finally:
        await source.aclose()


@router.get("/events")
async def events(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    channels: str = Query(default=""),
    last_event_id_query: str | None = Query(default=None, alias="last_event_id"),
) -> EventSourceResponse:
    state = await _event_stream_state(
        request,
        user.id,
        db,
        channels,
        last_event_id_query,
    )
    session_id = getattr(getattr(request, "state", None), "session_id", None)
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        await rollback()
    return EventSourceResponse(_event_stream(state, session_id=session_id))
