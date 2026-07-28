"""SSE event publishing for API routes.

API handlers sometimes create user-visible events before worker tasks start.
Those events still need the same durable replay contract as worker events:
write the per-user stream first, then publish an envelope carrying ``sse_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

from lumen_core import sse_durable
from lumen_core.constants import (
    EVENTS_STREAM_MAXLEN,
    EVENTS_STREAM_PREFIX,
    EVENTS_STREAM_TTL_SECONDS,
    user_channel,
)

from .observability import (
    sse_live_publish_bytes_total,
    sse_live_publish_duration_seconds,
    sse_live_publish_total,
)

logger = logging.getLogger(__name__)

_XADD_RETRY_DELAYS_SECONDS = (0.05, 0.2)
_LIVE_PUBLISH_RETRY_DELAYS_SECONDS = (0.05,)
_EVENTS_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_XADD_IDEMPOTENT_LUA = sse_durable.XADD_IDEMPOTENT_LUA
_DURABLE_APPEND_CONFIG = sse_durable.DurableSseAppendConfig(
    maxlen=EVENTS_STREAM_MAXLEN,
    dedupe_ttl_seconds=_EVENTS_DEDUPE_TTL_SECONDS,
    stream_ttl_seconds=EVENTS_STREAM_TTL_SECONDS,
)


# Map the process monotonic clock onto the wall-clock epoch without mutable
# process state. Redis stream ids remain the replay and ordering authority;
# ts_ms is only a display hint.
_MONOTONIC_EPOCH_OFFSET_MS = (time.time_ns() - time.monotonic_ns()) // 1_000_000


class SSEPublishEvent(TypedDict):
    user_id: str
    channel: str
    event_name: str
    data: dict[str, Any]


@dataclass(slots=True)
class _PreparedSseEvent:
    event: SSEPublishEvent
    stream_key: str
    envelope: dict[str, Any]
    payload_json: str


async def _monotonic_ts_ms() -> int:
    return _MONOTONIC_EPOCH_OFFSET_MS + time.monotonic_ns() // 1_000_000


async def _refresh_stream_ttl(redis: Any, stream_key: str) -> None:
    expire_fn = getattr(redis, "expire", None)
    if not callable(expire_fn):
        return
    try:
        await expire_fn(stream_key, EVENTS_STREAM_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "api publish_sse_event stream ttl refresh failed key=%s err=%s",
            stream_key,
            exc,
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _live_channels(channel: str, user_id: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((user_channel(user_id), channel)))


def _live_channel_kind(channel: str, user_id: str) -> str:
    return "user" if channel == user_channel(user_id) else "compat"


async def _publish_live_channel(
    redis: Any,
    *,
    user_id: str,
    channel: str,
    payload_json: str,
) -> sse_durable.SseLivePublishOutcome:
    started = time.monotonic()
    payload_bytes = len(payload_json.encode("utf-8"))
    channel_kind = _live_channel_kind(channel, user_id)
    attempts = 0
    outcome = "failed"
    for attempts in range(1, len(_LIVE_PUBLISH_RETRY_DELAYS_SECONDS) + 2):
        try:
            await redis.publish(channel, payload_json)
            outcome = "success"
            break
        except Exception as exc:  # noqa: BLE001
            if attempts <= len(_LIVE_PUBLISH_RETRY_DELAYS_SECONDS):
                logger.warning(
                    "api sse live publish retry channel=%s kind=%s err=%s",
                    channel,
                    channel_kind,
                    exc,
                )
                await asyncio.sleep(_LIVE_PUBLISH_RETRY_DELAYS_SECONDS[attempts - 1])
                continue
            logger.warning(
                "api sse live publish failed channel=%s kind=%s err=%s",
                channel,
                channel_kind,
                exc,
            )
    duration = max(0.0, time.monotonic() - started)
    sse_live_publish_total.labels(
        channel_kind=channel_kind,
        outcome=outcome,
    ).inc()
    sse_live_publish_bytes_total.labels(channel_kind=channel_kind).inc(payload_bytes)
    sse_live_publish_duration_seconds.labels(
        channel_kind=channel_kind,
        outcome=outcome,
    ).observe(duration)
    return sse_durable.SseLivePublishOutcome(
        channel=channel,
        channel_kind=channel_kind,
        outcome=outcome,
        attempts=attempts,
        payload_bytes=payload_bytes,
        duration_seconds=duration,
    )


def _payload_event_id(payload: dict[str, Any]) -> str:
    raw = payload.get("event_id")
    if raw is None or raw == "":
        raw = uuid.uuid4()
    return str(raw)


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _is_lua_xadd_unsupported(exc: Exception) -> bool:
    return sse_durable.is_lua_xadd_unsupported(
        exc
    ) or sse_durable.is_reservation_pending(exc)


def _is_stream_command_unsupported(exc: Exception) -> bool:
    return sse_durable.is_stream_command_unsupported(exc)


async def _xadd_event_once(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
) -> str:
    return await sse_durable.append_sse_event_once(
        redis,
        stream_key=stream_key,
        event_name=event_name,
        event_id=event_id,
        payload_json=payload_json,
        config=_DURABLE_APPEND_CONFIG,
    )


async def publish_sse_event(
    redis: Any,
    *,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> str:
    return (
        await publish_sse_events(
            redis,
            [
                {
                    "user_id": user_id,
                    "channel": channel,
                    "event_name": event_name,
                    "data": data,
                }
            ],
        )
    )[0]


async def _publish_sse_events_individually(
    redis: Any,
    events: list[SSEPublishEvent],
) -> list[str]:
    return [
        await _publish_sse_event_single(
            redis,
            user_id=event["user_id"],
            channel=event["channel"],
            event_name=event["event_name"],
            data=event["data"],
        )
        for event in events
    ]


async def _prepare_sse_events(
    events: list[SSEPublishEvent],
) -> list[_PreparedSseEvent]:
    prepared: list[_PreparedSseEvent] = []
    for event in events:
        payload = dict(event["data"])
        event_id = _payload_event_id(payload)
        payload["event_id"] = event_id
        envelope: dict[str, Any] = {
            "event": event["event_name"],
            "channel": event["channel"],
            "event_id": event_id,
            "ts_ms": await _monotonic_ts_ms(),
            "data": payload,
        }
        prepared.append(
            _PreparedSseEvent(
                event=event,
                stream_key=f"{EVENTS_STREAM_PREFIX}{event['user_id']}",
                envelope=envelope,
                payload_json=_json(envelope),
            )
        )
    return prepared


def _queue_batch_xadd(pipe_eval: Any, prepared: _PreparedSseEvent) -> None:
    event_id = str(prepared.envelope["event_id"])
    pipe_eval(
        _XADD_IDEMPOTENT_LUA,
        2,
        prepared.stream_key,
        f"{prepared.stream_key}:dedupe:{event_id}",
        event_id,
        prepared.event["event_name"],
        prepared.payload_json,
        str(EVENTS_STREAM_MAXLEN),
        str(_EVENTS_DEDUPE_TTL_SECONDS),
        str(EVENTS_STREAM_TTL_SECONDS),
        _DURABLE_APPEND_CONFIG.reservation_prefix,
        f"{_DURABLE_APPEND_CONFIG.reservation_prefix}{uuid.uuid4().hex}",
    )


async def _append_sse_event_batch(
    pipe_fn: Any,
    prepared: list[_PreparedSseEvent],
) -> list[str] | None:
    for attempt in range(3):
        pipe = pipe_fn(transaction=False)
        pipe_eval = getattr(pipe, "eval", None)
        if not callable(pipe_eval):
            return None
        for item in prepared:
            _queue_batch_xadd(pipe_eval, item)
        try:
            raw_ids = await pipe.execute()
            stream_ids = [_decode_redis_value(item) for item in raw_ids]
            if len(stream_ids) != len(prepared):
                raise RuntimeError(
                    f"xadd returned {len(stream_ids)} ids for {len(prepared)} events"
                )
            return stream_ids
        except Exception as exc:  # noqa: BLE001
            if _is_lua_xadd_unsupported(exc):
                logger.warning(
                    "api publish_sse_events xadd batch lua fallback count=%d err=%s",
                    len(prepared),
                    exc,
                )
                return None
            logger.warning(
                "api publish_sse_events xadd batch failed count=%d attempt=%d err=%s",
                len(prepared),
                attempt + 1,
                exc,
            )
            if attempt < len(_XADD_RETRY_DELAYS_SECONDS):
                await asyncio.sleep(_XADD_RETRY_DELAYS_SECONDS[attempt])
    raise RuntimeError(f"publish_sse_events: xadd failed for {len(prepared)} events")


async def _publish_sse_event_batch_live(
    redis: Any,
    prepared: list[_PreparedSseEvent],
    stream_ids: list[str],
) -> None:
    for stream_key in {item.stream_key for item in prepared}:
        await _refresh_stream_ttl(redis, stream_key)
    for item, stream_id in zip(prepared, stream_ids, strict=False):
        item.envelope["sse_id"] = stream_id
        payload_json = _json(item.envelope)
        for live_channel in _live_channels(
            item.event["channel"],
            item.event["user_id"],
        ):
            await _publish_live_channel(
                redis,
                user_id=item.event["user_id"],
                channel=live_channel,
                payload_json=payload_json,
            )


async def publish_sse_events(
    redis: Any,
    events: list[SSEPublishEvent],
) -> list[str]:
    if not events:
        return []
    if len(events) == 1:
        return await _publish_sse_events_individually(redis, events)

    pipe_fn = getattr(redis, "pipeline", None)
    if not callable(pipe_fn):
        return await _publish_sse_events_individually(redis, events)
    prepared = await _prepare_sse_events(events)
    stream_ids = await _append_sse_event_batch(pipe_fn, prepared)
    if stream_ids is None:
        return await _publish_sse_events_individually(redis, events)
    await _publish_sse_event_batch_live(redis, prepared, stream_ids)
    return stream_ids


async def _publish_sse_event_single(
    redis: Any,
    *,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> str:
    payload = dict(data)
    event_id = _payload_event_id(payload)
    payload["event_id"] = event_id
    envelope: dict[str, Any] = {
        "event": event_name,
        "channel": channel,
        "event_id": event_id,
        "ts_ms": await _monotonic_ts_ms(),
        "data": payload,
    }
    stream_key = f"{EVENTS_STREAM_PREFIX}{user_id}"
    stream_id: str | None = None

    for attempt in range(3):
        payload_json = _json(envelope)
        try:
            stream_id = await _xadd_event_once(
                redis,
                stream_key=stream_key,
                event_name=event_name,
                event_id=event_id,
                payload_json=payload_json,
            )
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "api publish_sse_event xadd failed key=%s attempt=%d err=%s",
                stream_key,
                attempt + 1,
                exc,
            )
            if attempt < len(_XADD_RETRY_DELAYS_SECONDS):
                await asyncio.sleep(_XADD_RETRY_DELAYS_SECONDS[attempt])

    if stream_id is None:
        raise RuntimeError(f"publish_sse_event: xadd failed for {stream_key}")

    await _refresh_stream_ttl(redis, stream_key)
    envelope["sse_id"] = stream_id
    payload_json = _json(envelope)
    for live_channel in _live_channels(channel, user_id):
        await _publish_live_channel(
            redis,
            user_id=user_id,
            channel=live_channel,
            payload_json=payload_json,
        )
    return stream_id


__all__ = ["SSEPublishEvent", "publish_sse_event", "publish_sse_events"]
