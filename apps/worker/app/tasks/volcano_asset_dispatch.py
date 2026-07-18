"""Dispatch and idempotency checks for Volcano asset operations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _runtime() -> Any:
    from . import volcano_assets

    return volcano_assets


def _delivery_result(
    operation_id: str,
    *,
    status: str,
    attempt: int,
    delivery_generation: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "operation_id": operation_id,
        "attempt": attempt,
        "delivery_generation": delivery_generation,
    }


async def _load_delivery(
    persistence: Any,
    operation_id: str,
    expected_attempt: int | None,
    expected_delivery_generation: int | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    runtime = _runtime()
    redis = persistence.redis
    operation = await runtime._get_operation(redis, operation_id)
    if operation is None:
        return None, {"status": "missing", "operation_id": operation_id}
    persistence.bind(operation)
    current_attempt = max(1, int(operation.get("attempt") or 1))
    current_generation = max(
        0,
        int(operation.get("delivery_generation") or 0),
    )
    if expected_attempt is not None and expected_attempt != current_attempt:
        return None, _delivery_result(
            operation_id,
            status="stale",
            attempt=current_attempt,
            delivery_generation=current_generation,
        )
    generation_mismatch = (
        expected_delivery_generation is not None
        and expected_delivery_generation != current_generation
    )
    if not generation_mismatch:
        return operation, None
    recovered = await runtime._recover_unconfirmed_delivery(
        persistence,
        operation,
    )
    return None, _delivery_result(
        operation_id,
        status="delivery_recovered" if recovered else "stale",
        attempt=current_attempt,
        delivery_generation=current_generation,
    )


async def _normalize_operation(
    persistence: Any,
    operation: dict[str, Any],
) -> None:
    runtime = _runtime()
    action = str(operation.get("action") or "")
    should_normalize_name = action in {
        "create_group",
        "create_asset",
        "update_asset",
    } or (action == "update_group" and runtime._operation_has_value(operation, "name"))
    if should_normalize_name:
        normalized_name = runtime.normalize_volcano_asset_name(
            operation.get("name"),
            fallback_id=str(operation.get("id") or ""),
        )
        if normalized_name != operation.get("name"):
            operation["name"] = normalized_name
            if operation.get("status") in {"queued", "running"}:
                await persistence.update(operation, name=normalized_name)
    if action == "create_group" and not runtime._operation_has_value(
        operation,
        "description",
    ):
        operation["description"] = ""
        if operation.get("status") in {"queued", "running"}:
            await persistence.update(operation, description="")


async def _receipt_completion(
    persistence: Any,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _runtime()
    try:
        receipt_result = await runtime._read_success_receipt(
            operation,
            fence=persistence.fence,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "video_asset.success_receipt_lookup_failed operation_id=%s",
            operation.get("id"),
            exc_info=True,
        )
        return None
    if receipt_result is None:
        return None
    return await runtime._complete_operation(
        persistence,
        operation,
        receipt_result,
        receipt_exists=True,
    )


def _terminal_result(operation: dict[str, Any]) -> dict[str, Any] | None:
    operation_id = str(operation.get("id") or "")
    status = str(operation.get("status") or "")
    if status == "succeeded":
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "result": operation.get("result"),
        }
    if status == "failed":
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": operation.get("error"),
        }
    return None


def _dispatch_failure(operation: dict[str, Any]) -> Any | None:
    runtime = _runtime()
    status = str(operation.get("status") or "")
    if status not in {"queued", "running"}:
        return runtime._OperationFailure(
            "video_asset_operation_state_invalid",
            "video asset operation state is invalid",
            retryable=False,
        )
    return runtime._operation_contract_failure(operation)


async def _process_locked(
    ctx: dict[str, Any],
    operation_id: str,
    expected_attempt: int | None,
    expected_delivery_generation: int | None,
    *,
    persistence: Any,
) -> dict[str, Any]:
    """Run one idempotent or safely reconcilable Volcano asset operation."""
    runtime = _runtime()
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("Redis is required for Volcano asset operations")
    operation, early_result = await _load_delivery(
        persistence,
        operation_id,
        expected_attempt,
        expected_delivery_generation,
    )
    if early_result is not None:
        return early_result
    assert operation is not None
    await _normalize_operation(persistence, operation)
    receipt_result = await _receipt_completion(persistence, operation)
    if receipt_result is not None:
        return receipt_result
    terminal_result = _terminal_result(operation)
    if terminal_result is not None:
        return terminal_result
    failure = _dispatch_failure(operation)
    if str(operation.get("action") or "") == "create_asset":
        return await runtime._process_create_asset(
            redis,
            operation,
            failure,
            persistence=persistence,
        )
    if failure is not None:
        return await runtime._record_operation_failure(
            persistence,
            operation,
            failure,
        )
    return await runtime._process_management_action(
        redis,
        operation,
        persistence=persistence,
    )


__all__ = ["_process_locked"]
