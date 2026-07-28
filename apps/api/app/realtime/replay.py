from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from lumen_core.constants import (
    EVENTS_REPLAY_MAX_SCAN,
    EVENTS_STREAM_MAXLEN,
)

from ..services import event_replay as _event_replay


logger = logging.getLogger("app.routes.events")

REPLAY_BATCH_SIZE = 500
REPLAY_MAX_EVENTS = EVENTS_REPLAY_MAX_SCAN
CONNECTION_DEDUPE_MAX_KEYS = 4096
LAST_EVENT_ID_MAX_AGE_MS = 24 * 60 * 60 * 1000

decode_replay_fields = _event_replay.decode_replay_fields
event_channels_from_payload = _event_replay.event_channels_from_payload
normalize_event_id = _event_replay.normalize_event_id
normalize_recoverable_sse_id = _event_replay.normalize_recoverable_sse_id
payload_with_sse_id = _event_replay.payload_with_sse_id
replay_payload_matches_channels = _event_replay.replay_payload_matches_channels
stream_high_water_id = _event_replay.stream_high_water_id
stream_id_parts = _event_replay.stream_id_parts
task_ids_from_payload = _event_replay.task_ids_from_payload


async def redis_time_ms(redis: object) -> int | None:
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


def sanitize_last_event_id(raw: Any, *, now_ms: int | None = None) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 64:
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
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if ms > now_ms + 60_000:
        return None
    if now_ms - ms > LAST_EVENT_ID_MAX_AGE_MS:
        return None
    return raw


def is_stream_command_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        or "unknown redis command" in message
        or (
            "xadd" in message and ("unsupported" in message or "not allowed" in message)
        )
    )


async def stream_id_for_pubsub_event(
    redis: object,
    *,
    stream_key: str,
    event_name: str,
    envelope_event_id: str | None,
    payload: object,
    channel: str | None = None,
) -> str | None:
    event_id = normalize_event_id(envelope_event_id) or str(uuid.uuid4())
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
        if is_stream_command_unsupported(exc):
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


async def iter_replay_events(
    redis: object,
    *,
    stream_key: str,
    last_event_id: str,
    replay_until_id: str | None = None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
    batch_size: int = REPLAY_BATCH_SIZE,
    max_events: int = REPLAY_MAX_EVENTS,
) -> AsyncIterator[dict]:
    async for event in _event_replay.iter_replay_events(
        redis,
        stream_key=stream_key,
        last_event_id=last_event_id,
        replay_until_id=replay_until_id,
        requested_channels=requested_channels,
        include_user_channel=include_user_channel,
        user_channel=user_channel,
        batch_size=batch_size,
        max_events=max_events,
    ):
        yield event


@dataclass
class ConnectionEventDeduper:
    max_keys: int = CONNECTION_DEDUPE_MAX_KEYS
    order: deque[str] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)

    def remember(
        self,
        *,
        sse_id: object = None,
        event_id: object = None,
    ) -> bool:
        keys: list[str] = []
        normalized_sse_id = normalize_recoverable_sse_id(sse_id)
        if normalized_sse_id is not None:
            keys.append(f"sse:{normalized_sse_id}")
        normalized_event_id = normalize_event_id(event_id)
        if normalized_event_id is not None and len(normalized_event_id) <= 256:
            keys.append(f"event:{normalized_event_id}")
        if not keys:
            return True
        if any(key in self.seen for key in keys):
            return False
        for key in dict.fromkeys(keys):
            self.seen.add(key)
            self.order.append(key)
        while len(self.order) > self.max_keys:
            self.seen.discard(self.order.popleft())
        return True


def event_payload_ids(event: dict) -> tuple[object, object]:
    try:
        payload = json.loads(event.get("data", ""))
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return event.get("id"), None
    return (
        event.get("id") or payload.get("sse_id"),
        payload.get("event_id"),
    )


def remember_replayed_event(
    event: dict,
    event_deduper: ConnectionEventDeduper,
) -> bool:
    if event.get("event") == "replay_truncated":
        return True
    sse_id, event_id = event_payload_ids(event)
    return event_deduper.remember(sse_id=sse_id, event_id=event_id)


async def replay_connection_events(
    redis: object,
    *,
    stream_key: str,
    last_event_id: str | None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
    user_id: str,
    event_deduper: ConnectionEventDeduper,
    iter_events: Any = iter_replay_events,
) -> AsyncIterator[dict]:
    if last_event_id is None:
        return
    try:
        replay_until_id = await stream_high_water_id(redis, stream_key=stream_key)
        if replay_until_id is None:
            return
        async for event in iter_events(
            redis,
            stream_key=stream_key,
            last_event_id=last_event_id,
            replay_until_id=replay_until_id,
            requested_channels=requested_channels,
            include_user_channel=include_user_channel,
            user_channel=user_channel,
        ):
            if remember_replayed_event(event, event_deduper):
                yield event
    except Exception:
        logger.warning(
            "sse replay failed user_id=%s stream_key=%s",
            user_id,
            stream_key,
            exc_info=True,
        )
        yield {
            "event": "recovery_required",
            "data": json.dumps(
                {
                    "reason": "replay_unavailable",
                    "message": "event replay failed; fetch a fresh snapshot before reconnecting",
                },
                separators=(",", ":"),
            ),
        }
