"""Durable finalization for multi-image generation results."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from ...provider_runtime.errors import UpstreamError
from .batch_obligations import batch_extra_idempotency_suffix
from .bonus_context import build_bonus_context
from .bonus_obligation import billing_obligation_metadata
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .persistence import handle_dual_race_bonus_image
from .retry_state import generation_execution_epoch
from .takeover_checkpoint import (
    RESULT_FINALIZATION_FINALIZED,
    GenerationTakeoverCheckpointUnavailable,
    generation_takeover_checkpoint,
    generation_takeover_extras_finalized,
    generation_takeover_result,
    mark_generation_takeover_result_finalized,
)


class BatchExtraFinalizationError(UpstreamError):
    """A produced extra image was not durably finalized."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_code="batch_extra_finalization_pending",
            payload={"recovery_only": True},
        )


async def finalize_batch_extra_images(
    state: Any,
    actual_image_count: int,
) -> None:
    checkpoint = generation_takeover_checkpoint(state)
    source_attempt = checkpoint.attempt if checkpoint is not None else state.attempt
    for batch_index, (extra_b64, extra_revised) in state.batch_extra_pairs:
        result = generation_takeover_result(state, batch_index)
        if (
            result is not None
            and result.finalization_state == RESULT_FINALIZATION_FINALIZED
        ):
            continue
        if checkpoint is not None and (
            result is None or result.bonus_generation_id is None
        ):
            raise GenerationTakeoverCheckpointUnavailable(
                f"generation takeover checkpoint result is invalid index={batch_index}"
            )
        bonus_generation_id = result.bonus_generation_id if result is not None else None
        try:
            persisted = await handle_dual_race_bonus_image(
                replace(
                    build_bonus_context(state, extra_b64, extra_revised),
                    upstream_provider=state.actual_upstream_provider,
                    upstream_actual_route=state.actual_upstream_route,
                    upstream_actual_source=state.actual_upstream_source,
                    upstream_actual_endpoint=state.actual_upstream_endpoint,
                    billing_meta=billing_obligation_metadata(
                        state,
                        policy="batch_extra_settled_separately",
                    ),
                    idempotency_suffix=batch_extra_idempotency_suffix(
                        execution_epoch=generation_execution_epoch(state),
                        source_attempt=source_attempt,
                        index=batch_index,
                    ),
                    extra_upstream_fields={
                        "batch_parent_generation_id": state.task_id,
                        "batch_index": batch_index,
                        "batch_count": actual_image_count,
                    },
                    record_model_library_candidate=False,
                    settle_billing=True,
                    log_label="image2 n result",
                    bonus_generation_id=bonus_generation_id,
                    require_precreated_generation=bonus_generation_id is not None,
                    source_attempt=source_attempt,
                )
            )
        except (
            LeaseLost,
            StaleGenerationAttempt,
            TaskCancelled,
            GenerationTakeoverCheckpointUnavailable,
            asyncio.CancelledError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            raise BatchExtraFinalizationError(
                "image batch extra finalization requires takeover "
                f"task={state.task_id} index={batch_index}"
            ) from exc
        if not persisted:
            raise BatchExtraFinalizationError(
                "image batch extra was not durably finalized "
                f"task={state.task_id} index={batch_index}"
            )
        if bonus_generation_id is not None:
            try:
                await mark_generation_takeover_result_finalized(
                    state,
                    index=batch_index,
                    bonus_generation_id=bonus_generation_id,
                )
            except (
                LeaseLost,
                StaleGenerationAttempt,
                TaskCancelled,
                GenerationTakeoverCheckpointUnavailable,
                asyncio.CancelledError,
            ):
                raise
            except Exception as exc:  # noqa: BLE001
                raise BatchExtraFinalizationError(
                    "image batch extra checkpoint update requires takeover "
                    f"task={state.task_id} index={batch_index}"
                ) from exc
    if not generation_takeover_extras_finalized(state):
        raise BatchExtraFinalizationError(
            f"image batch extras remain pending task={state.task_id}"
        )


__all__ = [
    "BatchExtraFinalizationError",
    "finalize_batch_extra_images",
]
