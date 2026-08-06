"""Durable Redis Stream consumer for Telegram control commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from redis import asyncio as aioredis
from redis.exceptions import ResponseError

from .api_client import LumenApi
from .config import settings
from .listener import redrive_quarantined_event


logger = logging.getLogger("lumen-tgbot.control")

CONTROL_STREAM = "lumen:tgbot:control:v1"
CONTROL_GROUP = "lumen-tgbot-control"
_CLAIM_IDLE_MS = 5_000
_READ_BLOCK_MS = 5_000
_BATCH_SIZE = 10
_BACKOFF_MAX_SECONDS = 60.0
_ALERT_THRESHOLD = 50


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decoded_fields(fields: Any) -> dict[str, str]:
    if not isinstance(fields, dict):
        return {}
    return {_decode(key): _decode(value) for key, value in fields.items()}


def _claimed_entries(result: Any) -> list[tuple[Any, Any]]:
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        return []
    entries = result[1]
    return list(entries) if isinstance(entries, (list, tuple)) else []


def _new_entries(result: Any) -> list[tuple[Any, Any]]:
    if not isinstance(result, (list, tuple)):
        return []
    entries: list[tuple[Any, Any]] = []
    for batch in result:
        if not isinstance(batch, (list, tuple)) or len(batch) < 2:
            continue
        batch_entries = batch[1]
        if isinstance(batch_entries, (list, tuple)):
            entries.extend(batch_entries)
    return entries


async def ensure_control_group(redis: Any) -> None:
    try:
        await redis.xgroup_create(
            CONTROL_STREAM,
            CONTROL_GROUP,
            id="0-0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _quarantine_invalid_entry(
    api: LumenApi,
    *,
    stream_id: str,
    fields: dict[str, str],
    reason: str,
) -> None:
    await api.persist_delivery_quarantine(
        source_stream=CONTROL_STREAM,
        source_id=stream_id,
        stream_user_id="control",
        event="telegram.control.invalid",
        generation_id="",
        payload_raw=json.dumps(fields, separators=(",", ":"), sort_keys=True),
        reason=reason,
        attempts=1,
    )


async def _ack_and_xack(
    api: LumenApi,
    redis: Any,
    *,
    stream_id: str,
    command_id: str,
    command: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    acknowledgement = await api.ack_control_command(
        command_id,
        command=command,
        status=status,
        error=error,
    )
    await redis.xack(CONTROL_STREAM, CONTROL_GROUP, stream_id)
    return acknowledgement


async def process_control_entry(
    redis: Any,
    api: LumenApi,
    stop_event: asyncio.Event,
    *,
    stream_id_raw: Any,
    fields_raw: Any,
    bot: Bot | None,
) -> bool:
    """Process one entry, returning True when a restart was newly accepted."""

    stream_id = _decode(stream_id_raw)
    fields = _decoded_fields(fields_raw)
    command_id = fields.get("command_id", "").strip()
    command = fields.get("command", "").strip()
    payload_raw = fields.get("payload", "{}")
    try:
        payload = json.loads(payload_raw)
    except (TypeError, ValueError):
        payload = None

    if not command_id or not command or not isinstance(payload, dict):
        await _quarantine_invalid_entry(
            api,
            stream_id=stream_id,
            fields=fields,
            reason="malformed Telegram control transport entry",
        )
        await redis.xack(CONTROL_STREAM, CONTROL_GROUP, stream_id)
        return False

    if command == "restart":
        acknowledgement = await _ack_and_xack(
            api,
            redis,
            stream_id=stream_id,
            command_id=command_id,
            command=command,
            status="accepted",
        )
        if bool(acknowledgement.get("newly_accepted")):
            logger.info("control: restart accepted command_id=%s", command_id)
            stop_event.set()
            return True
        return False

    if command == "redrive_quarantine":
        try:
            if bot is None:
                raise RuntimeError("Telegram bot is unavailable for quarantine redrive")
            await redrive_quarantined_event(
                bot,
                api,
                payload_raw=str(payload.get("payload_raw") or ""),
                stream_user_id=str(payload.get("stream_user_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            await _ack_and_xack(
                api,
                redis,
                stream_id=stream_id,
                command_id=command_id,
                command=command,
                status="failed",
                error=error,
            )
            logger.error(
                "control: quarantine redrive failed command_id=%s error=%s",
                command_id,
                error,
            )
            return False
        await _ack_and_xack(
            api,
            redis,
            stream_id=stream_id,
            command_id=command_id,
            command=command,
            status="accepted",
        )
        logger.info("control: quarantine redrive accepted command_id=%s", command_id)
        return False

    try:
        await _ack_and_xack(
            api,
            redis,
            stream_id=stream_id,
            command_id=command_id,
            command=command,
            status="failed",
            error=f"unsupported control command: {command}",
        )
    except Exception:
        await _quarantine_invalid_entry(
            api,
            stream_id=stream_id,
            fields=fields,
            reason=f"unsupported Telegram control command: {command}",
        )
        await redis.xack(CONTROL_STREAM, CONTROL_GROUP, stream_id)
    return False


async def _read_control_entries(
    redis: Any,
    *,
    consumer: str,
) -> list[tuple[Any, Any]]:
    claimed = await redis.xautoclaim(
        CONTROL_STREAM,
        CONTROL_GROUP,
        consumer,
        min_idle_time=_CLAIM_IDLE_MS,
        start_id="0-0",
        count=_BATCH_SIZE,
    )
    entries = _claimed_entries(claimed)
    if entries:
        return entries
    batches = await redis.xreadgroup(
        CONTROL_GROUP,
        consumer,
        streams={CONTROL_STREAM: ">"},
        count=_BATCH_SIZE,
        block=_READ_BLOCK_MS,
    )
    return _new_entries(batches)


async def run_control_listener(
    stop_event: asyncio.Event,
    *,
    api: LumenApi | None = None,
    bot: Bot | None = None,
    sleep_or_stop: Callable[[asyncio.Event, float], Awaitable[bool]],
    redis_factory: Callable[..., Any] = aioredis.from_url,
) -> None:
    """Reclaim pending commands before reading new entries and ack DB before XACK."""

    owned_api = api is None
    api_client = api or LumenApi()
    consumer = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    backoff = 1.0
    consecutive_failures = 0
    try:
        while not stop_event.is_set():
            client = None
            try:
                client = redis_factory(settings.redis_url, decode_responses=False)
                await ensure_control_group(client)
                logger.info(
                    "control: consuming stream=%s group=%s consumer=%s",
                    CONTROL_STREAM,
                    CONTROL_GROUP,
                    consumer,
                )
                backoff = 1.0
                consecutive_failures = 0
                while not stop_event.is_set():
                    entries = await _read_control_entries(
                        client,
                        consumer=consumer,
                    )
                    for stream_id, fields in entries:
                        if await process_control_entry(
                            client,
                            api_client,
                            stop_event,
                            stream_id_raw=stream_id,
                            fields_raw=fields,
                            bot=bot,
                        ):
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                level = (
                    logging.ERROR
                    if consecutive_failures >= _ALERT_THRESHOLD
                    else logging.WARNING
                )
                logger.log(
                    level,
                    "control listener error: %s; reconnect in %.1fs (failures=%d)",
                    exc,
                    backoff,
                    consecutive_failures,
                )
                if await sleep_or_stop(stop_event, backoff):
                    return
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        if owned_api:
            await api_client.aclose()


__all__ = [
    "CONTROL_GROUP",
    "CONTROL_STREAM",
    "ensure_control_group",
    "process_control_entry",
    "run_control_listener",
]
