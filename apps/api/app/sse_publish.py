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
from typing import Any, TypedDict

from lumen_core.constants import (
    EVENTS_STREAM_MAXLEN,
    EVENTS_STREAM_PREFIX,
    EVENTS_STREAM_TTL_SECONDS,
)
logger = logging.getLogger(__name__)

_XADD_RETRY_DELAYS_SECONDS = (0.05, 0.2)
_EVENTS_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_XADD_IDEMPOTENT_LUA = """
local existing = redis.call('GET', KEYS[2])
if existing and existing ~= '' then
  return existing
end
local reserved = redis.call('SET', KEYS[2], '', 'NX', 'EX', tonumber(ARGV[5]))
if not reserved then
  existing = redis.call('GET', KEYS[2])
  if existing and existing ~= '' then
    return existing
  end
  return redis.error_reply('sse dedupe reservation has no stream id')
end
local stream_id = redis.call(
  'XADD',
  KEYS[1],
  'MAXLEN',
  '~',
  tonumber(ARGV[4]),
  '*',
  'event',
  ARGV[2],
  'data',
  ARGV[3],
  'event_id',
  ARGV[1]
)
redis.call('SET', KEYS[2], stream_id, 'XX', 'EX', tonumber(ARGV[5]))
return stream_id
"""

# Per-process monotonic only. Different API workers can still produce
# non-comparable values, so clients must use Redis stream ids for replay
# ordering and treat ts_ms as a display hint.
_LAST_TS_MS = 0
_TS_LOCK = asyncio.Lock()


class SSEPublishEvent(TypedDict):
    user_id: str
    channel: str
    event_name: str
    data: dict[str, Any]


async def _monotonic_ts_ms() -> int:
    global _LAST_TS_MS
    async with _TS_LOCK:
        now = int(time.time() * 1000)
        if now <= _LAST_TS_MS:
            now = _LAST_TS_MS + 1
        _LAST_TS_MS = now
        return now


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


def _payload_event_id(payload: dict[str, Any]) -> str:
    raw = payload.get("event_id")
    if raw is None or raw == "":
        raw = uuid.uuid4()
    return str(raw)


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _has_stream_id(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bytes):
        return value != b""
    return str(value) != ""


def _is_lua_xadd_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown redis command called from script" in message
        or "sse dedupe reservation has no stream id" in message
        or (
            "xadd" in message
            and "script" in message
            and (
                "unknown" in message
                or "unsupported" in message
                or "not allowed" in message
            )
        )
    )


def _is_stream_command_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        or "unknown redis command" in message
        or (
            "xadd" in message and ("unsupported" in message or "not allowed" in message)
        )
    )


async def _read_dedupe_stream_id(redis: Any, dedupe_key: str) -> str | None:
    get_fn = getattr(redis, "get", None)
    if not callable(get_fn):
        return None
    existing = await get_fn(dedupe_key)
    if not _has_stream_id(existing):
        return None
    return _decode_redis_value(existing)


async def _reserve_dedupe_key(redis: Any, dedupe_key: str) -> bool:
    set_fn = getattr(redis, "set", None)
    if not callable(set_fn):
        return True
    return bool(
        await set_fn(
            dedupe_key,
            "",
            nx=True,
            ex=_EVENTS_DEDUPE_TTL_SECONDS,
        )
    )


async def _store_dedupe_stream_id(
    redis: Any, *, dedupe_key: str, stream_id: str
) -> None:
    set_fn = getattr(redis, "set", None)
    if not callable(set_fn):
        return
    try:
        await set_fn(
            dedupe_key,
            stream_id,
            xx=True,
            ex=_EVENTS_DEDUPE_TTL_SECONDS,
        )
    except TypeError:
        await set_fn(dedupe_key, stream_id)


async def _xadd_event_without_lua(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
) -> str:
    dedupe_key = f"{stream_key}:dedupe:{event_id}"
    existing = await _read_dedupe_stream_id(redis, dedupe_key)
    if existing is not None:
        return existing

    reserved = await _reserve_dedupe_key(redis, dedupe_key)
    if not reserved:
        existing = await _read_dedupe_stream_id(redis, dedupe_key)
        if existing is not None:
            return existing
        # Garnet can leave an empty reservation when a Lua script reaches XADD
        # and then rejects that command. Take over that empty reservation.
        delete_fn = getattr(redis, "delete", None)
        if callable(delete_fn):
            await delete_fn(dedupe_key)
            reserved = await _reserve_dedupe_key(redis, dedupe_key)
        if not reserved:
            raise RuntimeError("sse dedupe reservation has no stream id")

    try:
        stream_id = await redis.xadd(
            stream_key,
            {
                "event": event_name,
                "data": payload_json,
                "event_id": event_id,
            },
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_stream_command_unsupported(exc):
            raise RuntimeError(
                "redis stream xadd unsupported; cannot create recoverable sse id"
            ) from exc
        raise
    decoded = _decode_redis_value(stream_id)
    await _store_dedupe_stream_id(redis, dedupe_key=dedupe_key, stream_id=decoded)
    return decoded


async def _xadd_event_once(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
) -> str:
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        try:
            stream_id = await eval_fn(
                _XADD_IDEMPOTENT_LUA,
                2,
                stream_key,
                f"{stream_key}:dedupe:{event_id}",
                event_id,
                event_name,
                payload_json,
                str(EVENTS_STREAM_MAXLEN),
                str(_EVENTS_DEDUPE_TTL_SECONDS),
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_lua_xadd_unsupported(exc):
                raise
            return await _xadd_event_without_lua(
                redis,
                stream_key=stream_key,
                event_name=event_name,
                event_id=event_id,
                payload_json=payload_json,
            )
    else:
        return await _xadd_event_without_lua(
            redis,
            stream_key=stream_key,
            event_name=event_name,
            event_id=event_id,
            payload_json=payload_json,
        )
    if isinstance(stream_id, bytes):
        return stream_id.decode("ascii", errors="replace")
    return str(stream_id)


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


async def publish_sse_events(
    redis: Any,
    events: list[SSEPublishEvent],
) -> list[str]:
    if not events:
        return []
    if len(events) == 1:
        event = events[0]
        return [
            await _publish_sse_event_single(
                redis,
                user_id=event["user_id"],
                channel=event["channel"],
                event_name=event["event_name"],
                data=event["data"],
            )
        ]

    pipe_fn = getattr(redis, "pipeline", None)
    if not callable(pipe_fn):
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

    stream_keys: list[str] = []
    envelopes: list[dict[str, Any]] = []
    payload_jsons: list[str] = []
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
        stream_keys.append(f"{EVENTS_STREAM_PREFIX}{event['user_id']}")
        envelopes.append(envelope)
        payload_jsons.append(_json(envelope))

    stream_ids: list[str] | None = None
    for attempt in range(3):
        pipe = pipe_fn(transaction=False)
        pipe_eval = getattr(pipe, "eval", None)
        if not callable(pipe_eval):
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
        for event, stream_key, envelope, payload_json in zip(
            events, stream_keys, envelopes, payload_jsons, strict=False
        ):
            event_id = str(envelope["event_id"])
            pipe_eval(
                _XADD_IDEMPOTENT_LUA,
                2,
                stream_key,
                f"{stream_key}:dedupe:{event_id}",
                event_id,
                event["event_name"],
                payload_json,
                str(EVENTS_STREAM_MAXLEN),
                str(_EVENTS_DEDUPE_TTL_SECONDS),
            )
        try:
            raw_ids = await pipe.execute()
            ids = [
                item.decode("ascii", errors="replace")
                if isinstance(item, bytes)
                else str(item)
                for item in raw_ids
            ]
            if len(ids) != len(events):
                raise RuntimeError(
                    f"xadd returned {len(ids)} ids for {len(events)} events"
                )
            stream_ids = ids
            break
        except Exception as exc:  # noqa: BLE001
            if _is_lua_xadd_unsupported(exc):
                logger.warning(
                    "api publish_sse_events xadd batch lua fallback count=%d err=%s",
                    len(events),
                    exc,
                )
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
            logger.warning(
                "api publish_sse_events xadd batch failed count=%d attempt=%d err=%s",
                len(events),
                attempt + 1,
                exc,
            )
            if attempt < len(_XADD_RETRY_DELAYS_SECONDS):
                await asyncio.sleep(_XADD_RETRY_DELAYS_SECONDS[attempt])

    if stream_ids is None:
        raise RuntimeError(f"publish_sse_events: xadd failed for {len(events)} events")

    for stream_key in set(stream_keys):
        await _refresh_stream_ttl(redis, stream_key)
    publish_pipe = pipe_fn(transaction=False)
    for event, envelope, stream_id in zip(events, envelopes, stream_ids, strict=False):
        envelope["sse_id"] = stream_id
        publish_pipe.publish(event["channel"], _json(envelope))
    await publish_pipe.execute()
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
    await redis.publish(channel, _json(envelope))
    return stream_id


__all__ = ["SSEPublishEvent", "publish_sse_event", "publish_sse_events"]
