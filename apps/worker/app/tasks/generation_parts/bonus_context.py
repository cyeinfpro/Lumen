from __future__ import annotations

from ...upstream_parts import GeneratedPayloadInput
from .bonus_artifacts import BonusGenerationContext
from .bonus_obligation import (
    billing_obligation_metadata,
    dual_race_bonus_idempotency_suffix,
)
from .retry_state import generation_execution_epoch
from .run_state import GenerationRunState


def build_bonus_context(
    state: GenerationRunState,
    b64_result: GeneratedPayloadInput,
    revised_prompt: str | None,
) -> BonusGenerationContext:
    return BonusGenerationContext(
        services=state.services,
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        parent_task_id=state.task_id,
        execution_epoch=generation_execution_epoch(state),
        attempt=state.attempt,
        parent_idempotency_key=state.gen_idempotency_key,
        parent_upstream_request=(
            state.parent_upstream_request_for_bonus
            or state.gen_upstream_request_snapshot
        ),
        message_id=state.message_id,
        action=str(state.action),
        model=state.gen_model,
        prompt=state.prompt,
        size_requested=state.size_requested,
        aspect_ratio=state.aspect_ratio,
        input_image_ids=state.input_image_ids,
        primary_input_image_id=state.primary_input_image_id,
        references=state.references,
        image_request_options=state.image_request_options,
        b64_result=b64_result,
        revised_prompt=revised_prompt,
        upstream_provider=None,
        upstream_actual_route=None,
        upstream_actual_source=None,
        upstream_actual_endpoint=None,
        billing_meta=billing_obligation_metadata(
            state,
            policy="dual_race_loser_settled_separately",
            is_dual_race_bonus=True,
        ),
        idempotency_suffix=dual_race_bonus_idempotency_suffix(
            generation_execution_epoch(state),
            state.attempt,
        ),
        extra_upstream_fields=None,
        record_model_library_candidate=True,
        settle_billing=False,
        log_label="dual_race bonus",
        bonus_generation_id=state.dual_race_bonus_obligation_id,
        require_precreated_generation=(
            state.dual_race_bonus_obligation_id is not None
        ),
    )
