"""Redis Stream replay helpers for the SSE events route."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator


logger = logging.getLogger(__name__)


def normalize_event_id(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    return str(raw)


def normalize_recoverable_sse_id(raw: object) -> str | None:
    value = normalize_event_id(raw)
    if value is None or len(value) > 64:
        return None
    parts = value.split("-")
    if len(parts) != 2:
        return None
    ms_str, seq_str = parts
    if not ms_str.isdigit() or not seq_str.isdigit():
        return None
    return value


def stream_id_parts(raw: object) -> tuple[int, int] | None:
    value = normalize_recoverable_sse_id(raw)
    if value is None:
        return None
    ms_str, seq_str = value.split("-", 1)
    return int(ms_str), int(seq_str)


async def stream_high_water_id(redis: object, *, stream_key: str) -> str | None:
    """Capture the latest stream ID after the PubSub subscription is active."""

    xrevrange = getattr(redis, "xrevrange", None)
    if not callable(xrevrange):
        raise RuntimeError("redis stream high-water read is unavailable")
    try:
        rows = await xrevrange(stream_key, count=1)
    except TypeError:
        rows = await xrevrange(stream_key, "+", "-", 1)
    if not rows:
        return None
    try:
        raw_id = rows[0][0]
    except (IndexError, KeyError, TypeError):
        raise RuntimeError("redis stream high-water response is invalid") from None
    if isinstance(raw_id, (bytes, bytearray)):
        raw_id = raw_id.decode("ascii", errors="replace")
    high_water_id = normalize_recoverable_sse_id(raw_id)
    if high_water_id is None:
        raise RuntimeError("redis stream high-water ID is invalid")
    return high_water_id


def task_ids_from_payload(payload: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("task_id", "generation_id", "completion_id", "video_generation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            ids.add(value)
    return ids


def event_channels_from_payload(payload: dict) -> set[str]:
    channels: set[str] = set()
    conv_id = payload.get("conversation_id")
    if isinstance(conv_id, str) and conv_id:
        channels.add(f"conv:{conv_id}")
    channels.update(f"task:{task_id}" for task_id in task_ids_from_payload(payload))
    for key in ("storyboard_id", "storyboard_run_id", "workflow_run_id", "run_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            channels.add(f"storyboard:{value}")
    return channels


def replay_payload_matches_channels(
    payload: object,
    *,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
    envelope_channel: str | None = None,
) -> bool:
    if not requested_channels:
        return True
    if envelope_channel:
        if envelope_channel in requested_channels:
            return True
        if envelope_channel == user_channel:
            return include_user_channel and user_channel in requested_channels
        if envelope_channel.startswith(("conv:", "task:", "storyboard:")):
            return False
    if not isinstance(payload, dict):
        return include_user_channel and user_channel in requested_channels

    event_channels = event_channels_from_payload(payload)
    if event_channels:
        return bool(event_channels & requested_channels)
    return include_user_channel and user_channel in requested_channels


def _replay_field(fields: dict, name: str) -> object | None:
    value = fields.get(name)
    if value is None:
        value = fields.get(name.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def decode_replay_fields(fields: object) -> tuple[str, object, str | None] | None:
    if not isinstance(fields, dict):
        return None
    data = _replay_field(fields, "data")
    event_name = _replay_field(fields, "event")
    event_name = event_name if isinstance(event_name, str) else None
    if not isinstance(data, str) or not data:
        return None
    try:
        parsed = json.loads(data)
    except Exception:
        return event_name or "message", {"raw": data}, None
    if not isinstance(parsed, dict):
        return event_name or "message", parsed, None

    resolved_event_name = event_name or parsed.get("event") or "message"
    if not isinstance(resolved_event_name, str) or not resolved_event_name:
        resolved_event_name = "message"
    envelope_channel = parsed.get("channel")
    envelope_channel = envelope_channel if isinstance(envelope_channel, str) else None
    payload = parsed.get("data", parsed)
    envelope_event_id = normalize_event_id(parsed.get("event_id"))
    if isinstance(payload, dict) and envelope_event_id is not None:
        payload = {**payload}
        payload.setdefault("event_id", envelope_event_id)
    return resolved_event_name, payload, envelope_channel


def payload_with_sse_id(payload: object, sse_id: str) -> dict:
    if isinstance(payload, dict):
        return {**payload, "msg_id": sse_id, "sse_id": sse_id}
    return {"data": payload, "msg_id": sse_id, "sse_id": sse_id}


@dataclass(frozen=True)
class _ReplayEntry:
    msg_id: str
    event: dict[str, Any] | None
    beyond_limit: bool = False


def _message_id(raw: object) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("ascii", errors="replace")
    return str(raw)


def _validated_replay_limit(replay_until_id: str | None) -> tuple[int, int] | None:
    if replay_until_id is None:
        return None
    replay_limit = stream_id_parts(replay_until_id)
    if replay_limit is None:
        raise ValueError(f"invalid replay high-water ID: {replay_until_id}")
    return replay_limit


def _prepare_replay_entry(
    msg_id_raw: object,
    fields: object,
    *,
    replay_limit: tuple[int, int] | None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
) -> _ReplayEntry:
    msg_id = _message_id(msg_id_raw)
    if replay_limit is not None:
        msg_id_parts = stream_id_parts(msg_id)
        if msg_id_parts is None:
            raise ValueError(f"invalid replay stream ID: {msg_id}")
        if msg_id_parts > replay_limit:
            return _ReplayEntry(msg_id=msg_id, event=None, beyond_limit=True)

    decoded = decode_replay_fields(fields)
    if decoded is None:
        return _ReplayEntry(msg_id=msg_id, event=None)
    event_name, payload, envelope_channel = decoded
    if not replay_payload_matches_channels(
        payload,
        requested_channels=requested_channels,
        include_user_channel=include_user_channel,
        user_channel=user_channel,
        envelope_channel=envelope_channel,
    ):
        return _ReplayEntry(msg_id=msg_id, event=None)
    payload = payload_with_sse_id(payload, msg_id)
    return _ReplayEntry(
        msg_id=msg_id,
        event={
            "id": msg_id,
            "event": event_name,
            "data": json.dumps(payload, separators=(",", ":")),
        },
    )


async def _read_replay_entries(
    redis: object,
    *,
    stream_key: str,
    cursor: str,
    batch_size: int,
) -> list[tuple[object, object]]:
    replay = await redis.xread(  # type: ignore[attr-defined]
        {stream_key: cursor},
        count=batch_size,
    )
    entries: list[tuple[object, object]] = []
    for _stream, batch in replay or []:
        entries.extend(batch or [])
    return entries


async def iter_replay_events(
    redis: object,
    *,
    stream_key: str,
    last_event_id: str,
    replay_until_id: str | None,
    requested_channels: set[str],
    include_user_channel: bool,
    user_channel: str,
    batch_size: int,
    max_events: int,
) -> AsyncIterator[dict]:
    cursor = last_event_id
    scanned = 0
    replay_limit = _validated_replay_limit(replay_until_id)

    while cursor and scanned < max_events:
        entries = await _read_replay_entries(
            redis,
            stream_key=stream_key,
            cursor=cursor,
            batch_size=batch_size,
        )
        if not entries:
            break

        reached_replay_limit = False
        for msg_id_raw, fields in entries:
            entry = _prepare_replay_entry(
                msg_id_raw,
                fields,
                replay_limit=replay_limit,
                requested_channels=requested_channels,
                include_user_channel=include_user_channel,
                user_channel=user_channel,
            )
            if entry.beyond_limit:
                reached_replay_limit = True
                break
            cursor = entry.msg_id
            scanned += 1
            if entry.event is not None:
                yield entry.event
            if scanned >= max_events:
                break

        if reached_replay_limit or len(entries) < batch_size:
            break

    if scanned >= max_events:
        logger.warning(
            "SSE replay capped stream=%s last_event_id=%s scanned=%s",
            stream_key,
            last_event_id,
            scanned,
        )
        data: dict[str, Any] = {"reason": "too_many_events", "limit": max_events}
        if cursor:
            data["cursor"] = cursor
        yield {
            "id": cursor,
            "event": "replay_truncated",
            "data": json.dumps(data, separators=(",", ":")),
        }


__all__ = [
    "decode_replay_fields",
    "event_channels_from_payload",
    "iter_replay_events",
    "normalize_event_id",
    "normalize_recoverable_sse_id",
    "payload_with_sse_id",
    "replay_payload_matches_channels",
    "stream_high_water_id",
    "stream_id_parts",
    "task_ids_from_payload",
]
