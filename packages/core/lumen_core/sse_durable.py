"""Shared idempotent Redis Stream append contract for SSE publishers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import time
from typing import Any
import uuid


RESERVATION_PENDING_ERROR = "sse dedupe reservation has no stream id"
STREAM_TTL_ERROR = "sse stream ttl was not established"
XADD_IDEMPOTENT_LUA = """
local existing = redis.call('GET', KEYS[2])
local function is_reservation(value)
  return value == '' or string.sub(value, 1, string.len(ARGV[7])) == ARGV[7]
end
if existing and not is_reservation(existing) then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))
  return existing
end
local reserved = redis.call('SET', KEYS[2], ARGV[8], 'NX', 'EX', tonumber(ARGV[5]))
if not reserved then
  existing = redis.call('GET', KEYS[2])
  if existing and not is_reservation(existing) then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))
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
local ttl_set = redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))
if ttl_set ~= 1 then
  redis.call('XDEL', KEYS[1], stream_id)
  redis.call('DEL', KEYS[2])
  return redis.error_reply('sse stream ttl was not established')
end
redis.call('SET', KEYS[2], stream_id, 'XX', 'EX', tonumber(ARGV[5]))
return stream_id
"""


@dataclass(frozen=True, slots=True)
class DurableSseAppendConfig:
    maxlen: int
    dedupe_ttl_seconds: int
    stream_ttl_seconds: int
    reservation_prefix: str = "pending:"
    reservation_wait_seconds: float = 0.25
    reservation_poll_seconds: float = 0.025
    reservation_stale_seconds: float = 2.0
    recovery_scan_count: int = 100
    transaction_retries: int = 3


@dataclass(frozen=True, slots=True)
class SseLivePublishOutcome:
    channel: str
    channel_kind: str
    outcome: str
    attempts: int
    payload_bytes: int
    duration_seconds: float


def decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def has_stream_id(value: Any, *, reservation_prefix: str = "pending:") -> bool:
    if value is None:
        return False
    decoded = decode_redis_value(value)
    return decoded != "" and not decoded.startswith(reservation_prefix)


def is_reservation_pending(exc: Exception) -> bool:
    return RESERVATION_PENDING_ERROR in str(exc).lower()


def is_lua_xadd_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return "unknown redis command called from script" in message or (
        "xadd" in message
        and "script" in message
        and (
            "unknown" in message or "unsupported" in message or "not allowed" in message
        )
    )


def is_stream_command_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        or "unknown redis command" in message
        or (
            "xadd" in message and ("unsupported" in message or "not allowed" in message)
        )
    )


def _is_watch_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "WatchError"


async def _reset_pipeline(pipe: Any) -> None:
    reset = getattr(pipe, "reset", None)
    if not callable(reset):
        return
    result = reset()
    if inspect.isawaitable(result):
        await result


def _transaction_pipeline(redis: Any) -> Any:
    pipeline = getattr(redis, "pipeline", None)
    if not callable(pipeline):
        raise RuntimeError(
            "redis transactional pipeline required when Lua XADD is unavailable"
        )
    return pipeline(transaction=True)


async def _read_value(redis: Any, key: str) -> str | None:
    get = getattr(redis, "get", None)
    if not callable(get):
        return None
    value = await get(key)
    return None if value is None else decode_redis_value(value)


async def _wait_for_stream_id(
    redis: Any,
    key: str,
    config: DurableSseAppendConfig,
) -> str | None:
    deadline = time.monotonic() + max(0.0, config.reservation_wait_seconds)
    while True:
        current = await _read_value(redis, key)
        if has_stream_id(current, reservation_prefix=config.reservation_prefix):
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(config.reservation_poll_seconds, remaining))


def _mapping_value(mapping: Any, field: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    return mapping.get(field, mapping.get(field.encode("utf-8")))


async def _recover_stream_id(
    redis: Any,
    *,
    stream_key: str,
    dedupe_key: str,
    event_id: str,
    config: DurableSseAppendConfig,
) -> str | None:
    xrevrange = getattr(redis, "xrevrange", None)
    if not callable(xrevrange):
        return None
    try:
        rows = await xrevrange(stream_key, count=config.recovery_scan_count)
    except TypeError:
        rows = await xrevrange(stream_key, "+", "-", config.recovery_scan_count)
    except Exception:  # noqa: BLE001
        return None
    for raw_stream_id, fields in rows or []:
        raw_event_id = _mapping_value(fields, "event_id")
        if raw_event_id is not None and decode_redis_value(raw_event_id) == event_id:
            stream_id = decode_redis_value(raw_stream_id)
            set_value = getattr(redis, "set", None)
            if callable(set_value):
                try:
                    await set_value(
                        dedupe_key,
                        stream_id,
                        xx=True,
                        ex=config.dedupe_ttl_seconds,
                    )
                except TypeError:
                    await set_value(dedupe_key, stream_id)
            return stream_id
    return None


async def _reserve(
    redis: Any,
    *,
    key: str,
    owner_token: str,
    config: DurableSseAppendConfig,
) -> bool:
    set_value = getattr(redis, "set", None)
    if not callable(set_value):
        return False
    return bool(
        await set_value(
            key,
            owner_token,
            nx=True,
            ex=config.dedupe_ttl_seconds,
        )
    )


async def _reservation_is_stale(
    redis: Any,
    key: str,
    config: DurableSseAppendConfig,
) -> bool:
    pttl = getattr(redis, "pttl", None)
    if not callable(pttl):
        return True
    try:
        ttl_ms = int(await pttl(key))
    except Exception:  # noqa: BLE001
        return False
    if ttl_ms < 0:
        return True
    age_ms = (config.dedupe_ttl_seconds * 1000) - ttl_ms
    return age_ms >= int(config.reservation_stale_seconds * 1000)


async def _compare_delete(
    redis: Any,
    *,
    key: str,
    owner_token: str,
    config: DurableSseAppendConfig,
) -> bool:
    for _ in range(config.transaction_retries):
        pipe: Any | None = None
        try:
            pipe = _transaction_pipeline(redis)
            await pipe.watch(key)
            current = await pipe.get(key)
            if current is None or decode_redis_value(current) != owner_token:
                return False
            pipe.multi()
            pipe.delete(key)
            result = await pipe.execute()
            return bool(result and result[0])
        except Exception as exc:  # noqa: BLE001
            if not _is_watch_error(exc):
                raise
        finally:
            if pipe is not None:
                await _reset_pipeline(pipe)
    return False


async def _store_stream_id(
    redis: Any,
    *,
    key: str,
    stream_id: str,
    owner_token: str,
    config: DurableSseAppendConfig,
) -> bool:
    for _ in range(config.transaction_retries):
        pipe: Any | None = None
        try:
            pipe = _transaction_pipeline(redis)
            await pipe.watch(key)
            current = await pipe.get(key)
            if current is None or decode_redis_value(current) != owner_token:
                return False
            pipe.multi()
            pipe.set(
                key,
                stream_id,
                xx=True,
                ex=config.dedupe_ttl_seconds,
            )
            result = await pipe.execute()
            return bool(result and result[0])
        except Exception as exc:  # noqa: BLE001
            if not _is_watch_error(exc):
                raise
        finally:
            if pipe is not None:
                await _reset_pipeline(pipe)
    return False


async def _xadd_with_ttl(
    redis: Any,
    *,
    stream_key: str,
    dedupe_key: str,
    owner_token: str,
    event_name: str,
    event_id: str,
    payload_json: str,
    config: DurableSseAppendConfig,
) -> str:
    pipe: Any | None = None
    try:
        pipe = _transaction_pipeline(redis)
        await pipe.watch(dedupe_key)
        current = await pipe.get(dedupe_key)
        if current is None or decode_redis_value(current) != owner_token:
            raise RuntimeError(RESERVATION_PENDING_ERROR)
        pipe.multi()
        pipe.xadd(
            stream_key,
            {"event": event_name, "data": payload_json, "event_id": event_id},
            maxlen=config.maxlen,
            approximate=True,
        )
        pipe.expire(stream_key, config.stream_ttl_seconds)
        result = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        if _is_watch_error(exc):
            raise RuntimeError(RESERVATION_PENDING_ERROR) from exc
        raise
    finally:
        if pipe is not None:
            await _reset_pipeline(pipe)
    if not result or len(result) < 2 or not result[1]:
        raise RuntimeError(STREAM_TTL_ERROR)
    return decode_redis_value(result[0])


async def append_sse_event_without_lua(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
    config: DurableSseAppendConfig,
    reclaim_reservation: bool = False,
    reservation_token: str | None = None,
) -> str:
    dedupe_key = f"{stream_key}:dedupe:{event_id}"
    owner_token = reservation_token or (
        f"{config.reservation_prefix}{uuid.uuid4().hex}"
    )
    current = await _read_value(redis, dedupe_key)
    if has_stream_id(current, reservation_prefix=config.reservation_prefix):
        return current or ""
    reserved = current == owner_token or await _reserve(
        redis,
        key=dedupe_key,
        owner_token=owner_token,
        config=config,
    )
    if not reserved:
        existing = await _wait_for_stream_id(redis, dedupe_key, config)
        if existing is not None:
            return existing
        recovered = await _recover_stream_id(
            redis,
            stream_key=stream_key,
            dedupe_key=dedupe_key,
            event_id=event_id,
            config=config,
        )
        if recovered is not None:
            return recovered
        if not reclaim_reservation or not await _reservation_is_stale(
            redis,
            dedupe_key,
            config,
        ):
            raise RuntimeError(RESERVATION_PENDING_ERROR)
        stale_owner = await _read_value(redis, dedupe_key)
        if stale_owner is None or has_stream_id(
            stale_owner,
            reservation_prefix=config.reservation_prefix,
        ):
            raise RuntimeError(RESERVATION_PENDING_ERROR)
        if not await _compare_delete(
            redis,
            key=dedupe_key,
            owner_token=stale_owner,
            config=config,
        ):
            raise RuntimeError(RESERVATION_PENDING_ERROR)
        if not await _reserve(
            redis,
            key=dedupe_key,
            owner_token=owner_token,
            config=config,
        ):
            raise RuntimeError(RESERVATION_PENDING_ERROR)
        recovered = await _recover_stream_id(
            redis,
            stream_key=stream_key,
            dedupe_key=dedupe_key,
            event_id=event_id,
            config=config,
        )
        if recovered is not None:
            return recovered

    stream_id = await _xadd_with_ttl(
        redis,
        stream_key=stream_key,
        dedupe_key=dedupe_key,
        owner_token=owner_token,
        event_name=event_name,
        event_id=event_id,
        payload_json=payload_json,
        config=config,
    )
    await _store_stream_id(
        redis,
        key=dedupe_key,
        stream_id=stream_id,
        owner_token=owner_token,
        config=config,
    )
    return stream_id


async def append_sse_event_once(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
    config: DurableSseAppendConfig,
) -> str:
    reservation_token = f"{config.reservation_prefix}{uuid.uuid4().hex}"
    eval_command = getattr(redis, "eval", None)
    if not callable(eval_command):
        return await append_sse_event_without_lua(
            redis,
            stream_key=stream_key,
            event_name=event_name,
            event_id=event_id,
            payload_json=payload_json,
            config=config,
            reservation_token=reservation_token,
        )
    try:
        stream_id = await eval_command(
            XADD_IDEMPOTENT_LUA,
            2,
            stream_key,
            f"{stream_key}:dedupe:{event_id}",
            event_id,
            event_name,
            payload_json,
            str(config.maxlen),
            str(config.dedupe_ttl_seconds),
            str(config.stream_ttl_seconds),
            config.reservation_prefix,
            reservation_token,
        )
    except Exception as exc:  # noqa: BLE001
        if is_reservation_pending(exc):
            existing = await _wait_for_stream_id(
                redis,
                f"{stream_key}:dedupe:{event_id}",
                config,
            )
            if existing is not None:
                return existing
            return await append_sse_event_without_lua(
                redis,
                stream_key=stream_key,
                event_name=event_name,
                event_id=event_id,
                payload_json=payload_json,
                config=config,
                reclaim_reservation=True,
            )
        if not is_lua_xadd_unsupported(exc):
            raise
        return await append_sse_event_without_lua(
            redis,
            stream_key=stream_key,
            event_name=event_name,
            event_id=event_id,
            payload_json=payload_json,
            config=config,
            reclaim_reservation=True,
            reservation_token=reservation_token,
        )
    return decode_redis_value(stream_id)


__all__ = [
    "DurableSseAppendConfig",
    "RESERVATION_PENDING_ERROR",
    "STREAM_TTL_ERROR",
    "SseLivePublishOutcome",
    "XADD_IDEMPOTENT_LUA",
    "append_sse_event_once",
    "append_sse_event_without_lua",
    "decode_redis_value",
    "has_stream_id",
    "is_lua_xadd_unsupported",
    "is_reservation_pending",
    "is_stream_command_unsupported",
]
