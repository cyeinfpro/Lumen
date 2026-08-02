"""SSE 事件发布辅助。

两个动作组成一次事件发布：
1. `PUBLISH {channel}` + `PUBLISH user:{uid}` —— 保留任务/会话专用消费者，
   同时让 Web 用稳定用户频道接收全部实时事件；消息体共享同一 `sse_id`。
2. `XADD events:user:{uid}` —— 回放 buffer。用户断线重连后用 Last-Event-ID 从这条
   Stream 里补齐未看到的事件。MAXLEN ≈ 86400（~24h）按 DESIGN §8.2。

保持幂等：如果同一事件被重放，订阅方按 event_id 或 SSE id 去重即可。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any

from redis.exceptions import WatchError

from lumen_core import sse_durable
from lumen_core.constants import (
    EVENTS_STREAM_MAXLEN,
    EVENTS_STREAM_PREFIX,
    EVENTS_STREAM_TTL_SECONDS,
    user_channel,
)
from lumen_core.models import OutboxDeadLetter

from app.observability import (
    sse_live_publish_bytes_total,
    sse_live_publish_duration_seconds,
    sse_live_publish_total,
)

logger = logging.getLogger(__name__)

# 24h 粗略上限——redis 的 MAXLEN ~ 是近似修剪
_EVENTS_DLQ_MAXLEN = 1000
_EVENTS_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_DEDUPE_RESERVATION_PREFIX = "pending:"
_DEDUPE_RESERVATION_PENDING_ERROR = "sse dedupe reservation has no stream id"
_STREAM_TTL_NOT_ESTABLISHED_ERROR = "sse stream ttl was not established"
_DEDUPE_RESERVATION_WAIT_SECONDS = 0.25
_DEDUPE_RESERVATION_POLL_SECONDS = 0.025
_DEDUPE_RESERVATION_STALE_SECONDS = 2.0
_DEDUPE_RECOVERY_SCAN_COUNT = 100
_TRANSACTION_RETRIES = 3
_XADD_RETRY_DELAYS_SECONDS = (0.5, 2.0)
_XADD_IDEMPOTENT_LUA = sse_durable.XADD_IDEMPOTENT_LUA


class SSEPublishRetryableError(RuntimeError):
    """The event has no durable replay-stream entry and must be retried."""

    def __init__(
        self,
        *,
        stream_key: str,
        event_id: str,
        diagnostic_dlq_persisted: bool,
    ) -> None:
        self.stream_key = stream_key
        self.event_id = event_id
        self.diagnostic_dlq_persisted = diagnostic_dlq_persisted
        dlq_status = "recorded" if diagnostic_dlq_persisted else "failed"
        super().__init__(
            "publish_event: durable replay stream unavailable "
            f"for {stream_key} event_id={event_id}; caller must retry "
            f"(diagnostic_dlq={dlq_status})"
        )


# Map the process monotonic clock onto the wall-clock epoch without mutable
# process state. Redis stream ids remain the cross-process replay authority;
# ts_ms is only a display hint.
_MONOTONIC_EPOCH_OFFSET_MS = (time.time_ns() - time.monotonic_ns()) // 1_000_000


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
            "publish_event: stream ttl refresh failed key=%s err=%s",
            stream_key,
            exc,
        )


async def _envelope(
    event_name: str,
    channel: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    raw_event_id = data.get("event_id")
    event_id = raw_event_id if raw_event_id not in (None, "") else uuid.uuid4()
    return {
        "event": event_name,
        "channel": channel,
        "data": data,
        "event_id": str(event_id),
        "ts_ms": await _monotonic_ts_ms(),
    }


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
    for attempts in range(1, 3):
        try:
            await redis.publish(channel, payload_json)
            outcome = "success"
            break
        except Exception as exc:  # noqa: BLE001
            if attempts == 1:
                logger.warning(
                    "publish_event: PUBLISH retry channel=%s kind=%s err=%s",
                    channel,
                    channel_kind,
                    exc,
                )
                await asyncio.sleep(0.05)
                continue
            logger.warning(
                "publish_event: PUBLISH failed channel=%s kind=%s err=%s",
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


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _has_stream_id(value: Any) -> bool:
    return sse_durable.has_stream_id(
        value,
        reservation_prefix=_DEDUPE_RESERVATION_PREFIX,
    )


def _is_dedupe_reservation_pending(exc: Exception) -> bool:
    return sse_durable.is_reservation_pending(exc)


def _is_lua_xadd_unsupported(exc: Exception) -> bool:
    return sse_durable.is_lua_xadd_unsupported(exc)


def _is_stream_command_unsupported(exc: Exception) -> bool:
    return sse_durable.is_stream_command_unsupported(exc)


def _durable_append_config() -> sse_durable.DurableSseAppendConfig:
    return sse_durable.DurableSseAppendConfig(
        maxlen=EVENTS_STREAM_MAXLEN,
        dedupe_ttl_seconds=_EVENTS_DEDUPE_TTL_SECONDS,
        stream_ttl_seconds=EVENTS_STREAM_TTL_SECONDS,
        reservation_prefix=_DEDUPE_RESERVATION_PREFIX,
        reservation_wait_seconds=_DEDUPE_RESERVATION_WAIT_SECONDS,
        reservation_poll_seconds=_DEDUPE_RESERVATION_POLL_SECONDS,
        reservation_stale_seconds=_DEDUPE_RESERVATION_STALE_SECONDS,
        recovery_scan_count=_DEDUPE_RECOVERY_SCAN_COUNT,
        transaction_retries=_TRANSACTION_RETRIES,
    )


async def _read_dedupe_stream_id(redis: Any, dedupe_key: str) -> str | None:
    existing = await _read_dedupe_value(redis, dedupe_key)
    if not _has_stream_id(existing):
        return None
    return existing


async def _read_dedupe_value(redis: Any, dedupe_key: str) -> str | None:
    get_fn = getattr(redis, "get", None)
    if not callable(get_fn):
        return None
    existing = await get_fn(dedupe_key)
    if existing is None:
        return None
    return _decode_redis_value(existing)


def _redis_mapping_value(mapping: Any, field: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    if field in mapping:
        return mapping[field]
    encoded = field.encode("utf-8")
    if encoded in mapping:
        return mapping[encoded]
    return None


async def _find_stream_id_by_event_id(
    redis: Any,
    *,
    stream_key: str,
    event_id: str,
) -> str | None:
    xrevrange_fn = getattr(redis, "xrevrange", None)
    if not callable(xrevrange_fn):
        return None
    try:
        rows = await xrevrange_fn(stream_key, count=_DEDUPE_RECOVERY_SCAN_COUNT)
    except TypeError:
        rows = await xrevrange_fn(stream_key, "+", "-", _DEDUPE_RECOVERY_SCAN_COUNT)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "publish_event: dedupe stream recovery scan failed key=%s err=%s",
            stream_key,
            exc,
        )
        return None
    for raw_stream_id, fields in rows or []:
        raw_event_id = _redis_mapping_value(fields, "event_id")
        if raw_event_id is None:
            continue
        if _decode_redis_value(raw_event_id) == event_id:
            return _decode_redis_value(raw_stream_id)
    return None


async def _reservation_stale_enough_to_reclaim(redis: Any, dedupe_key: str) -> bool:
    pttl_fn = getattr(redis, "pttl", None)
    if not callable(pttl_fn):
        return True
    try:
        ttl_ms = int(await pttl_fn(dedupe_key))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "publish_event: dedupe reservation pttl failed key=%s err=%s",
            dedupe_key,
            exc,
        )
        return False
    if ttl_ms < 0:
        return True
    age_ms = (_EVENTS_DEDUPE_TTL_SECONDS * 1000) - ttl_ms
    return age_ms >= int(_DEDUPE_RESERVATION_STALE_SECONDS * 1000)


async def _recover_dedupe_stream_id(
    redis: Any,
    *,
    stream_key: str,
    dedupe_key: str,
    event_id: str,
) -> str | None:
    stream_id = await _find_stream_id_by_event_id(
        redis,
        stream_key=stream_key,
        event_id=event_id,
    )
    if stream_id is None:
        return None
    try:
        await _store_dedupe_stream_id(
            redis,
            dedupe_key=dedupe_key,
            stream_id=stream_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "publish_event: dedupe recovery store failed key=%s err=%s",
            dedupe_key,
            exc,
        )
    return stream_id


async def _wait_for_dedupe_stream_id(
    redis: Any,
    dedupe_key: str,
    *,
    timeout_s: float = _DEDUPE_RESERVATION_WAIT_SECONDS,
) -> str | None:
    """Wait briefly for another publisher to fill an in-flight dedupe reservation.

    Redis fallback mode cannot atomically reserve, XADD, and store the resulting
    stream id. Seeing an existing dedupe key with an empty value therefore means
    another worker may already be between reserve and XADD. Deleting that key
    immediately can duplicate stream entries; a short bounded wait keeps the
    fallback path idempotent without adding user-visible latency in the common
    Lua-capable Redis path.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        existing = await _read_dedupe_stream_id(redis, dedupe_key)
        if existing is not None:
            return existing
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(_DEDUPE_RESERVATION_POLL_SECONDS, remaining))


async def _reserve_dedupe_key(
    redis: Any,
    dedupe_key: str,
    owner_token: str,
) -> bool:
    set_fn = getattr(redis, "set", None)
    if not callable(set_fn):
        return False
    return bool(
        await set_fn(
            dedupe_key,
            owner_token,
            nx=True,
            ex=_EVENTS_DEDUPE_TTL_SECONDS,
        )
    )


async def _reset_pipeline(pipe: Any) -> None:
    reset_fn = getattr(pipe, "reset", None)
    if not callable(reset_fn):
        return
    result = reset_fn()
    if inspect.isawaitable(result):
        await result


def _transaction_pipeline(redis: Any) -> Any:
    pipeline_fn = getattr(redis, "pipeline", None)
    if not callable(pipeline_fn):
        raise RuntimeError(
            "redis transactional pipeline required when Lua XADD is unavailable"
        )
    return pipeline_fn(transaction=True)


async def _compare_delete_reservation(
    redis: Any,
    *,
    dedupe_key: str,
    owner_token: str,
) -> bool:
    """Delete a stale reservation only while its exact owner token is unchanged."""
    for _attempt in range(_TRANSACTION_RETRIES):
        pipe: Any | None = None
        try:
            pipe = _transaction_pipeline(redis)
            await pipe.watch(dedupe_key)
            current = await pipe.get(dedupe_key)
            if current is None or _decode_redis_value(current) != owner_token:
                return False
            pipe.multi()
            pipe.delete(dedupe_key)
            results = await pipe.execute()
            return bool(results and results[0])
        except WatchError:
            continue
        finally:
            if pipe is not None:
                await _reset_pipeline(pipe)
    return False


async def _store_dedupe_stream_id(
    redis: Any,
    *,
    dedupe_key: str,
    stream_id: str,
    owner_token: str | None = None,
) -> bool:
    set_fn = getattr(redis, "set", None)
    if not callable(set_fn):
        return False
    if owner_token is not None:
        for _attempt in range(_TRANSACTION_RETRIES):
            pipe: Any | None = None
            try:
                pipe = _transaction_pipeline(redis)
                await pipe.watch(dedupe_key)
                current = await pipe.get(dedupe_key)
                if current is None or _decode_redis_value(current) != owner_token:
                    return False
                pipe.multi()
                pipe.set(
                    dedupe_key,
                    stream_id,
                    xx=True,
                    ex=_EVENTS_DEDUPE_TTL_SECONDS,
                )
                results = await pipe.execute()
                return bool(results and results[0])
            except WatchError:
                continue
            finally:
                if pipe is not None:
                    await _reset_pipeline(pipe)
        return False
    try:
        return bool(
            await set_fn(
                dedupe_key,
                stream_id,
                xx=True,
                ex=_EVENTS_DEDUPE_TTL_SECONDS,
            )
        )
    except TypeError:
        await set_fn(dedupe_key, stream_id)
        expire_fn = getattr(redis, "expire", None)
        if callable(expire_fn):
            try:
                await expire_fn(dedupe_key, _EVENTS_DEDUPE_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "publish_event: dedupe expire fallback failed key=%s err=%s",
                    dedupe_key,
                    exc,
                )
        return True


async def _xadd_with_transactional_ttl(
    redis: Any,
    *,
    stream_key: str,
    dedupe_key: str,
    owner_token: str,
    event_name: str,
    event_id: str,
    payload_json: str,
) -> str:
    pipe: Any | None = None
    try:
        pipe = _transaction_pipeline(redis)
        await pipe.watch(dedupe_key)
        current = await pipe.get(dedupe_key)
        if current is None or _decode_redis_value(current) != owner_token:
            raise RuntimeError(_DEDUPE_RESERVATION_PENDING_ERROR)
        pipe.multi()
        pipe.xadd(
            stream_key,
            {
                "event": event_name,
                "data": payload_json,
                "event_id": event_id,
            },
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
        pipe.expire(stream_key, EVENTS_STREAM_TTL_SECONDS)
        results = await pipe.execute()
    except WatchError as exc:
        raise RuntimeError(_DEDUPE_RESERVATION_PENDING_ERROR) from exc
    finally:
        if pipe is not None:
            await _reset_pipeline(pipe)
    if not results or len(results) < 2 or not results[1]:
        raise RuntimeError(_STREAM_TTL_NOT_ESTABLISHED_ERROR)
    return _decode_redis_value(results[0])


async def _xadd_event_without_lua(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    event_id: str,
    payload_json: str,
    reclaim_empty_reservation: bool = False,
    reservation_token: str | None = None,
) -> str:
    return await sse_durable.append_sse_event_without_lua(
        redis,
        stream_key=stream_key,
        event_name=event_name,
        event_id=event_id,
        payload_json=payload_json,
        config=_durable_append_config(),
        reclaim_reservation=reclaim_empty_reservation,
        reservation_token=reservation_token,
    )


async def _xadd_event_once(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    envelope: dict[str, Any],
    payload_json: str,
) -> str:
    return await sse_durable.append_sse_event_once(
        redis,
        stream_key=stream_key,
        event_name=event_name,
        event_id=str(envelope["event_id"]),
        payload_json=payload_json,
        config=_durable_append_config(),
    )


async def publish_event(
    redis: Any,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    """发布一条 SSE 事件。

    Args:
        redis: arq 注入的 ArqRedis（继承 redis.asyncio.Redis），支持 publish / xadd
        user_id: 回放 stream 的分片 key
        channel: PubSub 通道名——使用 `task_channel(id)` 或 `user_channel(uid)` 计算
        event_name: `lumen_core.constants.EV_*`
        data: 事件 payload（一定是 dict，不塞 None / bytes）

    Raises:
        SSEPublishRetryableError: durable replay stream 未返回事件 ID。诊断 DLQ
            是否写入成功都不代表源事件已消费，调用方必须保留并重试源事件。
    """
    stream_key = f"{EVENTS_STREAM_PREFIX}{user_id}"
    envelope = await _envelope(event_name, channel, data)
    stream_id: str | None = None
    last_xadd_error: Exception | None = None

    for attempt in range(3):
        payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        try:
            # Write replay stream first so the live PubSub message can carry the
            # same id browsers later send as Last-Event-ID.
            stream_id = await _xadd_event_once(
                redis,
                stream_key=stream_key,
                event_name=event_name,
                envelope=envelope,
                payload_json=payload_json,
            )
            if not _has_stream_id(stream_id):
                raise RuntimeError("XADD returned no durable stream id")
            break
        except Exception as exc:  # noqa: BLE001
            last_xadd_error = exc
            stream_id = None
            logger.warning(
                "publish_event: XADD failed key=%s attempt=%d err=%s",
                stream_key,
                attempt + 1,
                exc,
            )
            if attempt < len(_XADD_RETRY_DELAYS_SECONDS):
                await asyncio.sleep(_XADD_RETRY_DELAYS_SECONDS[attempt])

    if stream_id is None:
        envelope["dlq_id"] = f"dlq-{envelope['ts_ms']}-{uuid.uuid4().hex[:12]}"
        envelope["recoverable"] = False
        redis_dlq_ok = False
        try:
            await redis.lpush(
                f"{stream_key}:dlq",
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            )
            redis_dlq_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "publish_event: DLQ write failed key=%s err=%s", stream_key, exc
            )
        if redis_dlq_ok:
            try:
                await redis.ltrim(f"{stream_key}:dlq", 0, _EVENTS_DLQ_MAXLEN - 1)
                await _refresh_stream_ttl(redis, f"{stream_key}:dlq")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "publish_event: DLQ trim failed key=%s err=%s",
                    stream_key,
                    exc,
                )
        # 追加：持久化到 PG outbox_dead_letter（独立短事务）
        pg_dlq_ok = await _persist_sse_dlq(
            event_name=event_name,
            payload={"user_id": user_id, "channel": channel, "envelope": envelope},
            error_class="XADDFailed",
            error_message=f"all retries failed for stream {stream_key}",
        )
        diagnostic_dlq_persisted = redis_dlq_ok or pg_dlq_ok
        logger.error(
            "publish_event: durable replay unavailable key=%s event_id=%s "
            "diagnostic_dlq_persisted=%s",
            stream_key,
            envelope["event_id"],
            diagnostic_dlq_persisted,
        )
        raise SSEPublishRetryableError(
            stream_key=stream_key,
            event_id=str(envelope["event_id"]),
            diagnostic_dlq_persisted=diagnostic_dlq_persisted,
        ) from last_xadd_error

    await _refresh_stream_ttl(redis, stream_key)
    envelope["sse_id"] = stream_id

    payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    for live_channel in _live_channels(channel, user_id):
        await _publish_live_channel(
            redis,
            user_id=user_id,
            channel=live_channel,
            payload_json=payload_json,
        )


async def _persist_sse_dlq(
    *,
    event_name: str,
    payload: dict[str, Any],
    error_class: str,
    error_message: str,
) -> bool:
    """把彻底失败的 SSE 发布事件写入 PG outbox_dead_letter（独立事务）。

    XADD 全部重试失败后，该路径可能被外层 publisher 重新触发。仅使用发布方提供的
    稳定身份 event_id + user_id + channel 去重，不能用 ts_ms 推断事件相同。
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from .db import SessionLocal

    raw_envelope = payload.get("envelope")
    envelope = raw_envelope if isinstance(raw_envelope, dict) else {}
    raw_event_id = envelope.get("event_id")
    event_id = str(raw_event_id) if raw_event_id not in (None, "") else None
    raw_user_id = payload.get("user_id")
    user_id = str(raw_user_id) if raw_user_id not in (None, "") else None
    raw_channel = payload.get("channel")
    channel = str(raw_channel) if raw_channel not in (None, "") else None
    dedupe_id = (
        str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"lumen:sse-dlq:{event_name}:{event_id}:{user_id}:{channel}",
            )
        )
        if event_id is not None and user_id is not None and channel is not None
        else None
    )

    try:
        async with SessionLocal() as session, session.begin():
            # 缺少完整稳定身份时直接写，避免用时间戳或事件名误丢不同事件。
            if event_id is not None and user_id is not None and channel is not None:
                stmt = (
                    select(OutboxDeadLetter)
                    .where(
                        OutboxDeadLetter.outbox_id.is_(None),
                        OutboxDeadLetter.event_type == f"sse.{event_name}",
                        OutboxDeadLetter.payload["envelope"]["event_id"].as_string()
                        == event_id,
                        OutboxDeadLetter.payload["user_id"].as_string() == user_id,
                        OutboxDeadLetter.payload["channel"].as_string() == channel,
                    )
                    .order_by(OutboxDeadLetter.id.desc())
                )
                rows = (await session.execute(stmt)).scalars().all()
                duplicate = False
                for row in rows:
                    row_payload = row.payload if isinstance(row.payload, dict) else {}
                    row_env = (
                        row_payload.get("envelope")
                        if isinstance(row_payload, dict)
                        else None
                    )
                    if not isinstance(row_env, dict):
                        continue
                    if (
                        str(row_env.get("event_id")) == event_id
                        and str(row_payload.get("user_id")) == user_id
                        and str(row_payload.get("channel")) == channel
                    ):
                        duplicate = True
                        break
                if duplicate:
                    logger.info(
                        "publish_event: PG DLQ dedup hit event=%s event_id=%s "
                        "user_id=%s channel=%s",
                        event_name,
                        event_id,
                        user_id,
                        channel,
                    )
                    return True

            row_kwargs: dict[str, Any] = {
                "outbox_id": None,
                "event_type": f"sse.{event_name}",
                "payload": payload,
                "error_class": error_class,
                "error_message": error_message,
            }
            if dedupe_id is not None:
                row_kwargs["id"] = dedupe_id
            session.add(OutboxDeadLetter(**row_kwargs))
            return True
    except IntegrityError:
        if dedupe_id is not None:
            logger.info(
                "publish_event: PG DLQ concurrent dedup hit event=%s event_id=%s "
                "user_id=%s channel=%s",
                event_name,
                event_id,
                user_id,
                channel,
            )
            return True
        logger.error("publish_event: PG DLQ persist failed event=%s", event_name)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "publish_event: PG DLQ persist failed event=%s err=%s", event_name, exc
        )
        return False


__all__ = ["SSEPublishRetryableError", "publish_event"]
