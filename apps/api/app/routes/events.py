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
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from lumen_core.constants import EVENTS_STREAM_PREFIX
from lumen_core.models import Completion, Conversation, Generation

from ..db import get_db
from ..deps import CurrentUser
from ..redis_client import get_redis


router = APIRouter()
logger = logging.getLogger(__name__)

_COMPACTION_EVENT = "context.compaction"
_COMPACTION_CHANNEL_PREFIX = "lumen:events:conversation:"
_COMPACTION_MERGE_WINDOW_SECONDS = 0.2
MAX_SSE_CHANNELS = 64
# 15s 注释级 keepalive 让 nginx / 浏览器知道连接活；
# 60s 内若没有任何 upstream 数据再补一个 JSON `idle` 心跳，
# 让前端区分 “连接活但上游空闲” vs “上游真的有事件”。
_KEEPALIVE_INTERVAL_SECONDS = 15
_IDLE_HEARTBEAT_INTERVAL_SECONDS = 60


_LAST_EVENT_ID_MAX_AGE_MS = 24 * 60 * 60 * 1000  # 24h replay window cap


def _sanitize_last_event_id(raw: str | None) -> str | None:
    """Validate a client-provided ``Last-Event-ID`` against Redis Stream IDs.

    Why: the value is an attacker-controlled HTTP header. Forwarding a
    malformed or far-future ID to ``XREAD`` makes the read cursor skip the
    real backlog, silently dropping events for the legitimate user.
    """

    if raw is None:
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


async def _validate_channels(
    channels: list[str],
    user_id: str,
    db: AsyncSession,
) -> list[str]:
    """Ensure every requested channel is owned by this user. Silently drop unknown
    formats. Raise 403 if a known channel belongs to someone else."""
    clean: list[str] = []
    for ch in channels:
        ch = ch.strip()
        if not ch or ":" not in ch:
            continue
        prefix, _, ref = ch.partition(":")
        if prefix == "user":
            if ref != user_id:
                raise _http("forbidden_channel", f"cannot subscribe to user:{ref}", 403)
            clean.append(ch)
        elif prefix == "conv":
            row = (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.id == ref,
                        Conversation.user_id == user_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).first()
            if not row:
                raise _http("forbidden_channel", f"conv {ref} not owned", 403)
            clean.append(ch)
        elif prefix == "task":
            # task_id can be either a generation or completion; check both.
            gen_row = (
                await db.execute(
                    select(Generation.id).where(
                        Generation.id == ref, Generation.user_id == user_id
                    )
                )
            ).first()
            comp_row = None
            if not gen_row:
                comp_row = (
                    await db.execute(
                        select(Completion.id).where(
                            Completion.id == ref, Completion.user_id == user_id
                        )
                    )
                ).first()
            if not gen_row and not comp_row:
                raise _http("forbidden_channel", f"task {ref} not owned", 403)
            clean.append(ch)
        else:
            # unknown channel prefix — drop silently
            continue
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


@router.get("/events")
async def events(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    channels: str = Query(default=""),
) -> EventSourceResponse:
    client_requested = list(dict.fromkeys(c.strip() for c in channels.split(",") if c.strip()))
    requested = list(client_requested)
    # ensure the personal user channel is always included.
    if f"user:{user.id}" not in requested:
        requested.append(f"user:{user.id}")
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

    last_event_id = request.headers.get("Last-Event-ID")
    # Why: Last-Event-ID is attacker-controlled; an unsanitised value can
    # advance XREAD's cursor past the entire backlog so the client silently
    # misses real events. Require strict `ms-seq` shape and a sane age window.
    last_event_id = _sanitize_last_event_id(last_event_id)
    stream_key = f"{EVENTS_STREAM_PREFIX}{user.id}"
    redis = get_redis()

    async def gen() -> AsyncIterator[dict]:
        # 1) Replay from per-user stream since last_event_id (if provided).
        # GEN-P0-7: `last_event_id` 必须是之前 XREAD 返回的原生 stream ID
        # (`ms-seq`)，这样 XREAD 从它严格之后继续；绝不本地生成假 ID。
        if last_event_id:
            try:
                replay = await redis.xread({stream_key: last_event_id}, count=500, block=0)
                # xread returns [[stream_key, [(id, {field: val}), ...]]]
                for _stream, entries in replay or []:
                    for msg_id, fields in entries:
                        # Redis client 通常返回 bytes;先 normalize 成 str 供 SSE id 字段与 JSON 去重使用
                        if isinstance(msg_id, (bytes, bytearray)):
                            msg_id = msg_id.decode("ascii", errors="replace")
                        data = fields.get("data") if isinstance(fields, dict) else None
                        event_name = fields.get("event") if isinstance(fields, dict) else None
                        if isinstance(data, (bytes, bytearray)):
                            data = data.decode("utf-8", errors="replace")
                        if isinstance(event_name, (bytes, bytearray)):
                            event_name = event_name.decode("utf-8", errors="replace")
                        if not data:
                            continue
                        try:
                            parsed = json.loads(data)
                            ev_name = event_name or parsed.get("event") or "message"
                            payload = parsed.get("data", parsed)
                        except Exception:
                            ev_name = event_name or "message"
                            payload = {"raw": data}
                        # GEN-P0-7 (3): 让 payload 也带 msg_id，前端做 JSON 级去重
                        if isinstance(payload, dict):
                            payload = {**payload, "msg_id": msg_id}
                        yield {
                            "id": msg_id,
                            "event": ev_name,
                            "data": json.dumps(payload, separators=(",", ":")),
                        }
            except Exception:
                # Replay failures shouldn't break the live stream.
                logger.warning(
                    "sse replay failed user_id=%s stream_key=%s",
                    user.id,
                    stream_key,
                    exc_info=True,
                )

        # 2) Subscribe live.
        # 用 try/finally 包裹 pubsub 全生命周期：
        # - 客户端断开（is_disconnected）→ break 走 finally 清理；
        # - 协程被取消（CancelledError，例如 ASGI 关闭、上游超时）→ finally 清理后 reraise，
        #   绝不 swallow 取消信号，避免后台资源泄漏。
        pubsub = redis.pubsub()
        bridge_channels = _compaction_bridge_channels(valid)
        subscribed = [*valid, *bridge_channels.keys()]
        try:
            await pubsub.subscribe(*subscribed)
        except Exception:
            await pubsub.aclose()
            raise

        last_keepalive = time.monotonic()
        last_upstream = time.monotonic()
        pending_compaction_started: dict[str, tuple[float, dict]] = {}
        try:
            while True:
                if await request.is_disconnected():
                    # 客户端已断开（浏览器关闭 / nginx 切断），主动退出 generator；
                    # finally 会负责 unsubscribe + close。
                    break

                now = time.monotonic()
                expired_started = [
                    conv_id
                    for conv_id, (deadline, _event) in pending_compaction_started.items()
                    if deadline <= now
                ]
                for conv_id in expired_started:
                    _deadline, event = pending_compaction_started.pop(conv_id)
                    yield event

                timeout = 1.0
                if pending_compaction_started:
                    next_deadline = min(
                        deadline for deadline, _event in pending_compaction_started.values()
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
                        conv_id = _compaction_conv_id(channel)
                        if data_text and conv_id:
                            out = _format_compaction_sse(
                                data_text, expected_conv_id=conv_id
                            )
                            if out is not None:
                                try:
                                    payload = json.loads(out["data"])
                                    phase = payload.get("phase")
                                except Exception:
                                    phase = None
                                if phase == "started":
                                    pending_compaction_started[conv_id] = (
                                        time.monotonic()
                                        + _COMPACTION_MERGE_WINDOW_SECONDS,
                                        out,
                                    )
                                else:
                                    if phase in {"progress", "completed"}:
                                        pending_compaction_started.pop(conv_id, None)
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
                        # 再 PUBLISH——这里直接透传，绝不本地生成。重连时浏览器的
                        # Last-Event-ID 即为这个 id，下次 XREAD 严格 resume。
                        event_id = parsed.get("sse_id") if isinstance(parsed, dict) else None
                        # 同时把 msg_id 放进 payload 方便前端 JSON 级去重
                        if isinstance(payload, dict) and isinstance(event_id, str) and event_id:
                            payload = {**payload, "msg_id": event_id}
                        out = {
                            "event": ev_name,
                            "data": json.dumps(payload, separators=(",", ":")),
                        }
                        if isinstance(event_id, str) and event_id:
                            out["id"] = event_id
                        # 真实的 upstream 业务事件，刷新 idle 计时
                        last_upstream = time.monotonic()
                        yield out

                # 注释级 keepalive，每 15s 一次（防 nginx idle 关闭、保活 TCP）
                now = time.monotonic()
                if now - last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
                    last_keepalive = now
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

    return EventSourceResponse(gen())
