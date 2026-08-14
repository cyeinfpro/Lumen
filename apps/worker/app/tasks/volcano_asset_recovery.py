"""Periodic recovery for orphaned Volcano asset operations."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from arq import cron
from lumen_core.volcano_assets import compare_and_set_volcano_asset_operation

from .volcano_asset_retry_policy import (
    VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT,
)

logger = logging.getLogger(__name__)

_RECOVERY_LOCK_KEY = "video-assets:recovery-lock"
_RECOVERY_LOCK_TTL_SECONDS = 25
_RECOVERY_SCAN_LIMIT = 500
_QUEUED_STALE_SECONDS = 90
_RUNNING_STALE_SECONDS = 30
_LEGACY_AMBIGUOUS_STALE_SECONDS = 30
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _CronJobs(tuple[Any, ...]):
    """Immutable schedules that compose with legacy list-based schedules."""

    def __radd__(self, other: list[Any]) -> list[Any]:
        return [*other, *self]


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _operation_age_seconds(operation: dict[str, Any], now: datetime) -> float:
    updated_at = _parse_time(operation.get("updated_at"))
    if updated_at is None:
        return float("inf")
    return max(0.0, (now - updated_at).total_seconds())


def _retry_due(operation: dict[str, Any], now: datetime) -> bool:
    retry_not_before = _parse_time(operation.get("retry_not_before"))
    return retry_not_before is None or retry_not_before <= now


def _legacy_ambiguous_is_recoverable(
    operation: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    error = operation.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    return bool(
        operation.get("status") == "failed"
        and error_code == "volcano_asset_create_reconcile_ambiguous"
        and operation.get("submit_started_at")
        and operation.get("submit_outcome_uncertain")
        and max(0, int(operation.get("automatic_retry_count") or 0))
        < VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT
        and _operation_age_seconds(operation, now) >= _LEGACY_AMBIGUOUS_STALE_SECONDS
    )


async def _recovery_stage(
    redis: Any,
    operation: dict[str, Any],
    *,
    now: datetime,
) -> str | None:
    status = str(operation.get("status") or "")
    if _legacy_ambiguous_is_recoverable(operation, now=now):
        return "reconciling_submit"
    if status == "queued":
        if not _retry_due(operation, now):
            return None
        stale_after = (
            5
            if operation.get("delivery_enqueued") is False
            else max(
                _QUEUED_STALE_SECONDS,
                int(operation.get("retry_after_seconds") or 0) + 30,
            )
        )
        if _operation_age_seconds(operation, now) >= stale_after:
            return str(operation.get("progress_stage") or "recovery_queued")
        return None
    if status != "running":
        return None
    if _operation_age_seconds(operation, now) < _RUNNING_STALE_SECONDS:
        return None
    operation_id = str(operation.get("id") or "")
    if not operation_id:
        return None
    if await redis.exists(f"video-assets:operation-lock:{operation_id}"):
        return None
    return "recovery_queued"


async def _claim_recovery(
    redis: Any,
    operation: dict[str, Any],
    *,
    progress_stage: str,
    now: datetime,
) -> dict[str, Any] | None:
    operation_id = str(operation.get("id") or "")
    user_id = str(operation.get("user_id") or "")
    status = str(operation.get("status") or "")
    attempt = max(1, int(operation.get("attempt") or 1))
    if not operation_id or not user_id or not status:
        return None
    delivery_generation = (
        max(
            0,
            int(operation.get("delivery_generation") or 0),
        )
        + 1
    )
    replacement = {
        **operation,
        "status": "queued",
        "progress_stage": progress_stage,
        "delivery_generation": delivery_generation,
        "delivery_enqueued": True,
        "retry_not_before": None,
        "retryable": True,
        "retry_after_seconds": 1,
        "updated_at": _utc_iso(now),
        "completed_at": None,
        "result": None,
    }
    claimed, current = await compare_and_set_volcano_asset_operation(
        redis,
        operation_id,
        owner_user_id=user_id,
        expected_status=status,
        expected_attempt=attempt,
        expected_progress_stage=str(operation.get("progress_stage") or ""),
        replacement=replacement,
    )
    if claimed:
        return replacement
    return current if current and current == replacement else None


async def _recover_operation(
    redis: Any,
    operation: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    stage = await _recovery_stage(redis, operation, now=now)
    if stage is None:
        return False
    claimed = await _claim_recovery(
        redis,
        operation,
        progress_stage=stage,
        now=now,
    )
    if claimed is None:
        return False
    operation_id = str(claimed["id"])
    attempt = max(1, int(claimed.get("attempt") or 1))
    delivery_generation = max(
        0,
        int(claimed.get("delivery_generation") or 0),
    )
    await redis.enqueue_job(
        "process_volcano_asset_operation",
        operation_id,
        attempt,
        delivery_generation,
        _job_id=(
            f"volcano-asset:{operation_id}:recovery:{attempt}:{delivery_generation}"
        ),
    )
    logger.warning(
        "video_asset.operation_recovered operation_id=%s previous_status=%s "
        "stage=%s delivery_generation=%s",
        operation_id,
        operation.get("status"),
        stage,
        delivery_generation,
    )
    return True


async def reconcile_volcano_asset_operations(ctx: dict[str, Any]) -> int:
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("Redis is required for Volcano asset recovery")
    token = secrets.token_hex(16)
    locked = await redis.set(
        _RECOVERY_LOCK_KEY,
        token,
        nx=True,
        ex=_RECOVERY_LOCK_TTL_SECONDS,
    )
    if not locked:
        return 0
    recovered = 0
    now = datetime.now(timezone.utc)
    try:
        scanned = 0
        async for raw_key in redis.scan_iter(match="video-assets:operation:*"):
            scanned += 1
            if scanned > _RECOVERY_SCAN_LIMIT:
                break
            key = (
                raw_key.decode("utf-8", errors="replace")
                if isinstance(raw_key, bytes)
                else str(raw_key)
            )
            raw = await redis.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                operation = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.error("video_asset.recovery_invalid_operation key=%s", key)
                continue
            if not isinstance(operation, dict):
                continue
            try:
                if await _recover_operation(redis, operation, now=now):
                    recovered += 1
            except Exception:
                logger.exception(
                    "video_asset.operation_recovery_failed operation_id=%s",
                    operation.get("id"),
                )
        return recovered
    finally:
        try:
            await redis.eval(
                _RELEASE_LOCK_SCRIPT,
                1,
                _RECOVERY_LOCK_KEY,
                token,
            )
        except Exception:
            logger.warning("video_asset.recovery_lock_release_failed", exc_info=True)


cron_jobs = _CronJobs(
    (
        cron(
            reconcile_volcano_asset_operations,
            second={15, 45},
            run_at_startup=True,
            timeout=20,
        ),
    )
)


__all__ = ["cron_jobs", "reconcile_volcano_asset_operations"]
