"""Durable upstream evidence used by task reconciliation."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lumen_core.upstream_billing import (
    GENERATION_TAKEOVER_CHECKPOINT_KEY,
    decide_dispatch_evidence_billing,
    has_upstream_response_receipt,
    receipt_execution_identity,
    upstream_request_dict,
)


def generation_sidecar_cost_requires_settlement(task: Any) -> bool:
    execution = upstream_request_dict(task).get("sidecar_execution")
    return bool(
        isinstance(execution, dict)
        and execution.get("cost_knowledge") in {"unknown", "incurred"}
    )


def dispatch_cost_requires_settlement(task: Any) -> bool:
    return not decide_dispatch_evidence_billing(
        task,
        actual_cost_known=False,
    ).released


def generation_has_takeover_checkpoint(task: Any) -> bool:
    """Validate the durable envelope; the runner verifies payload bytes."""

    request = upstream_request_dict(task)
    raw = request.get(GENERATION_TAKEOVER_CHECKPOINT_KEY)
    if not isinstance(raw, dict):
        return False
    try:
        version = int(raw.get("version"))
        execution_epoch = max(0, int(raw.get("execution_epoch")))
        attempt = max(1, int(raw.get("attempt")))
        task_epoch = max(0, int(getattr(task, "execution_epoch", 0) or 0))
        task_attempt = max(0, int(getattr(task, "attempt", 0) or 0))
    except (TypeError, ValueError):
        return False
    task_id = str(getattr(task, "id", "") or "")
    user_id = str(getattr(task, "user_id", "") or "")
    response_attempt, response_epoch = receipt_execution_identity(
        request,
        response=True,
    )
    return bool(
        execution_epoch == task_epoch
        and attempt <= task_attempt
        and task_id
        and user_id
        and _takeover_payloads_valid(
            raw,
            version=version,
            task_id=task_id,
            user_id=user_id,
            execution_epoch=execution_epoch,
            attempt=attempt,
        )
        and has_upstream_response_receipt(request, execution_epoch=execution_epoch)
        and response_attempt == attempt
        and response_epoch == execution_epoch
    )


def _takeover_payloads_valid(
    raw: dict[str, Any],
    *,
    version: int,
    task_id: str,
    user_id: str,
    execution_epoch: int,
    attempt: int,
) -> bool:
    prefix = (
        f"u/{user_id}/g/{task_id}/executions/{execution_epoch}/"
        f"attempts/{attempt}"
    )
    if version == 1:
        return _payload_valid(
            raw,
            expected_index=1,
            expected_storage_key=f"{prefix}/takeover-result.bin",
            expected_bonus_id=None,
        )
    if version != 2 or raw.get("collection_complete") is not True:
        return False
    results = raw.get("results")
    try:
        expected_count = max(1, int(raw.get("expected_count")))
    except (TypeError, ValueError):
        return False
    if not isinstance(results, list) or len(results) != expected_count:
        return False
    return all(
        _payload_valid(
            result,
            expected_index=index,
            expected_storage_key=f"{prefix}/takeover-result-{index}.bin",
            expected_bonus_id=(
                _batch_extra_generation_id(
                    task_id=task_id,
                    execution_epoch=execution_epoch,
                    attempt=attempt,
                    index=index,
                )
                if index > 1
                else None
            ),
        )
        for index, result in enumerate(results, start=1)
    )


def _payload_valid(
    raw: Any,
    *,
    expected_index: int,
    expected_storage_key: str,
    expected_bonus_id: str | None,
) -> bool:
    if not isinstance(raw, dict):
        return False
    try:
        index = max(1, int(raw.get("index", 1)))
        size_bytes = max(0, int(raw.get("size_bytes")))
    except (TypeError, ValueError):
        return False
    digest = raw.get("sha256")
    bonus_generation_id = raw.get("bonus_generation_id")
    finalization_state = raw.get("finalization_state", "pending")
    return bool(
        index == expected_index
        and raw.get("storage_key") == expected_storage_key
        and size_bytes > 0
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdefABCDEF" for character in digest)
        and bonus_generation_id == expected_bonus_id
        and finalization_state in {"pending", "finalized"}
    )


def _batch_extra_generation_id(
    *,
    task_id: str,
    execution_epoch: int,
    attempt: int,
    index: int,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            (
                f"lumen:batch-extra:{task_id}:{execution_epoch}:"
                f"{attempt}:{index}"
            ),
        )
    )


__all__ = [
    "dispatch_cost_requires_settlement",
    "generation_has_takeover_checkpoint",
    "generation_sidecar_cost_requires_settlement",
]
