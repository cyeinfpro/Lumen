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

from lumen_core.model_entities.control_operations import (
    TELEGRAM_CONTROL_EFFECT_PROTOCOL_VERSION,
    TELEGRAM_CONTROL_RESTART_INTENT_KEY,
)

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
_CONTROL_STREAM_MAXLEN = 10_000
_TERMINAL_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60


class ControlEffectLeaseLost(RuntimeError):
    pass


class ControlRuntimeNotReady(RuntimeError):
    pass


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
    await redis.expire(
        f"{CONTROL_STREAM}:command:{command_id}",
        _TERMINAL_DEDUP_TTL_SECONDS,
    )
    await redis.xack(CONTROL_STREAM, CONTROL_GROUP, stream_id)
    await redis.xdel(CONTROL_STREAM, stream_id)
    await _trim_control_stream(redis)
    return acknowledgement


async def _xack_and_xdel(redis: Any, stream_id: str) -> None:
    await redis.xack(CONTROL_STREAM, CONTROL_GROUP, stream_id)
    await redis.xdel(CONTROL_STREAM, stream_id)
    await _trim_control_stream(redis)


async def _trim_control_stream(redis: Any) -> None:
    try:
        await redis.xtrim(
            CONTROL_STREAM,
            maxlen=_CONTROL_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("control stream trim failed", exc_info=True)


async def _effect_heartbeat(
    api: LumenApi,
    *,
    command_id: str,
    command: str,
    owner: str,
    fence: int,
    lease_seconds: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    interval = max(1.0, lease_seconds / 3)
    while True:
        await sleep(interval)
        renewal = await api.renew_control_effect(
            command_id,
            command=command,
            owner=owner,
            fence=fence,
        )
        if (
            renewal.get("renewed") is not True
            or int(renewal.get("fence") or 0) != fence
        ):
            raise ControlEffectLeaseLost(
                f"control effect lease was lost command_id={command_id}"
            )
        lease_seconds = int(renewal.get("lease_seconds") or lease_seconds)
        interval = max(1.0, lease_seconds / 3)


async def _run_with_effect_heartbeat(
    api: LumenApi,
    operation: Awaitable[None],
    *,
    command_id: str,
    command: str,
    owner: str,
    fence: int,
    lease_seconds: int,
) -> None:
    effect_task = asyncio.create_task(
        operation,
        name=f"telegram-control-effect:{command_id}",
    )
    heartbeat_task = asyncio.create_task(
        _effect_heartbeat(
            api,
            command_id=command_id,
            command=command,
            owner=owner,
            fence=fence,
            lease_seconds=lease_seconds,
        ),
        name=f"telegram-control-heartbeat:{command_id}",
    )
    try:
        done, _pending = await asyncio.wait(
            {effect_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            effect_task.cancel()
            await asyncio.gather(effect_task, return_exceptions=True)
            if heartbeat_error is not None:
                raise heartbeat_error
            raise ControlEffectLeaseLost(
                f"control effect heartbeat stopped command_id={command_id}"
            )
        await effect_task
    finally:
        heartbeat_task.cancel()
        if not effect_task.done():
            effect_task.cancel()
        await asyncio.gather(
            heartbeat_task,
            effect_task,
            return_exceptions=True,
        )


async def _wait_for_restart_readiness(
    ready_event: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    if ready_event.is_set():
        return
    ready_wait = asyncio.create_task(
        ready_event.wait(),
        name="telegram-control-restart-ready",
    )
    stop_wait = asyncio.create_task(
        stop_event.wait(),
        name="telegram-control-restart-stop",
    )
    try:
        done, _pending = await asyncio.wait(
            {ready_wait, stop_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_wait in done:
            raise ControlRuntimeNotReady(
                "process stopped before Telegram polling became ready"
            )
        await ready_wait
    finally:
        for task in (ready_wait, stop_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(ready_wait, stop_wait, return_exceptions=True)


def new_control_generation() -> str:
    host = socket.gethostname().strip()[:32] or "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:16]}"


async def process_control_entry(
    redis: Any,
    api: LumenApi,
    stop_event: asyncio.Event,
    *,
    stream_id_raw: Any,
    fields_raw: Any,
    bot: Bot | None,
    generation: str | None = None,
    restart_ready: asyncio.Event | None = None,
) -> bool:
    """Process one entry, returning True when this generation must stop."""

    stream_id = _decode(stream_id_raw)
    process_generation = generation or new_control_generation()
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
        await _xack_and_xdel(redis, stream_id)
        return False

    owner = f"{process_generation}:{uuid.uuid4().hex[:12]}"
    claim = await api.claim_control_effect(
        command_id,
        command=command,
        owner=owner,
    )
    effect_status = str(claim.get("status") or "pending")
    acquired = bool(claim.get("acquired"))
    effect_fence = int(claim.get("fence") or 0)
    lease_seconds = int(claim.get("lease_seconds") or 0)
    effect_payload = claim.get("payload")
    if not isinstance(effect_payload, dict):
        raise RuntimeError("control effect payload is invalid")
    if not acquired:
        if effect_status == "running":
            raise RuntimeError("control effect is owned by another live consumer")
        if effect_status == "succeeded":
            await _ack_and_xack(
                api,
                redis,
                stream_id=stream_id,
                command_id=command_id,
                command=command,
                status="accepted",
            )
            return False
        if effect_status == "failed":
            await _ack_and_xack(
                api,
                redis,
                stream_id=stream_id,
                command_id=command_id,
                command=command,
                status="failed",
                error="control effect previously failed",
            )
            return False
        raise RuntimeError("control effect claim response is invalid")
    if effect_fence < 1 or lease_seconds < 3:
        raise RuntimeError("control effect claim is missing its fence or lease")

    if command == "restart":
        raw_intent = effect_payload.get(TELEGRAM_CONTROL_RESTART_INTENT_KEY)
        requested_generation = (
            str(raw_intent.get("requested_generation") or "")
            if isinstance(raw_intent, dict)
            else ""
        )
        if requested_generation and requested_generation != process_generation:
            if restart_ready is None:
                raise ControlRuntimeNotReady(
                    "restart completion requires an explicit runtime readiness gate"
                )
            await _run_with_effect_heartbeat(
                api,
                _wait_for_restart_readiness(restart_ready, stop_event),
                command_id=command_id,
                command=command,
                owner=owner,
                fence=effect_fence,
                lease_seconds=lease_seconds,
            )
            await api.finish_control_effect(
                command_id,
                command=command,
                owner=owner,
                fence=effect_fence,
                status="succeeded",
                generation=process_generation,
            )
            await _ack_and_xack(
                api,
                redis,
                stream_id=stream_id,
                command_id=command_id,
                command=command,
                status="accepted",
            )
            logger.info(
                "control: restart completed by new generation command_id=%s "
                "generation=%s",
                command_id,
                process_generation,
            )
            return False
        await api.commit_control_restart_intent(
            command_id,
            owner=owner,
            fence=effect_fence,
            generation=process_generation,
        )
        logger.info(
            "control: restart stop intent committed command_id=%s generation=%s",
            command_id,
            process_generation,
        )
        stop_event.set()
        return True

    if command == "redrive_quarantine":
        if bot is None:
            error = "RuntimeError: Telegram bot is unavailable for quarantine redrive"
            await api.finish_control_effect(
                command_id,
                command=command,
                owner=owner,
                fence=effect_fence,
                status="failed",
                error=error,
            )
            await _ack_and_xack(
                api,
                redis,
                stream_id=stream_id,
                command_id=command_id,
                command=command,
                status="failed",
                error=error,
            )
            return False
        try:
            payload_raw_value = str(effect_payload.get("payload_raw") or "")
            stream_user_id = str(effect_payload.get("stream_user_id") or "")
            preparation = await api.prepare_control_redrive_effect(
                command_id,
                owner=owner,
                fence=effect_fence,
            )
            action = str(preparation.get("action") or "")
            if action == "outcome_unknown":
                logger.warning(
                    "control: quarantine redrive remains pending reconciliation "
                    "command_id=%s",
                    command_id,
                )
                return False
            if action == "execute":
                await _run_with_effect_heartbeat(
                    api,
                    redrive_quarantined_event(
                        bot,
                        api,
                        payload_raw=payload_raw_value,
                        stream_user_id=stream_user_id,
                    ),
                    command_id=command_id,
                    command=command,
                    owner=owner,
                    fence=effect_fence,
                    lease_seconds=lease_seconds,
                )
            elif action != "already_succeeded":
                raise RuntimeError("control effect preparation response is invalid")
            await api.finish_control_effect(
                command_id,
                command=command,
                owner=owner,
                fence=effect_fence,
                status="succeeded",
            )
        except ControlEffectLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            await api.finish_control_effect(
                command_id,
                command=command,
                owner=owner,
                fence=effect_fence,
                status="outcome_unknown",
                error=error,
            )
            logger.warning(
                "control: quarantine redrive outcome unknown; transport retained "
                "command_id=%s error=%s",
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
        logger.info(
            "control: quarantine redrive effect committed command_id=%s",
            command_id,
        )
        return False

    error = f"unsupported control command: {command}"
    await api.finish_control_effect(
        command_id,
        command=command,
        owner=owner,
        fence=effect_fence,
        status="failed",
        error=error,
    )
    await _ack_and_xack(
        api,
        redis,
        stream_id=stream_id,
        command_id=command_id,
        command=command,
        status="failed",
        error=error,
    )
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
    generation: str | None = None,
    restart_ready: asyncio.Event | None = None,
    sleep_or_stop: Callable[[asyncio.Event, float], Awaitable[bool]],
    redis_factory: Callable[..., Any] = aioredis.from_url,
) -> None:
    """Reclaim pending commands before reading new entries and ack DB before XACK."""

    owned_api = api is None
    api_client = api or LumenApi()
    process_generation = generation or new_control_generation()
    consumer = f"{process_generation}:stream"
    backoff = 1.0
    consecutive_failures = 0
    try:
        capabilities = await api_client.control_capabilities()
        protocol_version = int(capabilities.get("effect_protocol_version") or 0)
        if protocol_version != TELEGRAM_CONTROL_EFFECT_PROTOCOL_VERSION:
            raise RuntimeError(
                "incompatible Telegram control effect protocol: "
                f"expected={TELEGRAM_CONTROL_EFFECT_PROTOCOL_VERSION} "
                f"actual={protocol_version}"
            )
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
                            generation=process_generation,
                            restart_ready=restart_ready,
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
    "ControlRuntimeNotReady",
    "ensure_control_group",
    "new_control_generation",
    "process_control_entry",
    "run_control_listener",
]
