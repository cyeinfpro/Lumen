"""Durable billing obligations for checkpointed batch extras."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import GenerationStage, GenerationStatus
from lumen_core.model_entities.tasks import Generation

from .bonus_obligation import (
    BONUS_ARTIFACT_PENDING,
    BONUS_ARTIFACT_STATE_KEY,
    BONUS_BILLING_HEIGHT_KEY,
    BONUS_BILLING_OBLIGATION_KEY,
    BONUS_BILLING_WIDTH_KEY,
    billing_obligation_metadata,
    bonus_idempotency_key,
)
from .retry_state import generation_execution_epoch


def batch_extra_idempotency_suffix(
    *,
    execution_epoch: int,
    source_attempt: int,
    index: int,
) -> str:
    return (
        f":n{max(2, int(index))}:e{max(0, int(execution_epoch))}:"
        f"a{max(1, int(source_attempt))}"
    )


def add_batch_extra_billing_obligations(
    session: Any,
    state: Any,
    *,
    bonus_results: Sequence[tuple[int, str]],
    source_attempt: int,
    expected_count: int,
) -> None:
    if not bonus_results:
        return
    parent_idempotency_key = str(state.gen_idempotency_key or "")
    if not parent_idempotency_key:
        raise ValueError("batch extra billing obligation requires an idempotency key")
    width, height = _billing_dimensions(state)
    now = datetime.now(timezone.utc)
    for index, bonus_generation_id in bonus_results:
        session.add(
            Generation(
                id=bonus_generation_id,
                message_id=state.message_id,
                user_id=state.user_id,
                action=str(state.action),
                model=state.gen_model,
                prompt=state.prompt,
                size_requested=state.size_requested or f"{width}x{height}",
                aspect_ratio=state.aspect_ratio,
                input_image_ids=list(state.input_image_ids),
                primary_input_image_id=state.primary_input_image_id,
                upstream_request=_obligation_request(
                    state,
                    source_attempt=source_attempt,
                    index=index,
                    expected_count=expected_count,
                    width=width,
                    height=height,
                ),
                status=GenerationStatus.SUCCEEDED.value,
                progress_stage=GenerationStage.FINALIZING.value,
                attempt=0,
                idempotency_key=bonus_idempotency_key(
                    parent_idempotency_key,
                    batch_extra_idempotency_suffix(
                        execution_epoch=generation_execution_epoch(state),
                        source_attempt=source_attempt,
                        index=index,
                    ),
                ),
                started_at=now,
                finished_at=now,
                upstream_pixels=width * height,
            )
        )


def _billing_dimensions(state: Any) -> tuple[int, int]:
    raw = (
        getattr(getattr(state, "resolved", None), "size", None)
        or getattr(state, "size_requested", None)
        or ""
    )
    width_raw, separator, height_raw = str(raw).lower().partition("x")
    if not separator:
        raise ValueError("batch extra billing obligation requires a resolved size")
    try:
        width = int(width_raw)
        height = int(height_raw)
    except ValueError as exc:
        raise ValueError("batch extra billing obligation size is invalid") from exc
    if width <= 0 or height <= 0:
        raise ValueError("batch extra billing obligation dimensions must be positive")
    return width, height


def _obligation_request(
    state: Any,
    *,
    source_attempt: int,
    index: int,
    expected_count: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    request = (
        dict(state.gen_upstream_request_snapshot)
        if isinstance(state.gen_upstream_request_snapshot, dict)
        else {}
    )
    request.update(getattr(state, "image_request_options", {}) or {})
    request.update(
        {
            **billing_obligation_metadata(
                state,
                policy="batch_extra_settled_separately",
            ),
            BONUS_BILLING_OBLIGATION_KEY: True,
            BONUS_BILLING_WIDTH_KEY: width,
            BONUS_BILLING_HEIGHT_KEY: height,
            BONUS_ARTIFACT_STATE_KEY: BONUS_ARTIFACT_PENDING,
            "parent_generation_id": state.task_id,
            "parent_execution_epoch": generation_execution_epoch(state),
            "parent_attempt": max(1, int(source_attempt)),
            "batch_parent_generation_id": state.task_id,
            "batch_index": max(2, int(index)),
            "batch_count": max(1, int(expected_count)),
        }
    )
    return request


__all__ = [
    "add_batch_extra_billing_obligations",
    "batch_extra_idempotency_suffix",
]
