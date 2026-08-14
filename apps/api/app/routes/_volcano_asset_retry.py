"""Retry state machine for queued Volcano asset operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
import logging
from typing import Any

from fastapi import HTTPException
from lumen_core.volcano_assets import VolcanoAssetOperationOwnershipError


@dataclass(frozen=True)
class RetryDependencies:
    http_error: Callable[..., HTTPException]
    compare_and_set: Callable[..., Awaitable[tuple[bool, dict[str, Any] | None]]]
    same_operation_scope: Callable[[dict[str, Any], dict[str, Any]], bool]
    enqueue_operation: Callable[[dict[str, Any]], Awaitable[None]]
    mark_enqueue_failed: Callable[
        [Any, dict[str, Any]],
        Awaitable[dict[str, Any]],
    ]
    utc_iso: Callable[[], str]
    logger: logging.Logger


@dataclass(frozen=True)
class RetryPlan:
    action: str
    previous_attempt: int
    next_attempt: int
    queued_operation: dict[str, Any]


@dataclass(frozen=True)
class RetryResult:
    action: str
    operation: dict[str, Any]
    audit_required: bool


def _prepare_retry(
    operation_id: str,
    operation: dict[str, Any],
    *,
    allowed_actions: Collection[str],
    deps: RetryDependencies,
) -> RetryPlan:
    action = str(operation.get("action") or "")
    if action not in allowed_actions:
        raise deps.http_error(
            "video_asset_operation_action_invalid",
            "video asset operation action is invalid",
            409,
        )
    if not all(
        str(operation.get(key) or "").strip()
        for key in ("model", "provider_name", "project_name", "region")
    ):
        raise deps.http_error(
            "video_asset_operation_scope_invalid",
            "video asset operation scope is invalid",
            409,
        )
    if operation.get("status") != "failed" or not operation.get("retryable"):
        raise deps.http_error(
            "video_asset_operation_not_retryable",
            "video asset operation is not retryable",
            409,
        )
    previous_attempt = max(1, int(operation.get("attempt") or 1))
    next_attempt = previous_attempt + 1
    return RetryPlan(
        action=action,
        previous_attempt=previous_attempt,
        next_attempt=next_attempt,
        queued_operation={
            **operation,
            "status": "queued",
            "progress_stage": "queued",
            "attempt": next_attempt,
            "delivery_generation": 0,
            "retryable": False,
            "retry_after_seconds": None,
            "updated_at": deps.utc_iso(),
            "completed_at": None,
            "result": None,
            "error": None,
        },
    )


async def _claim_retry(
    redis: Any,
    plan: RetryPlan,
    *,
    user_id: str,
    deps: RetryDependencies,
) -> tuple[bool, dict[str, Any] | None]:
    try:
        return await deps.compare_and_set(
            redis,
            str(plan.queued_operation["id"]),
            owner_user_id=str(user_id),
            expected_status="failed",
            expected_attempt=plan.previous_attempt,
            replacement=plan.queued_operation,
        )
    except VolcanoAssetOperationOwnershipError as exc:
        raise deps.http_error(
            "video_asset_operation_not_found",
            "video asset operation was not found",
            404,
        ) from exc
    except Exception as exc:
        raise deps.http_error(
            "video_asset_queue_unavailable",
            "video asset operation queue is unavailable",
            503,
        ) from exc


def _is_recovered_claim(
    current: dict[str, Any] | None,
    plan: RetryPlan,
    *,
    deps: RetryDependencies,
) -> bool:
    return bool(
        current is not None
        and deps.same_operation_scope(current, plan.queued_operation)
        and max(1, int(current.get("attempt") or 1)) == plan.next_attempt
        and current.get("status") in {"queued", "running", "succeeded"}
    )


async def _reenqueue_recovered(
    redis: Any,
    current: dict[str, Any],
    *,
    deps: RetryDependencies,
) -> dict[str, Any]:
    if current.get("status") != "queued" or current.get("progress_stage") != "queued":
        return current
    try:
        await deps.enqueue_operation(current)
    except Exception:
        try:
            current = await deps.mark_enqueue_failed(redis, current)
        except Exception:
            deps.logger.error(
                "video_asset.retry_enqueue_state_failed operation_id=%s",
                current.get("id"),
                exc_info=True,
            )
    return current


async def _resolve_unclaimed(
    redis: Any,
    current: dict[str, Any] | None,
    plan: RetryPlan,
    *,
    deps: RetryDependencies,
) -> dict[str, Any]:
    if _is_recovered_claim(current, plan, deps=deps):
        return await _reenqueue_recovered(
            redis,
            current,
            deps=deps,
        )
    raise deps.http_error(
        "video_asset_operation_not_retryable",
        "video asset operation is not retryable",
        409,
    )


async def _enqueue_claimed(
    redis: Any,
    plan: RetryPlan,
    *,
    deps: RetryDependencies,
) -> dict[str, Any]:
    operation = plan.queued_operation
    try:
        await deps.enqueue_operation(operation)
    except Exception:
        deps.logger.warning(
            "video_asset.retry_enqueue_failed operation_id=%s",
            operation.get("id"),
            exc_info=True,
        )
        try:
            operation = await deps.mark_enqueue_failed(redis, operation)
        except Exception:
            deps.logger.error(
                "video_asset.retry_enqueue_state_failed operation_id=%s",
                operation.get("id"),
                exc_info=True,
            )
    return operation


async def retry_failed_operation(
    redis: Any,
    operation_id: str,
    operation: dict[str, Any],
    *,
    user_id: str,
    allowed_actions: Collection[str],
    deps: RetryDependencies,
) -> RetryResult:
    plan = _prepare_retry(
        operation_id,
        operation,
        allowed_actions=allowed_actions,
        deps=deps,
    )
    claimed, current = await _claim_retry(
        redis,
        plan,
        user_id=user_id,
        deps=deps,
    )
    if not claimed:
        recovered = await _resolve_unclaimed(
            redis,
            current,
            plan,
            deps=deps,
        )
        return RetryResult(
            action=plan.action,
            operation=recovered,
            audit_required=False,
        )
    queued = await _enqueue_claimed(
        redis,
        plan,
        deps=deps,
    )
    return RetryResult(
        action=plan.action,
        operation=queued,
        audit_required=True,
    )
