"""SSE Hub（DESIGN §5.7）。

GET /events?channels=task:abc,conv:xyz,user:me
- Last-Event-ID → 从 events:user:{uid} 回放（XREAD）
- PubSub 订阅请求的频道
- 每 15s 发 `: keepalive` 心跳
- 断开时清理
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import time
import uuid
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from lumen_core.constants import (
    EVENTS_REPLAY_MAX_SCAN,
    EVENTS_STREAM_MAXLEN,
    EVENTS_STREAM_PREFIX,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    VideoGeneration,
    WorkflowRun,
)

from ..db import get_db
from ..deps import CurrentUser
from ..redis_client import get_redis
from ..services import event_replay as _event_replay


_decode_replay_fields = _event_replay.decode_replay_fields
_event_channels_from_payload = _event_replay.event_channels_from_payload
_iter_replay_events_service = _event_replay.iter_replay_events
_normalize_event_id = _event_replay.normalize_event_id
_normalize_recoverable_sse_id = _event_replay.normalize_recoverable_sse_id
_payload_with_sse_id = _event_replay.payload_with_sse_id
_replay_payload_matches_channels = _event_replay.replay_payload_matches_channels
_stream_high_water_id = _event_replay.stream_high_water_id
_stream_id_parts = _event_replay.stream_id_parts
_task_ids_from_payload = _event_replay.task_ids_from_payload


router = APIRouter()
logger = logging.getLogger(__name__)

_COMPACTION_EVENT = "context.compaction"
_COMPACTION_CHANNEL_PREFIX = "lumen:events:conversation:"
_COMPACTION_MERGE_WINDOW_SECONDS = 0.2
MAX_SSE_CHANNELS = 64
SSE_CONNECTION_LIMIT = 8
SSE_CONNECTION_TTL_SECONDS = 90
# 15s 注释级 keepalive 让 nginx / 浏览器知道连接活；
# 60s 内若没有任何 upstream 数据再补一个 JSON `idle` 心跳，
# 让前端区分 “连接活但上游空闲” vs “上游真的有事件”。
_KEEPALIVE_INTERVAL_SECONDS = 15
_IDLE_HEARTBEAT_INTERVAL_SECONDS = 60
_REPLAY_BATCH_SIZE = 500
_REPLAY_MAX_EVENTS = EVENTS_REPLAY_MAX_SCAN


_LAST_EVENT_ID_MAX_AGE_MS = 24 * 60 * 60 * 1000  # 24h replay window cap


async def _redis_time_ms(redis: object) -> int | None:
    try:
        raw = await redis.time()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        seconds = int(raw[0])
        micros = int(raw[1])
    except (TypeError, ValueError):
        return None
    return seconds * 1000 + micros // 1000


def _sanitize_last_event_id(raw: Any, *, now_ms: int | None = None) -> str | None:
    """Validate a client-provided ``Last-Event-ID`` against Redis Stream IDs.

    Why: the value is an attacker-controlled HTTP header. Forwarding a
    malformed or far-future ID to ``XREAD`` makes the read cursor skip the
    real backlog, silently dropping events for the legitimate user.
    """

    if raw is None or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Bound length first so we never feed multi-KB junk into Redis.
    if len(raw) > 64:
        return None
    parts = raw.split("-")
    if len(parts) != 2:
        return None
    ms_str, seq_str = parts
    if not ms_str.isdigit() or not seq_str.isdigit():
        return None
    try:
        ms = int(ms_str)
        int(seq_str)
    except ValueError:
        return None
    # Reject IDs from beyond a sane replay window: too far in the future
    # would skip everything; too old has nothing to replay.
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if ms > now_ms + 60_000:  # tolerate small clock skew
        return None
    if now_ms - ms > _LAST_EVENT_ID_MAX_AGE_MS:
        return None
    return raw


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


def _sse_connection_key(user_id: str) -> str:
    return f"sse:connections:{user_id}"


SseConnectionSlot = tuple[str, str]


async def _acquire_sse_connection_slot(
    redis,
    user_id: str,
    *,
    limit: int = SSE_CONNECTION_LIMIT,
    ttl_seconds: int = SSE_CONNECTION_TTL_SECONDS,
) -> SseConnectionSlot | None:
    key = _sse_connection_key(user_id)
    token = uuid.uuid4().hex
    now = time.time()
    expires_at = now + ttl_seconds
    script = (
        "redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1]); "
        "local count = redis.call('zcard', KEYS[1]); "
        "if count >= tonumber(ARGV[3]) then "
        "redis.call('expire', KEYS[1], ARGV[2]); "
        "return 0; "
        "end; "
        "redis.call('zadd', KEYS[1], ARGV[4], ARGV[5]); "
        "redis.call('expire', KEYS[1], ARGV[2]); "
        "return 1"
    )
    try:
        acquired = int(
            await redis.eval(
                script,
                1,
                key,
                now,
                ttl_seconds,
                limit,
                expires_at,
                token,
            )
        )
        if acquired != 1:
            raise _http(
                "too_many_sse_connections",
                "too many open event streams for this user",
                429,
                {"limit": limit},
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("sse connection limiter unavailable", exc_info=True)
        return None
    return key, token


async def _refresh_sse_connection_slot(
    redis,
    slot: SseConnectionSlot,
    *,
    ttl_seconds: int = SSE_CONNECTION_TTL_SECONDS,
) -> None:
    key, token = slot
    now = time.time()
    expires_at = now + ttl_seconds
    script = (
        "redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1]); "
        "if redis.call('zscore', KEYS[1], ARGV[3]) then "
        "redis.call('zadd', KEYS[1], ARGV[4], ARGV[3]); "
        "redis.call('expire', KEYS[1], ARGV[2]); "
        "return 1; "
        "end; "
        "return 0"
    )
    try:
        await redis.eval(script, 1, key, now, ttl_seconds, token, expires_at)
    except Exception:  # noqa: BLE001
        logger.warning("sse connection limiter refresh failed", exc_info=True)


async def _release_sse_connection_slot(redis, slot: SseConnectionSlot) -> None:
    key, token = slot
    script = (
        "redis.call('zrem', KEYS[1], ARGV[1]); "
        "if redis.call('zcard', KEYS[1]) == 0 then "
        "redis.call('del', KEYS[1]); "
        "else "
        "redis.call('expire', KEYS[1], ARGV[2]); "
        "end; "
        "return 1"
    )
    try:
        await redis.eval(script, 1, key, token, SSE_CONNECTION_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("sse connection limiter release failed", exc_info=True)


async def _validate_channels(
    channels: list[str],
    user_id: str,
    db: AsyncSession,
) -> list[str]:
    """Ensure every requested channel is owned by this user. Silently drop unknown
    formats. Raise 403 if a known channel belongs to someone else."""
    request = _parse_channel_request(channels, user_id)
    ownership = await _load_channel_ownership(request, user_id, db)
    return _authorized_channels(request.parsed, ownership)


@dataclass
class _ChannelRequest:
    parsed: list[tuple[str, str, str]] = field(default_factory=list)
    conversation_ids: set[str] = field(default_factory=set)
    task_ids: set[str] = field(default_factory=set)
    storyboard_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ChannelOwnership:
    conversation_ids: set[str]
    task_ids: set[str]
    storyboard_ids: set[str]


def _parse_channel(
    raw: str,
    user_id: str,
) -> tuple[str, str, str] | None:
    channel = raw.strip()
    if not channel or ":" not in channel:
        return None
    prefix, _, ref = channel.partition(":")
    if prefix not in {"user", "conv", "task", "storyboard"}:
        return None
    if prefix == "user" and ref != user_id:
        raise _http("forbidden_channel", f"cannot subscribe to user:{ref}", 403)
    return channel, prefix, ref


def _parse_channel_request(channels: list[str], user_id: str) -> _ChannelRequest:
    request = _ChannelRequest()
    for raw in channels:
        parsed = _parse_channel(raw, user_id)
        if parsed is None:
            continue
        request.parsed.append(parsed)
        _remember_channel_reference(request, parsed)
    return request


def _remember_channel_reference(
    request: _ChannelRequest,
    parsed: tuple[str, str, str],
) -> None:
    _channel, prefix, ref = parsed
    if prefix == "conv":
        request.conversation_ids.add(ref)
    elif prefix == "task":
        request.task_ids.add(ref)
    elif prefix == "storyboard":
        request.storyboard_ids.add(ref)


async def _owned_conversation_ids(
    db: AsyncSession,
    conversation_ids: set[str],
    user_id: str,
) -> set[str]:
    if not conversation_ids:
        return set()
    rows = await db.execute(
        select(Conversation.id).where(
            Conversation.id.in_(conversation_ids),
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def _owned_task_ids(
    db: AsyncSession,
    task_ids: set[str],
    user_id: str,
) -> set[str]:
    if not task_ids:
        return set()
    owned: set[str] = set()
    for model in (Generation, Completion, VideoGeneration):
        rows = await db.execute(
            select(model.id).where(
                model.id.in_(task_ids),
                model.user_id == user_id,
            )
        )
        owned.update(rows.scalars().all())
    return owned


async def _owned_storyboard_ids(
    db: AsyncSession,
    storyboard_ids: set[str],
    user_id: str,
) -> set[str]:
    if not storyboard_ids:
        return set()
    rows = await db.execute(
        select(WorkflowRun.id).where(
            WorkflowRun.id.in_(storyboard_ids),
            WorkflowRun.user_id == user_id,
            WorkflowRun.type == "storyboard",
            WorkflowRun.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def _load_channel_ownership(
    request: _ChannelRequest,
    user_id: str,
    db: AsyncSession,
) -> _ChannelOwnership:
    conversation_ids = await _owned_conversation_ids(
        db,
        request.conversation_ids,
        user_id,
    )
    task_ids = await _owned_task_ids(db, request.task_ids, user_id)
    storyboard_ids = await _owned_storyboard_ids(
        db,
        request.storyboard_ids,
        user_id,
    )
    return _ChannelOwnership(conversation_ids, task_ids, storyboard_ids)


def _authorized_channel(
    parsed: tuple[str, str, str],
    ownership: _ChannelOwnership,
) -> str:
    channel, prefix, ref = parsed
    if prefix == "conv" and ref not in ownership.conversation_ids:
        raise _http("forbidden_channel", f"conv {ref} not owned", 403)
    if prefix == "task" and ref not in ownership.task_ids:
        raise _http("forbidden_channel", f"task {ref} not owned", 403)
    if prefix == "storyboard" and ref not in ownership.storyboard_ids:
        raise _http("forbidden_channel", f"storyboard {ref} not owned", 403)
    return channel


def _authorized_channels(
    parsed_channels: list[tuple[str, str, str]],
    ownership: _ChannelOwnership,
) -> list[str]:
    return [_authorized_channel(parsed, ownership) for parsed in parsed_channels]


def _compaction_bridge_channels(channels: list[str]) -> dict[str, str]:
    """Map internal Redis compaction channels to public conv:{id} channels."""
    mapped: dict[str, str] = {}
    for channel in channels:
        prefix, _, ref = channel.partition(":")
        if prefix == "conv" and ref:
            mapped[f"{_COMPACTION_CHANNEL_PREFIX}{ref}"] = channel
    return mapped


def _decode_pubsub_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None


def _is_stream_command_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        or "unknown redis command" in message
        or (
            "xadd" in message and ("unsupported" in message or "not allowed" in message)
        )
    )


def _is_compaction_channel(channel: object, bridge_channels: dict[str, str]) -> bool:
    channel_text = _decode_pubsub_text(channel)
    return bool(channel_text and channel_text in bridge_channels)


def _format_compaction_sse(data: str, *, expected_conv_id: str) -> dict | None:
    try:
        payload = json.loads(data)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != _COMPACTION_EVENT:
        return None
    if str(payload.get("conversation_id") or "") != expected_conv_id:
        return None
    return {
        "event": _COMPACTION_EVENT,
        "data": json.dumps(payload, separators=(",", ":")),
    }


def _compaction_conv_id(channel: object) -> str | None:
    channel_text = _decode_pubsub_text(channel)
    if not channel_text or not channel_text.startswith(_COMPACTION_CHANNEL_PREFIX):
        return None
    return channel_text.removeprefix(_COMPACTION_CHANNEL_PREFIX)


async def _stream_id_for_pubsub_event(
    redis: object,
    *,
    stream_key: str,
    event_name: str,
    envelope_event_id: str | None,
    payload: object,
    channel: str | None = None,
) -> str | None:
    """Persist legacy PubSub-only events so live SSE always advances `id`.

    New publishers include ``sse_id`` after writing the Redis stream. This
    fallback is for older or external publishers that only PUBLISH; without an
    SSE id, browser reconnects keep an old Last-Event-ID and replay duplicates.
    """

    event_id = _normalize_event_id(envelope_event_id) or str(uuid.uuid4())
    payload_for_stream = payload if isinstance(payload, dict) else {"data": payload}
    envelope = {
        "event": event_name,
        "event_id": event_id,
        "ts_ms": int(time.time() * 1000),
        "data": payload_for_stream,
    }
    if channel:
        envelope["channel"] = channel
    try:
        raw = await redis.xadd(  # type: ignore[attr-defined]
            stream_key,
            {
                "event": event_name,
                "data": json.dumps(envelope, separators=(",", ":")),
                "event_id": event_id,
            },
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_stream_command_unsupported(exc):
            logger.warning(
                "sse pubsub event has no recoverable id because redis streams are unsupported stream=%s event=%s",
                stream_key,
                event_name,
            )
            return None
        logger.warning(
            "sse pubsub event missing sse_id and xadd fallback failed stream=%s event=%s",
            stream_key,
            event_name,
            exc_info=True,
        )
        return None
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("ascii", errors="replace")
    return str(raw)


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
    async for event in _iter_replay_events_service(
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


def _remember_replayed_event(event: dict, replayed_sse_ids: set[str]) -> bool:
    if event.get("event") == "replay_truncated":
        return True
    replay_id = _normalize_recoverable_sse_id(event.get("id"))
    if replay_id is None:
        return True
    if replay_id in replayed_sse_ids:
        return False
    replayed_sse_ids.add(replay_id)
    return True


async def _replay_connection_events(
    redis: object,
    *,
    stream_key: str,
    last_event_id: str | None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
    user_id: str,
    replayed_sse_ids: set[str],
) -> AsyncIterator[dict]:
    if last_event_id is None:
        return
    try:
        replay_until_id = await _stream_high_water_id(
            redis,
            stream_key=stream_key,
        )
        if replay_until_id is None:
            return
        async for event in _iter_replay_events(
            redis,
            stream_key=stream_key,
            last_event_id=last_event_id,
            replay_until_id=replay_until_id,
            requested_channels=requested_channels,
            include_user_channel=include_user_channel,
            user_channel=user_channel,
        ):
            if _remember_replayed_event(event, replayed_sse_ids):
                yield event
    except Exception:
        logger.warning(
            "sse replay failed user_id=%s stream_key=%s",
            user_id,
            stream_key,
            exc_info=True,
        )


@dataclass(frozen=True)
class _ChannelSelection:
    requested: list[str]
    client_requested: list[str]
    user_channel: str


@dataclass
class _EventStreamState:
    request: Request
    redis: Any
    user_id: str
    valid_channels: list[str]
    replay_channels: set[str]
    include_user_channel: bool
    user_channel: str
    stream_key: str
    last_event_id: str | None
    connection_slot: SseConnectionSlot | None
    last_keepalive: float = field(default_factory=time.monotonic)
    last_upstream: float = field(default_factory=time.monotonic)
    pending_compaction: dict[str, tuple[float, dict]] = field(default_factory=dict)
    replayed_sse_ids: set[str] = field(default_factory=set)


def _channel_selection(channels: str, user_id: str) -> _ChannelSelection:
    client_requested = list(
        dict.fromkeys(c.strip() for c in channels.split(",") if c.strip())
    )
    user_channel = f"user:{user_id}"
    requested = list(client_requested or [user_channel])
    return _ChannelSelection(requested, client_requested, user_channel)


def _validate_channel_limit(selection: _ChannelSelection) -> None:
    requested = selection.requested
    if len(requested) > MAX_SSE_CHANNELS:
        raise _http(
            "too_many_channels",
            f"cannot subscribe to more than {MAX_SSE_CHANNELS} channels",
            400,
            {
                "max_channels": MAX_SSE_CHANNELS,
                "requested_count": len(selection.client_requested),
                "effective_count": len(requested),
            },
        )


def _replay_channel_selection(
    valid: list[str],
    selection: _ChannelSelection,
) -> set[str]:
    replay_requested_channels = set(valid)
    if (
        selection.client_requested
        and selection.user_channel not in selection.client_requested
    ):
        replay_requested_channels.discard(selection.user_channel)
    return replay_requested_channels


async def _resolved_last_event_id(
    redis: Any,
    request: Request,
    last_event_id_query: str | None,
) -> str | None:
    raw = request.headers.get("Last-Event-ID") or last_event_id_query
    # Why: Last-Event-ID is attacker-controlled; an unsanitised value can
    # advance XREAD's cursor past the entire backlog so the client silently
    # misses real events. Require strict `ms-seq` shape and a sane age window
    # measured with Redis server time, because Redis Stream IDs are minted by
    # Redis and the API process clock may move independently.
    now_ms = await _redis_time_ms(redis) if raw is not None else None
    return _sanitize_last_event_id(
        raw,
        now_ms=now_ms,
    )


async def _event_stream_state(
    request: Request,
    user_id: str,
    db: AsyncSession,
    channels: str,
    last_event_id_query: str | None,
) -> _EventStreamState:
    selection = _channel_selection(channels, user_id)
    _validate_channel_limit(selection)
    valid = await _validate_channels(selection.requested, user_id, db)
    replay_channels = _replay_channel_selection(valid, selection)
    redis = get_redis()
    last_event_id = await _resolved_last_event_id(
        redis,
        request,
        last_event_id_query,
    )
    connection_slot = await _acquire_sse_connection_slot(redis, user_id)
    return _EventStreamState(
        request=request,
        redis=redis,
        user_id=user_id,
        valid_channels=valid,
        replay_channels=replay_channels,
        include_user_channel=selection.user_channel in replay_channels,
        user_channel=selection.user_channel,
        stream_key=f"{EVENTS_STREAM_PREFIX}{user_id}",
        last_event_id=last_event_id,
        connection_slot=connection_slot,
    )


async def _subscribe_pubsub(
    pubsub: Any,
    subscribed: list[str],
    user_id: str,
) -> None:
    try:
        await pubsub.subscribe(*subscribed)
    except Exception:
        logger.warning(
            "sse pubsub subscribe failed user_id=%s channels=%d",
            user_id,
            len(subscribed),
            exc_info=True,
        )
        raise


async def _cleanup_pubsub(
    state: _EventStreamState,
    pubsub: Any,
    subscribed: list[str],
    *,
    subscription_started: bool,
) -> None:
    if subscription_started:
        try:
            await pubsub.unsubscribe(*subscribed)
        except Exception:
            logger.warning("sse pubsub unsubscribe failed", exc_info=True)
    await pubsub.aclose()
    if state.connection_slot is not None:
        await _release_sse_connection_slot(state.redis, state.connection_slot)


def _expired_compaction_events(state: _EventStreamState) -> list[dict]:
    now = time.monotonic()
    expired = [
        conversation_id
        for conversation_id, (deadline, _event) in state.pending_compaction.items()
        if deadline <= now
    ]
    return [
        state.pending_compaction.pop(conversation_id)[1] for conversation_id in expired
    ]


def _pubsub_timeout(state: _EventStreamState) -> float:
    if not state.pending_compaction:
        return 1.0
    next_deadline = min(
        deadline for deadline, _event in state.pending_compaction.values()
    )
    return max(0.0, min(1.0, next_deadline - time.monotonic()))


def _decoded_live_event(data: object) -> tuple[dict | None, str, object] | None:
    data_text = _decode_pubsub_text(data)
    if data_text is None:
        return None
    try:
        parsed = json.loads(data_text)
        event_name = parsed.get("event", "message")
        payload = parsed.get("data", parsed)
    except Exception:
        return None, "message", {"raw": data_text}
    return parsed, event_name, payload


def _live_event_ids(parsed: dict | None) -> tuple[str | None, str | None]:
    event_id = _normalize_recoverable_sse_id(
        parsed.get("sse_id") if isinstance(parsed, dict) else None
    )
    envelope_event_id = _normalize_event_id(
        parsed.get("event_id") if isinstance(parsed, dict) else None
    )
    return event_id, envelope_event_id


def _payload_with_event_ids(
    payload: object,
    event_id: str | None,
    envelope_event_id: str | None,
) -> object:
    if event_id is not None:
        payload = _payload_with_sse_id(payload, event_id)
    if envelope_event_id is None:
        return payload
    if not isinstance(payload, dict):
        return {"data": payload, "event_id": envelope_event_id}
    if "event_id" in payload:
        return payload
    return {**payload, "event_id": envelope_event_id}


def _live_sse_event(
    event_name: str,
    payload: object,
    event_id: str | None,
) -> dict:
    event = {
        "event": event_name,
        "data": json.dumps(payload, separators=(",", ":")),
    }
    if event_id is not None:
        event["id"] = event_id
    return event


async def _standard_pubsub_events(
    state: _EventStreamState,
    message: dict,
) -> list[dict]:
    decoded = _decoded_live_event(message.get("data"))
    if decoded is None:
        return []
    parsed, event_name, payload = decoded
    event_id, envelope_event_id = _live_event_ids(parsed)
    if event_id is None:
        event_id = await _stream_id_for_pubsub_event(
            state.redis,
            stream_key=state.stream_key,
            event_name=event_name,
            envelope_event_id=envelope_event_id,
            payload=payload,
            channel=_decode_pubsub_text(message.get("channel")),
        )
    if event_id is not None and event_id in state.replayed_sse_ids:
        return []
    payload = _payload_with_event_ids(payload, event_id, envelope_event_id)
    state.last_upstream = time.monotonic()
    return [_live_sse_event(event_name, payload, event_id)]


def _parsed_sse_payload(event: dict) -> dict | None:
    try:
        payload = json.loads(event["data"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def _persisted_compaction_event(
    state: _EventStreamState,
    event: dict,
    payload: dict | None,
    public_channel: str | None,
) -> tuple[dict, str | None]:
    if payload is None:
        return event, None
    event_id = await _stream_id_for_pubsub_event(
        state.redis,
        stream_key=state.stream_key,
        event_name=_COMPACTION_EVENT,
        envelope_event_id=_normalize_event_id(payload.get("event_id")),
        payload=payload,
        channel=public_channel,
    )
    if not event_id:
        return event, event_id
    payload = _payload_with_sse_id(payload, event_id)
    return {
        "id": event_id,
        "event": _COMPACTION_EVENT,
        "data": json.dumps(payload, separators=(",", ":")),
    }, event_id


def _compaction_message(
    message: dict,
) -> tuple[str, dict] | None:
    conversation_id = _compaction_conv_id(message.get("channel"))
    data_text = _decode_pubsub_text(message.get("data"))
    if not conversation_id or not data_text:
        return None
    event = _format_compaction_sse(
        data_text,
        expected_conv_id=conversation_id,
    )
    if event is None:
        return None
    return conversation_id, event


async def _compaction_pubsub_events(
    state: _EventStreamState,
    message: dict,
    bridge_channels: dict[str, str],
) -> list[dict]:
    decoded = _compaction_message(message)
    if decoded is None:
        return []
    conversation_id, event = decoded
    payload = _parsed_sse_payload(event)
    channel_text = _decode_pubsub_text(message.get("channel"))
    public_channel = bridge_channels.get(channel_text) if channel_text else None
    event, event_id = await _persisted_compaction_event(
        state,
        event,
        payload,
        public_channel,
    )
    if event_id is not None and event_id in state.replayed_sse_ids:
        return []
    phase = payload.get("phase") if payload is not None else None
    if phase == "started":
        state.pending_compaction[conversation_id] = (
            time.monotonic() + _COMPACTION_MERGE_WINDOW_SECONDS,
            event,
        )
        return []
    if phase in {"progress", "completed"}:
        state.pending_compaction.pop(conversation_id, None)
    state.last_upstream = time.monotonic()
    return [event]


async def _pubsub_events(
    state: _EventStreamState,
    message: dict,
    bridge_channels: dict[str, str],
) -> list[dict]:
    if _is_compaction_channel(message.get("channel"), bridge_channels):
        return await _compaction_pubsub_events(state, message, bridge_channels)
    return await _standard_pubsub_events(state, message)


async def _heartbeat_events(state: _EventStreamState) -> list[dict]:
    now = time.monotonic()
    events: list[dict] = []
    if now - state.last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
        state.last_keepalive = now
        if state.connection_slot is not None:
            await _refresh_sse_connection_slot(state.redis, state.connection_slot)
        events.append({"event": "keepalive", "data": "{}"})
    if now - state.last_upstream >= _IDLE_HEARTBEAT_INTERVAL_SECONDS:
        state.last_upstream = now
        events.append(
            {
                "event": "idle",
                "data": json.dumps(
                    {"type": "idle", "ts": int(time.time())},
                    separators=(",", ":"),
                ),
            }
        )
    return events


async def _live_events(
    state: _EventStreamState,
    pubsub: Any,
    bridge_channels: dict[str, str],
) -> AsyncIterator[dict]:
    # A disconnect only closes the subscription. Task cancellation remains an
    # explicit API action so temporary network loss does not kill paid work.
    while not await state.request.is_disconnected():
        for event in _expired_compaction_events(state):
            yield event
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=_pubsub_timeout(state),
        )
        if message is not None:
            for event in await _pubsub_events(state, message, bridge_channels):
                yield event
        for event in await _heartbeat_events(state):
            yield event


async def _event_stream(state: _EventStreamState) -> AsyncIterator[dict]:
    # Subscribe before taking the replay high-water mark. Messages published
    # during replay stay buffered in PubSub and are deduplicated by stream id.
    pubsub = state.redis.pubsub()
    bridge_channels = _compaction_bridge_channels(state.valid_channels)
    subscribed = [*state.valid_channels, *bridge_channels.keys()]
    subscription_started = False
    try:
        await _subscribe_pubsub(pubsub, subscribed, state.user_id)
        subscription_started = True
        continue_with_live_events = True
        async for event in _replay_connection_events(
            state.redis,
            stream_key=state.stream_key,
            last_event_id=state.last_event_id,
            requested_channels=state.replay_channels,
            include_user_channel=state.include_user_channel,
            user_channel=state.user_channel,
            user_id=state.user_id,
            replayed_sse_ids=state.replayed_sse_ids,
        ):
            yield event
            if event.get("event") == "replay_truncated":
                continue_with_live_events = False
        if continue_with_live_events:
            async for event in _live_events(state, pubsub, bridge_channels):
                yield event
    except asyncio.CancelledError:
        raise
    finally:
        await _cleanup_pubsub(
            state,
            pubsub,
            subscribed,
            subscription_started=subscription_started,
        )


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
    return EventSourceResponse(_event_stream(state))
