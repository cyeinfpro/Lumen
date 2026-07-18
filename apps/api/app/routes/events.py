"""SSE Hub（DESIGN §5.7）。

GET /events?channels=task:abc,conv:xyz,user:me
- Last-Event-ID → 从 events:user:{uid} 回放（XREAD）
- PubSub 订阅请求的频道
- 每 15s 发 `: keepalive` 心跳
- 断开时清理
"""

from __future__ import annotations

import asyncio
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
    parsed: list[tuple[str, str, str]] = []
    conv_refs: set[str] = set()
    task_refs: set[str] = set()
    storyboard_refs: set[str] = set()

    for raw in channels:
        ch = raw.strip()
        if not ch or ":" not in ch:
            continue
        prefix, _, ref = ch.partition(":")
        if prefix == "user":
            if ref != user_id:
                raise _http("forbidden_channel", f"cannot subscribe to user:{ref}", 403)
            parsed.append((ch, prefix, ref))
        elif prefix == "conv":
            parsed.append((ch, prefix, ref))
            conv_refs.add(ref)
        elif prefix == "task":
            parsed.append((ch, prefix, ref))
            task_refs.add(ref)
        elif prefix == "storyboard":
            parsed.append((ch, prefix, ref))
            storyboard_refs.add(ref)
        else:
            # unknown channel prefix — drop silently
            continue

    owned_convs: set[str] = set()
    if conv_refs:
        rows = await db.execute(
            select(Conversation.id).where(
                Conversation.id.in_(conv_refs),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        owned_convs = set(rows.scalars().all())

    owned_tasks: set[str] = set()
    if task_refs:
        gen_rows = await db.execute(
            select(Generation.id).where(
                Generation.id.in_(task_refs), Generation.user_id == user_id
            )
        )
        owned_tasks.update(gen_rows.scalars().all())

        completion_rows = await db.execute(
            select(Completion.id).where(
                Completion.id.in_(task_refs), Completion.user_id == user_id
            )
        )
        owned_tasks.update(completion_rows.scalars().all())

        video_rows = await db.execute(
            select(VideoGeneration.id).where(
                VideoGeneration.id.in_(task_refs),
                VideoGeneration.user_id == user_id,
            )
        )
        owned_tasks.update(video_rows.scalars().all())

    owned_storyboards: set[str] = set()
    if storyboard_refs:
        storyboard_rows = await db.execute(
            select(WorkflowRun.id).where(
                WorkflowRun.id.in_(storyboard_refs),
                WorkflowRun.user_id == user_id,
                WorkflowRun.type == "storyboard",
                WorkflowRun.deleted_at.is_(None),
            )
        )
        owned_storyboards = set(storyboard_rows.scalars().all())

    clean: list[str] = []
    for ch, prefix, ref in parsed:
        if prefix == "user":
            clean.append(ch)
        elif prefix == "conv":
            if ref not in owned_convs:
                raise _http("forbidden_channel", f"conv {ref} not owned", 403)
            clean.append(ch)
        elif prefix == "task":
            if ref not in owned_tasks:
                raise _http("forbidden_channel", f"task {ref} not owned", 403)
            clean.append(ch)
        elif prefix == "storyboard":
            if ref not in owned_storyboards:
                raise _http("forbidden_channel", f"storyboard {ref} not owned", 403)
            clean.append(ch)
    return clean


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


@router.get("/events")
async def events(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    channels: str = Query(default=""),
    last_event_id_query: str | None = Query(default=None, alias="last_event_id"),
) -> EventSourceResponse:
    client_requested = list(
        dict.fromkeys(c.strip() for c in channels.split(",") if c.strip())
    )
    user_channel = f"user:{user.id}"
    requested = list(client_requested or [user_channel])
    if len(requested) > MAX_SSE_CHANNELS:
        raise _http(
            "too_many_channels",
            f"cannot subscribe to more than {MAX_SSE_CHANNELS} channels",
            400,
            {
                "max_channels": MAX_SSE_CHANNELS,
                "requested_count": len(client_requested),
                "effective_count": len(requested),
            },
        )
    valid = await _validate_channels(requested, user.id, db)
    replay_requested_channels = set(valid)
    if client_requested and user_channel not in client_requested:
        replay_requested_channels.discard(user_channel)
    include_user_channel = user_channel in replay_requested_channels

    redis = get_redis()
    last_event_id = request.headers.get("Last-Event-ID") or last_event_id_query
    # Why: Last-Event-ID is attacker-controlled; an unsanitised value can
    # advance XREAD's cursor past the entire backlog so the client silently
    # misses real events. Require strict `ms-seq` shape and a sane age window
    # measured with Redis server time, because Redis Stream IDs are minted by
    # Redis and the API process clock may move independently.
    last_event_id_now_ms = (
        await _redis_time_ms(redis) if last_event_id is not None else None
    )
    last_event_id = _sanitize_last_event_id(
        last_event_id,
        now_ms=last_event_id_now_ms,
    )
    stream_key = f"{EVENTS_STREAM_PREFIX}{user.id}"
    connection_slot_key = await _acquire_sse_connection_slot(redis, user.id)

    async def gen() -> AsyncIterator[dict]:
        slot_released = False

        async def release_slot_once() -> None:
            nonlocal slot_released
            if connection_slot_key is None or slot_released:
                return
            slot_released = True
            await _release_sse_connection_slot(redis, connection_slot_key)

        # Subscribe before capturing the replay boundary so every publish after
        # this point is either replayed up to the boundary or remains buffered
        # for the live loop.
        pubsub = redis.pubsub()
        bridge_channels = _compaction_bridge_channels(valid)
        subscribed = [*valid, *bridge_channels.keys()]
        try:
            await pubsub.subscribe(*subscribed)
        except Exception:
            logger.warning(
                "sse pubsub subscribe failed user_id=%s channels=%d",
                user.id,
                len(subscribed),
                exc_info=True,
            )
            await pubsub.aclose()
            await release_slot_once()
            raise

        last_keepalive = time.monotonic()
        last_upstream = time.monotonic()
        pending_compaction_started: dict[str, tuple[float, dict]] = {}
        replayed_sse_ids: set[str] = set()
        try:
            # Replay from the per-user stream only through the snapshot taken
            # after subscribe. PubSub messages for that interval can therefore
            # arrive again below; `replayed_sse_ids` suppresses that duplicate.
            # GEN-P0-7: `last_event_id` 必须是之前 XREAD 返回的原生 stream ID
            # (`ms-seq`)，这样 XREAD 从它严格之后继续；绝不本地生成假 ID。
            continue_with_live_events = True
            async for event in _replay_connection_events(
                redis,
                stream_key=stream_key,
                last_event_id=last_event_id,
                requested_channels=replay_requested_channels,
                include_user_channel=include_user_channel,
                user_channel=user_channel,
                user_id=user.id,
                replayed_sse_ids=replayed_sse_ids,
            ):
                yield event
                continue_with_live_events = (
                    event.get("event") != "replay_truncated"
                )

            # Keep the PubSub lifecycle inside try/finally:
            # - client disconnects (is_disconnected) break into cleanup;
            # - cancellation (for example ASGI shutdown or timeout) cleans up
            #   and re-raises instead of being swallowed.
            #
            # image-stability-hardening §P2 invariant：客户端断开 **不可** 写 task cancel key。
            # 浏览器关页面、4G/5G 切换、移动端 App 后台都属于"暂时失联"，但生图任务往往
            # 已花了几十秒上游算力——若 SSE 断开就杀任务，用户重连后什么都没拿到等于沉没成本。
            # cancel 必须显式：POST /tasks/generations/{id}/cancel（见 routes/tasks.py）。
            # worker 端 _is_cancelled 仅读 Redis cancel key，本路由只做 pubsub 订阅，二者解耦。
            # A replay_truncated event advances Last-Event-ID and closes this
            # connection. The reconnect continues from that cursor before any
            # live PubSub handoff, so captured backlog cannot be skipped.
            while continue_with_live_events:
                if await request.is_disconnected():
                    # 客户端已断开（浏览器关闭 / nginx 切断），主动退出 generator；
                    # finally 会负责 unsubscribe + close。
                    break

                now = time.monotonic()
                expired_started = [
                    conv_id
                    for conv_id, (
                        deadline,
                        _event,
                    ) in pending_compaction_started.items()
                    if deadline <= now
                ]
                for conv_id in expired_started:
                    _deadline, event = pending_compaction_started.pop(conv_id)
                    yield event

                # Why: default 1.0s instead of 0.25s — at 0.25s every idle
                # SSE connection wakes 4× per second just to spin the loop,
                # which scales poorly with many subscribers. A 1-second
                # baseline still satisfies our keep-alive cadence; when
                # `pending_compaction_started` carries a tighter deadline,
                # narrow the timeout dynamically.
                timeout = 1.0
                if pending_compaction_started:
                    next_deadline = min(
                        deadline
                        for deadline, _event in pending_compaction_started.values()
                    )
                    timeout = max(0.0, min(timeout, next_deadline - time.monotonic()))

                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=timeout
                )
                if msg is not None:
                    data = msg.get("data")
                    channel = msg.get("channel")
                    if _is_compaction_channel(channel, bridge_channels):
                        data_text = _decode_pubsub_text(data)
                        compaction_conv_id = _compaction_conv_id(channel)
                        if data_text and compaction_conv_id:
                            out = _format_compaction_sse(
                                data_text,
                                expected_conv_id=compaction_conv_id,
                            )
                            if out is not None:
                                try:
                                    payload = json.loads(out["data"])
                                    phase = payload.get("phase")
                                except Exception:
                                    payload = None
                                    phase = None
                                channel_text = _decode_pubsub_text(channel)
                                public_channel = (
                                    bridge_channels.get(channel_text)
                                    if channel_text
                                    else None
                                )
                                event_id: str | None = None
                                if isinstance(payload, dict):
                                    event_id = await _stream_id_for_pubsub_event(
                                        redis,
                                        stream_key=stream_key,
                                        event_name=_COMPACTION_EVENT,
                                        envelope_event_id=_normalize_event_id(
                                            payload.get("event_id")
                                        ),
                                        payload=payload,
                                        channel=public_channel,
                                    )
                                    if isinstance(event_id, str) and event_id:
                                        payload = _payload_with_sse_id(
                                            payload, event_id
                                        )
                                        out = {
                                            "id": event_id,
                                            "event": _COMPACTION_EVENT,
                                            "data": json.dumps(
                                                payload, separators=(",", ":")
                                            ),
                                        }
                                if (
                                    event_id is not None
                                    and event_id in replayed_sse_ids
                                ):
                                    continue
                                if phase == "started":
                                    pending_compaction_started[compaction_conv_id] = (
                                        time.monotonic()
                                        + _COMPACTION_MERGE_WINDOW_SECONDS,
                                        out,
                                    )
                                else:
                                    if phase in {"progress", "completed"}:
                                        pending_compaction_started.pop(
                                            compaction_conv_id,
                                            None,
                                        )
                                    # 任何 upstream 数据都要刷新 idle 计时
                                    last_upstream = time.monotonic()
                                    yield out
                        continue

                    if isinstance(data, (bytes, bytearray)):
                        data = data.decode("utf-8", errors="replace")
                    if isinstance(data, str):
                        try:
                            parsed = json.loads(data)
                            ev_name = parsed.get("event", "message")
                            payload = parsed.get("data", parsed)
                        except Exception:
                            parsed = None
                            ev_name = "message"
                            payload = {"raw": data}
                        # GEN-P0-7: publisher 在 XADD 之后把 stream msg_id 写进 envelope.sse_id
                        # 再 PUBLISH。这里只透传 Redis Stream 形态的 id，避免浏览器
                        # 把 live/dlq 等不可回放 id 当成 Last-Event-ID。
                        event_id = _normalize_recoverable_sse_id(
                            parsed.get("sse_id") if isinstance(parsed, dict) else None
                        )
                        envelope_event_id = _normalize_event_id(
                            parsed.get("event_id") if isinstance(parsed, dict) else None
                        )
                        channel_text = _decode_pubsub_text(channel)
                        if event_id is None:
                            event_id = await _stream_id_for_pubsub_event(
                                redis,
                                stream_key=stream_key,
                                event_name=ev_name,
                                envelope_event_id=envelope_event_id,
                                payload=payload,
                                channel=channel_text,
                            )
                        if event_id is not None and event_id in replayed_sse_ids:
                            continue
                        # 同时把 msg_id 放进 payload 方便前端 JSON 级去重
                        if event_id is not None:
                            payload = _payload_with_sse_id(payload, event_id)
                        if envelope_event_id is not None:
                            if isinstance(payload, dict):
                                if "event_id" not in payload:
                                    payload = {**payload, "event_id": envelope_event_id}
                            else:
                                payload = {
                                    "data": payload,
                                    "event_id": envelope_event_id,
                                }
                        out = {
                            "event": ev_name,
                            "data": json.dumps(payload, separators=(",", ":")),
                        }
                        if event_id is not None:
                            out["id"] = event_id
                        # 真实的 upstream 业务事件，刷新 idle 计时
                        last_upstream = time.monotonic()
                        yield out

                # 注释级 keepalive，每 15s 一次（防 nginx idle 关闭、保活 TCP）
                now = time.monotonic()
                if now - last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
                    last_keepalive = now
                    if connection_slot_key is not None:
                        await _refresh_sse_connection_slot(redis, connection_slot_key)
                    yield {"event": "keepalive", "data": "{}"}

                # idle 心跳：60s 内 upstream 无任何数据时，发一个 JSON `idle` 事件，
                # 让前端能区分 “在线但空闲” 与 “在线且有业务流”，
                # 也方便观察 nginx buffering 异常（前端能收到 keepalive 但收不到 idle 不应发生）。
                if now - last_upstream >= _IDLE_HEARTBEAT_INTERVAL_SECONDS:
                    last_upstream = now
                    yield {
                        "event": "idle",
                        "data": json.dumps(
                            {"type": "idle", "ts": int(time.time())},
                            separators=(",", ":"),
                        ),
                    }
        except asyncio.CancelledError:
            # 不要 swallow：必须先走 finally 清理 pubsub，再把取消信号 reraise。
            raise
        finally:
            # Why: pubsub holds an underlying redis connection; ensure it is
            # released even on cancel/exception so we don't leak per-client.
            try:
                await pubsub.unsubscribe(*subscribed)
            except Exception:
                logger.warning("sse pubsub unsubscribe failed", exc_info=True)
            await pubsub.aclose()
            await release_slot_once()

    return EventSourceResponse(gen())
