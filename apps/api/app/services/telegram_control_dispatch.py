"""Fenced PostgreSQL-to-Redis Stream publisher for Telegram commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from redis.exceptions import ResponseError
from sqlalchemy import or_, select, update

from lumen_core.model_entities.control_operations import TelegramControlCommand

from ..db import SessionLocal, affected_rows
from ..redis_client import get_redis


logger = logging.getLogger(__name__)

CONTROL_STREAM = "lumen:tgbot:control:v1"
CONTROL_GROUP = "lumen-tgbot-control"
_RUNTIME_STATE_KEY = "_telegram_control_runtime"
_PUBLISH_LEASE_SECONDS = 30
_RECONCILE_INTERVAL_SECONDS = 2.0
_RECONCILE_BATCH_SIZE = 10
_ERROR_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class TelegramCommandClaim:
    command_id: str
    command: str
    payload: dict[str, Any]
    owner: str
    fence: int


@dataclass(slots=True)
class TelegramControlRuntime:
    wakeup: asyncio.Event
    stop: asyncio.Event
    task: asyncio.Task[None] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def ensure_control_consumer_group(redis: Any) -> None:
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


async def _due_command_ids() -> list[str]:
    now = _now()
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(TelegramControlCommand.id)
                    .where(
                        TelegramControlCommand.status == "pending",
                        or_(
                            TelegramControlCommand.publish_lease_until.is_(None),
                            TelegramControlCommand.publish_lease_until <= now,
                        ),
                    )
                    .order_by(
                        TelegramControlCommand.created_at.asc(),
                        TelegramControlCommand.id.asc(),
                    )
                    .limit(_RECONCILE_BATCH_SIZE)
                )
            ).scalars()
        )


async def claim_telegram_command(
    command_id: str,
) -> TelegramCommandClaim | None:
    owner = f"api:{os.getpid()}:{uuid.uuid4().hex}"
    now = _now()
    async with SessionLocal() as session:
        row = (
            await session.execute(
                update(TelegramControlCommand)
                .where(
                    TelegramControlCommand.id == command_id,
                    TelegramControlCommand.status == "pending",
                    or_(
                        TelegramControlCommand.publish_lease_until.is_(None),
                        TelegramControlCommand.publish_lease_until <= now,
                    ),
                )
                .values(
                    publish_owner=owner,
                    publish_lease_until=now
                    + timedelta(seconds=_PUBLISH_LEASE_SECONDS),
                    publish_fence=TelegramControlCommand.publish_fence + 1,
                    publish_attempts=TelegramControlCommand.publish_attempts + 1,
                    last_error=None,
                )
                .returning(
                    TelegramControlCommand.command,
                    TelegramControlCommand.payload,
                    TelegramControlCommand.publish_fence,
                )
            )
        ).one_or_none()
        await session.commit()
    if row is None:
        return None
    return TelegramCommandClaim(
        command_id=command_id,
        command=str(row[0]),
        payload=dict(row[1] or {}),
        owner=owner,
        fence=int(row[2]),
    )


async def _record_publish_failure(
    claim: TelegramCommandClaim,
    error: str,
) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            update(TelegramControlCommand)
            .where(
                TelegramControlCommand.id == claim.command_id,
                TelegramControlCommand.status == "pending",
                TelegramControlCommand.publish_owner == claim.owner,
                TelegramControlCommand.publish_fence == claim.fence,
            )
            .values(
                publish_owner=None,
                publish_lease_until=None,
                last_error=error[:_ERROR_LIMIT],
            )
        )
        if affected_rows(result) != 1:
            await session.rollback()
            raise RuntimeError("telegram command publish fence was lost")
        await session.commit()


async def mark_telegram_command_published(
    claim: TelegramCommandClaim,
    *,
    stream_id: str,
) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            update(TelegramControlCommand)
            .where(
                TelegramControlCommand.id == claim.command_id,
                TelegramControlCommand.status == "pending",
                TelegramControlCommand.publish_owner == claim.owner,
                TelegramControlCommand.publish_fence == claim.fence,
            )
            .values(
                status="published",
                stream_id=stream_id,
                published_at=_now(),
                publish_owner=None,
                publish_lease_until=None,
                last_error=None,
            )
        )
        if affected_rows(result) != 1:
            await session.rollback()
            raise RuntimeError("telegram command publish fence was lost")
        await session.commit()


async def publish_telegram_command(
    command_id: str,
    *,
    redis: Any | None = None,
) -> bool:
    claim = await claim_telegram_command(command_id)
    if claim is None:
        return False
    client = redis or get_redis()
    try:
        await ensure_control_consumer_group(client)
        stream_id = await client.xadd(
            CONTROL_STREAM,
            {
                "command_id": claim.command_id,
                "command": claim.command,
                "payload": json.dumps(
                    claim.payload,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        )
        await mark_telegram_command_published(
            claim,
            stream_id=_decode(stream_id),
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await _record_publish_failure(
                claim,
                f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            logger.exception(
                "telegram command publish failure could not be persisted "
                "command_id=%s",
                command_id,
            )
        raise


async def run_telegram_control_reconciler_once() -> int:
    published = 0
    for command_id in await _due_command_ids():
        try:
            published += int(await publish_telegram_command(command_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "telegram command publish failed command_id=%s",
                command_id,
            )
    return published


async def telegram_control_reconciler_loop(
    runtime: TelegramControlRuntime,
    *,
    interval_seconds: float = _RECONCILE_INTERVAL_SECONDS,
) -> None:
    while not runtime.stop.is_set():
        runtime.wakeup.clear()
        try:
            await run_telegram_control_reconciler_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram control reconciliation iteration failed")
        if runtime.stop.is_set() or runtime.wakeup.is_set():
            continue
        try:
            await asyncio.wait_for(runtime.wakeup.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def create_telegram_control_lifespan() -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = TelegramControlRuntime(
            wakeup=asyncio.Event(),
            stop=asyncio.Event(),
        )
        runtime.task = asyncio.create_task(
            telegram_control_reconciler_loop(runtime),
            name="telegram-control-reconciler",
        )
        setattr(app.state, _RUNTIME_STATE_KEY, runtime)
        try:
            yield
        finally:
            runtime.stop.set()
            runtime.wakeup.set()
            if runtime.task is not None:
                runtime.task.cancel()
                await asyncio.gather(runtime.task, return_exceptions=True)
            if getattr(app.state, _RUNTIME_STATE_KEY, None) is runtime:
                delattr(app.state, _RUNTIME_STATE_KEY)

    return lifespan


def wake_telegram_control_publisher(request: Request) -> bool:
    runtime = getattr(request.app.state, _RUNTIME_STATE_KEY, None)
    if not isinstance(runtime, TelegramControlRuntime):
        logger.error(
            "telegram command persisted but publisher runtime is unavailable"
        )
        return False
    runtime.wakeup.set()
    return True


__all__ = [
    "CONTROL_GROUP",
    "CONTROL_STREAM",
    "TelegramCommandClaim",
    "claim_telegram_command",
    "create_telegram_control_lifespan",
    "ensure_control_consumer_group",
    "mark_telegram_command_published",
    "publish_telegram_command",
    "run_telegram_control_reconciler_once",
    "wake_telegram_control_publisher",
]
