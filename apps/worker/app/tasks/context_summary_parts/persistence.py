from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select, text as sa_text

from lumen_core.context_window import compare_message_position, is_summary_usable
from lumen_core.models import Conversation

from .common import SummaryLock


async def acquire_summary_lock(
    _session: Any,
    redis: Any,
    conv_id: str,
    *,
    engine: Any,
    ttl_s: int,
    lock_factory: Callable[..., SummaryLock],
    logger: logging.Logger,
) -> SummaryLock | None:
    import uuid

    token = uuid.uuid4().hex
    key = f"context:summary:lock:{conv_id}"
    if redis is not None:
        try:
            got_lock = await redis.set(key, token, nx=True, ex=ttl_s)
            if got_lock:
                return lock_factory("redis", token)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "context_summary.redis_lock_failed conv=%s err=%s",
                conv_id,
                exc,
            )
            # Do not consume an application-pool connection while Redis is down.
            return None

    connection = None
    try:
        connection = await engine.connect()
        result = await connection.execute(
            sa_text("select pg_try_advisory_lock(hashtext(:key))"),
            {"key": key},
        )
        await connection.commit()
        if bool(result.scalar_one_or_none()):
            return lock_factory("pg", pg_connection=connection, pg_key=key)
        await connection.close()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_summary.pg_lock_failed conv=%s err=%s", conv_id, exc)
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                pass
        return None


async def release_summary_lock(
    redis: Any,
    conv_id: str,
    lock: SummaryLock | None,
    *,
    release_script: str,
    logger: logging.Logger,
) -> None:
    if lock is None:
        return
    if lock.kind == "pg" and lock.pg_connection is not None and lock.pg_key:
        connection = lock.pg_connection
        try:
            await connection.execute(
                sa_text("select pg_advisory_unlock(hashtext(:key))"),
                {"key": lock.pg_key},
            )
            await connection.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "context_summary.pg_unlock_failed conv=%s err=%s",
                conv_id,
                exc,
            )
            try:
                await connection.invalidate()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                pass
        return
    if redis is None or lock.kind != "redis" or lock.token is None:
        return
    try:
        await redis.eval(
            release_script,
            1,
            f"context:summary:lock:{conv_id}",
            lock.token,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.redis_unlock_failed conv=%s err=%r", conv_id, exc)


async def release_business_transaction(session: Any) -> None:
    """Return the application DB connection before an upstream network wait."""
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()


async def renew_summary_lock_loop(
    redis: Any,
    conv_id: str,
    lock: SummaryLock,
    *,
    interval_s: float,
    ttl_s: int,
    renew_script: str,
    logger: logging.Logger,
) -> None:
    if redis is None or lock.kind != "redis" or lock.token is None:
        return
    key = f"context:summary:lock:{conv_id}"
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                renewed = await redis.eval(
                    renew_script,
                    1,
                    key,
                    lock.token,
                    str(ttl_s),
                )
                if int(renewed or 0) == 1:
                    continue
                value = await redis.get(key)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                lock.lost_reason = "expired" if value is None else "stolen"
                logger.warning(
                    "context_summary.lock_renew_lost conv=%s holder=%s reason=%s",
                    conv_id,
                    value,
                    lock.lost_reason,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "context_summary.lock_renew_failed conv=%s err=%s",
                    conv_id,
                    exc,
                )
    except asyncio.CancelledError:
        raise


async def read_current_summary(
    session: Any,
    conv_id: str,
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    try:
        row = await session.get(Conversation, conv_id, populate_existing=True)
        if row is None:
            return None
        summary = row.summary_jsonb
        return summary if isinstance(summary, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "context_summary.read_current_failed conv=%s err=%s",
            conv_id,
            exc,
        )
        return None


async def cas_write_summary(
    session: Any,
    conv_id: str,
    summary: dict[str, Any],
    *,
    lock: SummaryLock | None,
    allow_equal_boundary_refresh: bool,
    current_summary_wins_equal_boundary: Callable[..., bool],
    logger: logging.Logger,
) -> bool:
    """Serialize writes with a row lock and refuse to overwrite newer coverage."""
    if lock is not None and lock.lost_reason:
        logger.warning(
            "context_summary.cas_write_skipped_lock_lost conv=%s reason=%s",
            conv_id,
            lock.lost_reason,
        )
        return False
    try:
        result = await session.execute(
            select(Conversation)
            .where(Conversation.id == conv_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = result.scalar_one_or_none()
        if current is None:
            return False
        if lock is not None and lock.lost_reason:
            logger.warning(
                "context_summary.cas_write_aborted_lock_lost conv=%s reason=%s",
                conv_id,
                lock.lost_reason,
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False

        current_summary = (
            current.summary_jsonb if isinstance(current.summary_jsonb, dict) else None
        )
        if _newer_summary_wins(
            current_summary,
            summary,
            allow_equal_boundary_refresh=allow_equal_boundary_refresh,
            current_summary_wins_equal_boundary=current_summary_wins_equal_boundary,
        ):
            return False
        current.summary_jsonb = summary
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_summary.cas_write_failed conv=%s err=%s", conv_id, exc)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _newer_summary_wins(
    current_summary: dict[str, Any] | None,
    summary: dict[str, Any],
    *,
    allow_equal_boundary_refresh: bool,
    current_summary_wins_equal_boundary: Callable[..., bool],
) -> bool:
    if not isinstance(current_summary, dict) or not is_summary_usable(current_summary):
        return False
    current_raw = current_summary.get("up_to_created_at")
    new_raw = summary.get("up_to_created_at")
    if not isinstance(current_raw, str) or not isinstance(new_raw, str):
        return False
    try:
        current_dt = datetime.fromisoformat(current_raw.replace("Z", "+00:00"))
        new_dt = datetime.fromisoformat(new_raw.replace("Z", "+00:00"))
        current_id = current_summary.get("up_to_message_id")
        new_id = summary.get("up_to_message_id")
        position_cmp = compare_message_position(
            current_dt,
            current_id if isinstance(current_id, str) else None,
            new_dt,
            new_id if isinstance(new_id, str) else None,
        )
    except ValueError:
        return False
    if position_cmp > 0:
        return True
    return position_cmp == 0 and current_summary_wins_equal_boundary(
        current_summary,
        summary,
        allow_equal_boundary_refresh=allow_equal_boundary_refresh,
    )


async def stop_summary_lock_renewal(
    renew_task: asyncio.Task[None] | None,
) -> None:
    if renew_task is None:
        return
    renew_task.cancel()
    try:
        await renew_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
