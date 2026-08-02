"""Initial database claim and queued-state validation phase."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_GEN_FAILED,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
)
from lumen_core.generation_resources import generation_resource_demand
from lumen_core.models import Generation, Message
from lumen_core.queue_metadata import generation_queue_metadata
from lumen_core.upstream_billing import upstream_dispatch_result_unknown

from ...observability import safe_outcome, task_duration_seconds
from ...upstream_clients.image_job_models import ImageJobExecutionHandle
from ..state import is_generation_terminal
from .active_user_fence import lock_active_generation_user
from .diagnostics import generation_trace_id, queue_wait_ms
from .errors import TaskCancelled
from .execution_boundary import (
    SIDECAR_EXECUTION_KEY,
    release_or_settle_generation,
)
from .lifecycle import settle_existing_generated_image
from .persistence import (
    ensure_generation_conversation_alive,
    find_existing_generated_image,
)
from .request_options import (
    image_request_options,
    primary_input_image_id_valid,
)
from .retry_state import (
    MAX_ATTEMPTS,
    bounded_next_attempt,
    ensure_generation_updated,
    generation_attempt_update,
    generation_execution_trace_id,
)
from .run_state import GenerationRunState
from .runner_phase_services import ClaimGenerationServices


logger = logging.getLogger(f"{__package__}.runner")
RESULT_UNKNOWN_CODE = "result_unknown"
RESULT_UNKNOWN_MESSAGE = "upstream dispatch has no response receipt; result is unknown"


async def load_initial_generation(state: GenerationRunState) -> bool:
    services = ClaimGenerationServices.from_deps(state.services)
    async with services.store.session() as session:
        owner_id = (
            await session.execute(
                select(Generation.user_id).where(Generation.id == state.task_id)
            )
        ).scalar_one_or_none()
        if owner_id is not None and not await lock_active_generation_user(
            session,
            user_id=str(owner_id),
        ):
            logger.info(
                "generation initial claim blocked by inactive user task_id=%s",
                state.task_id,
            )
            return False
        generation = await claim_generation_row(state, session)
        if generation is None:
            return False
        if generation_cannot_start(generation):
            return False
        load_generation_fields(state, generation)
        if not await validate_conversation(state, session, services):
            return False
        if not await validate_primary_input(state, session, services):
            return False
        existing = await find_existing_generated_image(
            session,
            task_id=state.task_id,
            user_id=state.user_id,
        )
        if existing is not None:
            state.task_outcome = await settle_existing_generated_image(
                session,
                redis=state.redis,
                task_id=state.task_id,
                user_id=state.user_id,
                message_id=state.message_id,
                generation=generation,
                existing_image=existing,
                task_started_at=state.task_start,
                services=services,
            )
            return False
        if await fail_nonreplayable_dispatch(state, session, services):
            return False
        state.attempt, may_run = bounded_next_attempt(generation.attempt)
        if not may_run:
            await fail_max_attempts(state, session, services)
            return False
        request = state.gen_upstream_request_snapshot or {}
        metadata = generation_queue_metadata(
            upstream_request=request,
            action=state.action,
            size_requested=state.size_requested,
            mask_image_id=state.mask_image_id,
            created_at=state.gen_created_at,
        )
        state.resource_demand = generation_resource_demand(
            pixel_count=metadata.get("pixel_count"),
            reference_count=len(state.input_image_ids),
            action=str(state.action),
            has_mask=bool(state.mask_image_id),
            transparent=request.get("background") == "transparent",
            output_count=int(request.get("n") or 1),
            dual_race=request.get("image_route") == "dual_race",
        )
    return True


async def fail_nonreplayable_dispatch(
    state: GenerationRunState,
    session: Any,
    services: ClaimGenerationServices,
) -> bool:
    generation = state.generation
    if not upstream_dispatch_result_unknown(
        generation,
        execution_epoch=getattr(generation, "execution_epoch", None),
    ):
        return False
    logger.warning(
        "generation replay blocked by unresolved dispatch task=%s epoch=%s attempt=%s",
        state.task_id,
        getattr(generation, "execution_epoch", 0),
        getattr(generation, "attempt", 0),
    )
    await fail_queued_generation(
        state,
        session,
        code=RESULT_UNKNOWN_CODE,
        message=RESULT_UNKNOWN_MESSAGE,
        next_attempt=None,
        services=services,
    )
    return True


async def claim_generation_row(
    state: GenerationRunState,
    session: Any,
) -> Any | None:
    task_id = state.task_id
    generation = (
        await session.execute(
            select(Generation)
            .where(Generation.id == task_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if generation is not None:
        return generation
    existing_id = (
        await session.execute(select(Generation.id).where(Generation.id == task_id))
    ).scalar_one_or_none()
    if existing_id is not None:
        logger.info(
            "generation initial claim skipped locked row task_id=%s",
            task_id,
        )
    else:
        logger.warning("generation not found task_id=%s", task_id)
    return None


def generation_cannot_start(generation: Any) -> bool:
    if is_generation_terminal(generation.status):
        logger.info(
            "generation already terminal task_id=%s status=%s",
            generation.id,
            generation.status,
        )
        return True
    if getattr(generation, "cancel_requested_at", None) is not None:
        logger.info(
            "generation cancellation already requested task_id=%s",
            generation.id,
        )
        return True
    if generation.status == GenerationStatus.RUNNING.value:
        logger.info("generation already running task_id=%s", generation.id)
        return True
    return False


def load_generation_fields(
    state: GenerationRunState,
    generation: Any,
) -> None:
    state.generation = generation
    state.loaded_attempt = generation.attempt
    state.gen_created_at = getattr(generation, "created_at", None)
    state.user_id = generation.user_id
    state.message_id = generation.message_id
    state.action = generation.action
    state.prompt = generation.prompt
    state.aspect_ratio = generation.aspect_ratio
    state.size_requested = generation.size_requested
    state.input_image_ids = list(generation.input_image_ids or [])
    state.primary_input_image_id = generation.primary_input_image_id
    state.user_api_credential_id = getattr(
        generation,
        "user_api_credential_id",
        None,
    )
    state.mask_image_id = getattr(generation, "mask_image_id", None)
    state.gen_idempotency_key = generation.idempotency_key
    state.gen_model = generation.model
    state.gen_upstream_request_snapshot = (
        dict(generation.upstream_request)
        if isinstance(generation.upstream_request, dict)
        else None
    )
    raw_execution = (
        state.gen_upstream_request_snapshot.get(SIDECAR_EXECUTION_KEY)
        if isinstance(state.gen_upstream_request_snapshot, dict)
        else None
    )
    state.sidecar_execution = ImageJobExecutionHandle.from_mapping(raw_execution)
    state.trace_id = generation_execution_trace_id(
        generation_trace_id(
            state.task_id,
            state.gen_upstream_request_snapshot,
        ),
        generation.execution_epoch,
    )
    state.stage_timer.set_ms(
        "queue_wait",
        queue_wait_ms(state.gen_created_at),
    )
    state.image_request_options = image_request_options(
        generation.upstream_request,
        size=state.size_requested,
    )


async def validate_conversation(
    state: GenerationRunState,
    session: Any,
    services: ClaimGenerationServices,
) -> bool:
    try:
        await ensure_generation_conversation_alive(
            session,
            message_id=state.message_id,
            user_id=state.user_id,
        )
        return True
    except TaskCancelled as exc:
        await cancel_queued_generation(state, session, str(exc), services)
        return False


async def cancel_queued_generation(
    state: GenerationRunState,
    session: Any,
    message: str,
    services: ClaimGenerationServices,
) -> None:
    result = await session.execute(
        generation_attempt_update(
            state.task_id,
            state.generation.attempt,
            statuses=(GenerationStatus.QUEUED.value,),
            allow_cancel_requested=True,
        ).values(
            status=GenerationStatus.CANCELED.value,
            progress_stage=GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            error_code=EC.CANCELLED.value,
            error_message=message,
        )
    )
    ensure_generation_updated(
        result,
        state.task_id,
        state.generation.attempt,
    )
    row = await session.get(Message, state.message_id)
    if row is not None and row.status not in (
        MessageStatus.SUCCEEDED,
        MessageStatus.FAILED,
        MessageStatus.CANCELED,
    ):
        row.status = MessageStatus.FAILED
    await release_or_settle_generation(
        services.billing,
        session,
        state.generation,
        reason=EC.CANCELLED.value,
    )
    await session.commit()
    await services.billing.flush_after_commit(session)
    await publish_queued_failure(
        state,
        EC.CANCELLED.value,
        message,
        services,
    )
    state.task_outcome = "failed"


async def validate_primary_input(
    state: GenerationRunState,
    session: Any,
    services: ClaimGenerationServices,
) -> bool:
    if primary_input_image_id_valid(
        state.primary_input_image_id,
        state.input_image_ids,
    ):
        return True
    await fail_queued_generation(
        state,
        session,
        code=EC.INVALID_PARAM.value,
        message="primary_input_image_id must be included in input_image_ids",
        next_attempt=None,
        services=services,
    )
    return False


async def fail_max_attempts(
    state: GenerationRunState,
    session: Any,
    services: ClaimGenerationServices,
) -> None:
    await fail_queued_generation(
        state,
        session,
        code="max_attempts_exceeded",
        message=f"generation exceeded max attempts ({MAX_ATTEMPTS})",
        next_attempt=state.attempt,
        services=services,
    )
    observe_task_duration(state)


async def fail_queued_generation(
    state: GenerationRunState,
    session: Any,
    *,
    code: str,
    message: str,
    next_attempt: int | None,
    services: ClaimGenerationServices | None = None,
) -> None:
    phase_services = services or ClaimGenerationServices.from_deps(state.services)
    values: dict[str, Any] = {
        "status": GenerationStatus.FAILED.value,
        "progress_stage": GenerationStage.FINALIZING,
        "finished_at": datetime.now(timezone.utc),
        "error_code": code,
        "error_message": message,
    }
    if next_attempt is not None:
        values["attempt"] = next_attempt
    result = await session.execute(
        generation_attempt_update(
            state.task_id,
            state.generation.attempt,
            statuses=(GenerationStatus.QUEUED.value,),
        ).values(**values)
    )
    ensure_generation_updated(
        result,
        state.task_id,
        state.generation.attempt,
    )
    row = await session.get(Message, state.message_id)
    if row is not None and row.status != MessageStatus.CANCELED:
        row.status = MessageStatus.FAILED
    generation = await session.get(Generation, state.task_id)
    if generation is not None:
        if services is None:
            await release_or_settle_generation(
                state.services.billing,
                session,
                generation,
                reason=code,
            )
        else:
            await release_or_settle_generation(
                phase_services.billing,
                session,
                generation,
                reason=code,
            )
    await session.commit()
    if services is None:
        await state.services.billing.flush_after_commit(session)
    else:
        await phase_services.billing.flush_after_commit(session)
    await publish_queued_failure(state, code, message, phase_services)
    state.task_outcome = "failed"


async def publish_queued_failure(
    state: GenerationRunState,
    code: str,
    message: str,
    services: ClaimGenerationServices | None = None,
) -> None:
    phase_services = services or ClaimGenerationServices.from_deps(state.services)
    await phase_services.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_FAILED,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "code": code,
            "message": message,
            "retriable": False,
        },
    )


def observe_task_duration(state: GenerationRunState) -> None:
    try:
        duration = asyncio.get_event_loop().time() - state.task_start
        task_duration_seconds.labels(
            kind="generation",
            outcome=safe_outcome(state.task_outcome),
        ).observe(duration)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "claim_generation_row",
    "fail_nonreplayable_dispatch",
    "fail_queued_generation",
    "generation_cannot_start",
    "load_generation_fields",
    "load_initial_generation",
    "publish_queued_failure",
]
