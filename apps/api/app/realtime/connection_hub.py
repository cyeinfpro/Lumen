from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from .channel_policy import (
    COMPACTION_CHANNEL_PREFIX,
    compaction_bridge_channels,
)
from .multiplexer import ClientEventQueue, ProcessRealtimeMultiplexer
from .replay import (
    ConnectionEventDeduper,
    normalize_event_id,
    normalize_recoverable_sse_id,
    payload_with_sse_id,
    replay_connection_events,
    stream_id_for_pubsub_event,
)


logger = logging.getLogger("app.routes.events")

COMPACTION_EVENT = "context.compaction"
COMPACTION_MERGE_WINDOW_SECONDS = 0.2
SSE_CONNECTION_LIMIT = 8
SSE_CONNECTION_TTL_SECONDS = 90
KEEPALIVE_INTERVAL_SECONDS = 15
IDLE_HEARTBEAT_INTERVAL_SECONDS = 60

ACQUIRE_SSE_SLOT_LUA = """
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local ttl_seconds = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local token = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
  redis.call('EXPIRE', KEYS[1], ttl_seconds)
  return 0
end
redis.call('ZADD', KEYS[1], now_ms + ttl_seconds * 1000, token)
redis.call('EXPIRE', KEYS[1], ttl_seconds)
return 1
"""

REFRESH_SSE_SLOT_LUA = """
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local token = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZSCORE', KEYS[1], token) then
  redis.call('ZADD', KEYS[1], now_ms + ttl_seconds * 1000, token)
  redis.call('EXPIRE', KEYS[1], ttl_seconds)
  return 1
end
return 0
"""

SseConnectionSlot = tuple[str, str]
DisconnectProbe = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ConnectionLimitError(Exception):
    code: str
    message: str
    status_code: int
    extra: dict[str, int] | None = None


@dataclass
class EventStreamState:
    is_disconnected: DisconnectProbe
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
    connection_slot_lost: bool = False
    pending_compaction: dict[str, tuple[float, dict]] = field(default_factory=dict)
    event_deduper: ConnectionEventDeduper = field(
        default_factory=ConnectionEventDeduper
    )
    multiplexer: ProcessRealtimeMultiplexer | None = None
    owns_multiplexer: bool = False


def sse_connection_key(user_id: str) -> str:
    return f"sse:connections:{user_id}"


async def acquire_sse_connection_slot(
    redis: Any,
    user_id: str,
    *,
    environment: str,
    limit: int = SSE_CONNECTION_LIMIT,
    ttl_seconds: int = SSE_CONNECTION_TTL_SECONDS,
) -> SseConnectionSlot | None:
    key = sse_connection_key(user_id)
    token = uuid.uuid4().hex
    try:
        acquired = int(
            await redis.eval(
                ACQUIRE_SSE_SLOT_LUA,
                1,
                key,
                ttl_seconds,
                limit,
                token,
            )
        )
        if acquired != 1:
            raise ConnectionLimitError(
                "too_many_sse_connections",
                "too many open event streams for this user",
                429,
                {"limit": limit},
            )
    except ConnectionLimitError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("sse connection limiter unavailable", exc_info=True)
        if environment.strip().lower() not in {
            "dev",
            "development",
            "local",
            "test",
        }:
            raise ConnectionLimitError(
                "sse_connection_limiter_unavailable",
                "event stream connection limiter is unavailable",
                503,
            ) from None
        return None
    return key, token


async def refresh_sse_connection_slot(
    redis: Any,
    slot: SseConnectionSlot,
    *,
    ttl_seconds: int = SSE_CONNECTION_TTL_SECONDS,
) -> bool:
    key, token = slot
    try:
        renewed = await redis.eval(
            REFRESH_SSE_SLOT_LUA,
            1,
            key,
            token,
            ttl_seconds,
        )
        if int(renewed or 0) != 1:
            logger.warning("sse connection slot was lost key=%s token=%s", key, token)
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.warning("sse connection limiter refresh failed", exc_info=True)
        return False


async def release_sse_connection_slot(
    redis: Any,
    slot: SseConnectionSlot,
) -> None:
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


def decode_pubsub_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None


def is_compaction_channel(
    channel: object,
    bridge_channels: dict[str, str],
) -> bool:
    channel_text = decode_pubsub_text(channel)
    return bool(channel_text and channel_text in bridge_channels)


def format_compaction_sse(
    data: str,
    *,
    expected_conv_id: str,
) -> dict | None:
    try:
        payload = json.loads(data)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != COMPACTION_EVENT:
        return None
    if str(payload.get("conversation_id") or "") != expected_conv_id:
        return None
    return {
        "event": COMPACTION_EVENT,
        "data": json.dumps(payload, separators=(",", ":")),
    }


def compaction_conv_id(channel: object) -> str | None:
    channel_text = decode_pubsub_text(channel)
    if not channel_text or not channel_text.startswith(COMPACTION_CHANNEL_PREFIX):
        return None
    return channel_text.removeprefix(COMPACTION_CHANNEL_PREFIX)


def expired_compaction_events(state: EventStreamState) -> list[dict]:
    now = time.monotonic()
    expired = [
        conversation_id
        for conversation_id, (deadline, _event) in state.pending_compaction.items()
        if deadline <= now
    ]
    return [
        state.pending_compaction.pop(conversation_id)[1] for conversation_id in expired
    ]


def pubsub_timeout(state: EventStreamState) -> float:
    default_timeout = float(KEEPALIVE_INTERVAL_SECONDS)
    if not state.pending_compaction:
        return default_timeout
    next_deadline = min(
        deadline for deadline, _event in state.pending_compaction.values()
    )
    return max(0.0, min(default_timeout, next_deadline - time.monotonic()))


def decoded_live_event(data: object) -> tuple[dict | None, str, object] | None:
    data_text = decode_pubsub_text(data)
    if data_text is None:
        return None
    try:
        parsed = json.loads(data_text)
        event_name = parsed.get("event", "message")
        payload = parsed.get("data", parsed)
    except Exception:
        return None, "message", {"raw": data_text}
    return parsed, event_name, payload


def live_event_ids(parsed: dict | None) -> tuple[str | None, str | None]:
    event_id = normalize_recoverable_sse_id(
        parsed.get("sse_id") if isinstance(parsed, dict) else None
    )
    envelope_event_id = normalize_event_id(
        parsed.get("event_id") if isinstance(parsed, dict) else None
    )
    return event_id, envelope_event_id


def payload_with_event_ids(
    payload: object,
    event_id: str | None,
    envelope_event_id: str | None,
) -> object:
    if event_id is not None:
        payload = payload_with_sse_id(payload, event_id)
    if envelope_event_id is None:
        return payload
    if not isinstance(payload, dict):
        return {"data": payload, "event_id": envelope_event_id}
    if "event_id" in payload:
        return payload
    return {**payload, "event_id": envelope_event_id}


def live_sse_event(
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


async def standard_pubsub_events(
    state: EventStreamState,
    message: dict,
) -> list[dict]:
    decoded = decoded_live_event(message.get("data"))
    if decoded is None:
        return []
    parsed, event_name, payload = decoded
    event_id, envelope_event_id = live_event_ids(parsed)
    if event_id is None:
        event_id = await stream_id_for_pubsub_event(
            state.redis,
            stream_key=state.stream_key,
            event_name=event_name,
            envelope_event_id=envelope_event_id,
            payload=payload,
            channel=decode_pubsub_text(message.get("channel")),
        )
    if not state.event_deduper.remember(
        sse_id=event_id,
        event_id=envelope_event_id,
    ):
        return []
    payload = payload_with_event_ids(payload, event_id, envelope_event_id)
    state.last_upstream = time.monotonic()
    return [live_sse_event(event_name, payload, event_id)]


def parsed_sse_payload(event: dict) -> dict | None:
    try:
        payload = json.loads(event["data"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def persisted_compaction_event(
    state: EventStreamState,
    event: dict,
    payload: dict | None,
    public_channel: str | None,
) -> tuple[dict, str | None]:
    if payload is None:
        return event, None
    event_id = normalize_recoverable_sse_id(payload.get("sse_id"))
    if event_id is None:
        event_id = await stream_id_for_pubsub_event(
            state.redis,
            stream_key=state.stream_key,
            event_name=COMPACTION_EVENT,
            envelope_event_id=normalize_event_id(payload.get("event_id")),
            payload=payload,
            channel=public_channel,
        )
    if not event_id:
        return event, event_id
    payload = payload_with_sse_id(payload, event_id)
    return {
        "id": event_id,
        "event": COMPACTION_EVENT,
        "data": json.dumps(payload, separators=(",", ":")),
    }, event_id


def compaction_message(message: dict) -> tuple[str, dict] | None:
    conversation_id = compaction_conv_id(message.get("channel"))
    data_text = decode_pubsub_text(message.get("data"))
    if not conversation_id or not data_text:
        return None
    event = format_compaction_sse(data_text, expected_conv_id=conversation_id)
    if event is None:
        return None
    return conversation_id, event


async def compaction_pubsub_events(
    state: EventStreamState,
    message: dict,
    bridge_channels: dict[str, str],
) -> list[dict]:
    decoded = compaction_message(message)
    if decoded is None:
        return []
    conversation_id, event = decoded
    payload = parsed_sse_payload(event)
    channel_text = decode_pubsub_text(message.get("channel"))
    public_channel = bridge_channels.get(channel_text) if channel_text else None
    event, event_id = await persisted_compaction_event(
        state,
        event,
        payload,
        public_channel,
    )
    if not state.event_deduper.remember(
        sse_id=event_id,
        event_id=payload.get("event_id") if payload is not None else None,
    ):
        return []
    phase = payload.get("phase") if payload is not None else None
    if phase == "started":
        state.pending_compaction[conversation_id] = (
            time.monotonic() + COMPACTION_MERGE_WINDOW_SECONDS,
            event,
        )
        return []
    if phase in {"progress", "completed"}:
        state.pending_compaction.pop(conversation_id, None)
    state.last_upstream = time.monotonic()
    return [event]


async def pubsub_events(
    state: EventStreamState,
    message: dict,
    bridge_channels: dict[str, str],
) -> list[dict]:
    if is_compaction_channel(message.get("channel"), bridge_channels):
        return await compaction_pubsub_events(state, message, bridge_channels)
    return await standard_pubsub_events(state, message)


async def heartbeat_events(state: EventStreamState) -> list[dict]:
    now = time.monotonic()
    events: list[dict] = []
    if now - state.last_keepalive >= KEEPALIVE_INTERVAL_SECONDS:
        state.last_keepalive = now
        if state.connection_slot is not None:
            if not await refresh_sse_connection_slot(
                state.redis,
                state.connection_slot,
            ):
                state.connection_slot_lost = True
                events.append(
                    {
                        "event": "recovery_required",
                        "data": json.dumps(
                            {
                                "reason": "connection_slot_lost",
                                "message": "event stream quota lease expired; reconnect after snapshot recovery",
                            },
                            separators=(",", ":"),
                        ),
                    }
                )
                return events
        events.append({"event": "keepalive", "data": "{}"})
    if now - state.last_upstream >= IDLE_HEARTBEAT_INTERVAL_SECONDS:
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


async def live_events(
    state: EventStreamState,
    client_queue: ClientEventQueue,
    bridge_channels: dict[str, str],
) -> AsyncIterator[dict]:
    while not await state.is_disconnected():
        for event in expired_compaction_events(state):
            yield event
        try:
            message = await asyncio.wait_for(
                client_queue.get(),
                timeout=pubsub_timeout(state),
            )
        except TimeoutError:
            message = None
        else:
            if message is None:
                break
        if message is not None:
            for event in await pubsub_events(state, message, bridge_channels):
                yield event
        for event in await heartbeat_events(state):
            yield event


async def stream_events(
    state: EventStreamState,
    *,
    replay_events: Any = replay_connection_events,
    release_slot: Any = release_sse_connection_slot,
) -> AsyncIterator[dict]:
    multiplexer = state.multiplexer
    owns_multiplexer = state.owns_multiplexer
    if multiplexer is None:
        multiplexer = ProcessRealtimeMultiplexer(
            state.redis,
            close_pubsub_when_idle=True,
        )
        owns_multiplexer = True

    bridge_channels = compaction_bridge_channels(state.valid_channels)
    requested_channels = [*state.valid_channels, *bridge_channels.keys()]
    client_queue: ClientEventQueue | None = None
    subscribed: list[str] = []
    try:
        client_queue, subscribed = await multiplexer.subscribe_client(
            requested_channels,
            user_id=state.user_id,
        )
        continue_with_live_events = True
        async for event in replay_events(
            state.redis,
            stream_key=state.stream_key,
            last_event_id=state.last_event_id,
            requested_channels=state.replay_channels,
            include_user_channel=state.include_user_channel,
            user_channel=state.user_channel,
            user_id=state.user_id,
            event_deduper=state.event_deduper,
        ):
            yield event
            if event.get("event") in {
                "replay_truncated",
                "recovery_required",
            }:
                continue_with_live_events = False
        if continue_with_live_events and client_queue is not None:
            async with multiplexer.live_consumer(client_queue):
                async for event in live_events(
                    state,
                    client_queue,
                    bridge_channels,
                ):
                    yield event
                    if event.get("event") == "recovery_required":
                        break
    except asyncio.CancelledError:
        raise
    finally:
        try:
            if client_queue is not None:
                await multiplexer.unsubscribe_client(client_queue, subscribed)
        finally:
            try:
                if owns_multiplexer:
                    await multiplexer.aclose()
            finally:
                if state.connection_slot is not None:
                    await release_slot(state.redis, state.connection_slot)
