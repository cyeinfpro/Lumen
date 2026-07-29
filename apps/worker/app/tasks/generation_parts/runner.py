from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from lumen_core.constants import (
    EV_GEN_PROGRESS,
    EV_GEN_QUEUED,
    EV_GEN_STARTED,
    GenerationAction,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    task_channel,
)
from lumen_core.generation_resources import generation_resource_demand
from lumen_core.models import Generation, Message, new_uuid7
from lumen_core.queue_metadata import generation_queue_metadata, merge_queue_metadata

from ...observability import (
    safe_outcome,
    task_duration_seconds,
)
from ...generation_dispatch import dispatch_identity_from_context
from ...provider_runtime.errors import UpstreamError
from ..state import is_generation_terminal
from . import failure, success
from .admission import WeightedPermit
from .diagnostics import (
    StageTimer,
)
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .execution_boundary import release_or_settle_generation
from .lease import (
    acquire_lease,
    cancel_renewer_task,
    lease_renewer,
    release_lease,
)
from .queue import (
    IMAGE_PROVIDER_UNAVAILABLE_RETRY_S,
    IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
    enqueue_generation_once,
    image_queue_not_before_key,
    image_task_provider_key,
    inflight_set_fields,
    is_dual_race_sentinel,
    kick_image_queue,
    redis_text,
)
from .queue_claim import (
    GenerationResourceLease,
    release_generation_runtime_resources,
    release_image_queue_slot,
    reserve_image_queue_slot,
)
from .retry_state import (
    bounded_next_attempt,
    consume_image_iter_close_result,
    ensure_generation_updated,
    generation_attempt_update,
)
from .run_state import GenerationRunState
from .runtime_contracts import GENERATION_RUN_TIMEOUT_S
from .runner_claim_phase import (
    fail_queued_generation as _fail_queued_generation,
    load_initial_generation as _load_initial_generation,
    publish_queued_failure as _publish_queued_failure,
)
from .runner_dispatch_phase import (
    build_image_iterator as _build_image_iterator,
    dispatch_upstream_request as _dispatch_upstream_request,
    initialize_execution_state as _initialize_execution_state,
    prepare_upstream_request as _prepare_upstream_request,
)
from .services import RunGenerationDeps


LEASE_REACQUIRED_SUBSTAGE = "lease_reacquired"
logger = logging.getLogger(__name__)


async def run_generation(
    ctx: dict[str, Any],
    task_id: str,
    services: RunGenerationDeps,
) -> None:
    """Run one ARQ image-generation task through explicit lifecycle phases."""
    state = _new_run_state(ctx, task_id, services)
    if not await _load_initial_generation(state):
        return
    if not await _prepare_provider_reservation(state):
        return
    if not await _start_generation_attempt(state):
        return
    _initialize_execution_state(state)
    try:
        await _prepare_upstream_request(state)
        await _dispatch_upstream_request(state)
        await success.finalize_generation_success(state, state.services)
    except LeaseLost as exc:
        await failure.handle_lease_lost(state, exc, state.services)
    except StaleGenerationAttempt as exc:
        await failure.handle_stale_attempt(state, exc, state.services)
    except TaskCancelled as exc:
        await failure.handle_cancel(state, exc, state.services)
    except Exception as exc:  # noqa: BLE001
        await failure.handle_generation_exception(
            state,
            exc,
            state.services,
        )
    finally:
        await _cleanup_generation_run(state)


def _new_run_state(
    ctx: dict[str, Any],
    task_id: str,
    services: RunGenerationDeps,
) -> GenerationRunState:
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    task_start = asyncio.get_event_loop().time()
    return GenerationRunState(
        services=services,
        ctx=ctx,
        task_id=task_id,
        redis=redis,
        worker_id=worker_id,
        lease_token=f"{worker_id}:{new_uuid7()}",
        task_start=task_start,
        task_deadline=task_start + GENERATION_RUN_TIMEOUT_S,
        channel=task_channel(task_id),
        trace_id=f"gen_{task_id}",
        stage_timer=StageTimer(),
        dispatch_identity=dispatch_identity_from_context(ctx),
    )


async def _prepare_provider_reservation(
    state: GenerationRunState,
) -> bool:
    await _resolve_route(state)
    if not await _resolve_user_runtime_provider(state):
        return False
    _apply_route_constraints(state)
    await _attach_provider_pool(state)
    return await _reserve_provider_slot(state)


async def _resolve_route(state: GenerationRunState) -> None:
    try:
        state.raw_image_route = await state.services.provider.resolve_primary_route()
    except Exception:  # noqa: BLE001
        state.raw_image_route = "responses"
    state.image_route = state.raw_image_route


async def _resolve_user_runtime_provider(
    state: GenerationRunState,
) -> bool:
    credential_id = state.user_api_credential_id
    if not credential_id:
        return True
    try:
        async with state.services.store.session() as session:
            state.user_runtime_provider = await state.services.credentials.resolve(
                session,
                credential_id,
            )
        purposes = getattr(state.user_runtime_provider, "purposes", ()) or ()
        if "image" not in purposes:
            raise UpstreamError(
                "user API key supplier does not allow image purpose",
                status_code=403,
                error_code="byok_purpose_mismatch",
                payload={"credential_id": credential_id},
            )
    except Exception as exc:  # noqa: BLE001
        await _fail_user_runtime_provider(state, credential_id, exc)
        return False
    if state.raw_image_route == "dual_race":
        state.route_diagnostics.append(
            {
                "route": state.raw_image_route,
                "fallback_route": "responses",
                "reason": "byok_disables_dual_race",
                "byok": True,
            }
        )
        state.image_route = "responses"
    return True


async def _fail_user_runtime_provider(
    state: GenerationRunState,
    credential_id: str,
    exc: Exception,
) -> None:
    byok_error = state.services.credentials.classify_error(exc)[1] or "invalid_api_key"
    await state.services.credentials.record_runtime_error(credential_id, exc)
    error_code = state.services.credentials.generation_error_code(byok_error)
    error_message = state.services.credentials.error_message(byok_error)
    try:
        async with state.services.store.session() as session:
            await _persist_user_runtime_failure(
                state,
                session,
                error_code,
                error_message,
            )
    except StaleGenerationAttempt:
        state.task_outcome = "stale_attempt"
        return
    await _publish_queued_failure(state, error_code, error_message)
    state.task_outcome = "failed"


async def _persist_user_runtime_failure(
    state: GenerationRunState,
    session: Any,
    error_code: str,
    error_message: str,
) -> None:
    result = await session.execute(
        generation_attempt_update(
            state.task_id,
            state.loaded_attempt,
            statuses=(
                GenerationStatus.QUEUED.value,
                GenerationStatus.RUNNING.value,
            ),
        ).values(
            status=GenerationStatus.FAILED.value,
            progress_stage=GenerationStage.FINALIZING,
            attempt=state.loaded_attempt,
            finished_at=datetime.now(timezone.utc),
            error_code=error_code,
            error_message=error_message,
        )
    )
    ensure_generation_updated(
        result,
        state.task_id,
        state.loaded_attempt,
    )
    message = await session.get(Message, state.message_id)
    if message is not None and message.status != MessageStatus.CANCELED:
        message.status = MessageStatus.FAILED
    generation = await session.get(Generation, state.task_id)
    if generation is not None:
        await release_or_settle_generation(
            state.services.billing,
            session,
            generation,
            reason=error_code,
        )
    await session.commit()
    await state.services.billing.flush_after_commit(session)


def _apply_route_constraints(state: GenerationRunState) -> None:
    state.requires_mask_provider = (
        bool(state.mask_image_id) and state.action == GenerationAction.EDIT
    )
    if state.requires_mask_provider and state.raw_image_route in {
        "dual_race",
        "responses",
    }:
        state.route_diagnostics.append(
            {
                "route": state.raw_image_route,
                "fallback_route": "generations",
                "reason": "mask_requires_generations_endpoint",
                "has_mask": True,
            }
        )
        state.image_route = "image2"
    state.is_dual_race = (
        state.raw_image_route == "dual_race" and state.image_route == "dual_race"
    )
    state.endpoint_kind = (
        "generations"
        if state.requires_mask_provider
        else None
        if state.is_dual_race
        else state.services.provider.endpoint_kind_for_engine(state.image_route)
    )


async def _attach_provider_pool(state: GenerationRunState) -> None:
    try:
        from ...provider_pool import get_pool

        provider_pool = await get_pool()
        provider_pool.attach_redis(state.redis)
    except Exception:  # noqa: BLE001
        logger.debug("provider_pool attach_redis failed", exc_info=True)


async def _reserve_provider_slot(state: GenerationRunState) -> bool:
    queue_metadata = generation_queue_metadata(
        upstream_request=state.gen_upstream_request_snapshot,
        action=state.action,
        size_requested=state.size_requested,
        mask_image_id=state.mask_image_id,
        created_at=state.gen_created_at,
    )
    provider_delay = await _reserve_provider(state, queue_metadata)
    if state.reserved_provider is None:
        await _publish_provider_wait(state, provider_delay)
        return False
    state.reserved_provider_name = redis_text(
        getattr(state.reserved_provider, "name", None)
    )
    state.upstream_provider_label = (
        "dual_race"
        if is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    )
    return True


async def _reserve_provider(
    state: GenerationRunState,
    queue_metadata: dict[str, Any],
) -> int:
    provider_delay = 0
    demand = state.resource_demand or generation_resource_demand(
        pixel_count=None,
        dual_race=state.is_dual_race,
    )
    permit = WeightedPermit(
        task_id=state.task_id,
        attempt=state.attempt,
        revision=(
            state.dispatch_identity.revision
            if state.dispatch_identity is not None
            else 0
        ),
        demand=demand,
        user_id=state.user_id,
    )
    try:
        started = time.monotonic()
        state.reserved_provider = await reserve_image_queue_slot(
            state.redis,
            state.task_id,
            dual_race=state.is_dual_race,
            endpoint_kind=state.endpoint_kind,
            requires_mask=state.requires_mask_provider,
            provider_override=state.user_runtime_provider,
            queue_lane=queue_metadata.get("queue_lane"),
            size_bucket=queue_metadata.get("size_bucket"),
            cost_class=queue_metadata.get("cost_class"),
            weighted_permit=permit,
            services=state.services,
        )
        if state.reserved_provider is not None:
            state.weighted_permit = permit
        state.stage_timer.add_elapsed("provider_wait", started)
    except UpstreamError as exc:
        error_code = getattr(exc, "error_code", None)
        if error_code == EC.NO_MASK_CAPABLE_PROVIDER.value:
            raise
        if error_code != EC.ALL_ACCOUNTS_FAILED.value:
            raise
        provider_delay = IMAGE_PROVIDER_UNAVAILABLE_RETRY_S
        await state.redis.set(
            image_queue_not_before_key(state.task_id),
            str(time.time() + provider_delay),
            ex=provider_delay + IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
        )
        await enqueue_generation_once(
            state.redis,
            state.task_id,
            attempt=state.attempt,
            defer_by=provider_delay,
            replace_dispatch=state.dispatch_identity,
            services=state.services,
        )
    return provider_delay


async def _publish_provider_wait(
    state: GenerationRunState,
    provider_delay: int,
) -> None:
    await state.services.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_QUEUED,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": GenerationStage.QUEUED.value,
            "substage": ("waiting_provider" if provider_delay else "waiting_queue"),
            "reason": (
                "image_provider_unavailable"
                if provider_delay
                else "image_queue_waiting"
            ),
        },
    )
    state.task_outcome = "queued"


async def _start_generation_attempt(state: GenerationRunState) -> bool:
    if not await _acquire_generation_lease(state):
        return False
    if not await _transition_generation_running(state):
        return False
    try:
        await _publish_generation_started(state)
        return True
    except BaseException:
        await _cleanup_failed_setup(state)
        raise


async def _acquire_generation_lease(state: GenerationRunState) -> bool:
    try:
        await acquire_lease(
            state.redis,
            state.task_id,
            state.lease_token,
        )
        return True
    except LeaseLost as exc:
        logger.info(
            "generation lease already held task=%s err=%s",
            state.task_id,
            exc,
        )
        state.task_outcome = "lease_held"
        await release_image_queue_slot(
            state.redis,
            task_id=state.task_id,
            provider_name=state.reserved_provider_name,
            services=state.services,
        )
        return False


async def _transition_generation_running(
    state: GenerationRunState,
) -> bool:
    async with state.services.store.session() as session:
        current = (
            await session.execute(
                select(Generation)
                .where(Generation.id == state.task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if current is None or is_generation_terminal(current.status):
            await _release_stale_claim(state)
            return False
        state.attempt, may_run = bounded_next_attempt(current.attempt)
        if not may_run:
            await _release_stale_claim(state)
            return False
        running_request = _running_upstream_request(state, current)
        started_at = datetime.now(timezone.utc)
        state.queue_metadata_payload = generation_queue_metadata(
            upstream_request=running_request,
            action=current.action,
            size_requested=current.size_requested,
            mask_image_id=current.mask_image_id,
            created_at=current.created_at,
            started_at=started_at,
            finished_at=current.finished_at,
            upstream_pixels=current.upstream_pixels,
            now=started_at,
        )
        running_request = merge_queue_metadata(
            running_request,
            state.queue_metadata_payload,
        )
        state.gen_upstream_request_snapshot = dict(running_request)
        await _commit_running_transition(
            state,
            session,
            current,
            running_request,
            started_at,
        )
    return True


def _running_upstream_request(
    state: GenerationRunState,
    current: Any,
) -> dict[str, Any]:
    request = (
        dict(current.upstream_request)
        if isinstance(current.upstream_request, dict)
        else {}
    )
    state.lease_reacquired = current.error_code == "lease_lost"
    request["trace_id"] = state.trace_id
    request["upstream_route"] = state.image_route
    if state.route_diagnostics:
        request["route_diagnostics"] = state.route_diagnostics[:12]
    if state.is_dual_race:
        request.pop("provider", None)
        request.pop("actual_provider", None)
    elif state.upstream_provider_label:
        request["provider"] = state.upstream_provider_label
    return request


async def _commit_running_transition(
    state: GenerationRunState,
    session: Any,
    current: Any,
    running_request: dict[str, Any],
    started_at: datetime,
) -> None:
    result = await session.execute(
        update(Generation)
        .where(
            Generation.id == state.task_id,
            Generation.attempt == current.attempt,
            Generation.status == GenerationStatus.QUEUED.value,
        )
        .values(
            status=GenerationStatus.RUNNING.value,
            progress_stage=GenerationStage.RENDERING,
            started_at=started_at,
            attempt=state.attempt,
            upstream_request=running_request,
            error_code=None,
            error_message=None,
        )
    )
    try:
        ensure_generation_updated(
            result,
            state.task_id,
            current.attempt,
        )
    except StaleGenerationAttempt:
        await _release_stale_claim(state)
        raise
    await session.commit()


async def _release_stale_claim(state: GenerationRunState) -> None:
    state.task_outcome = "stale_attempt"
    await release_image_queue_slot(
        state.redis,
        task_id=state.task_id,
        provider_name=state.reserved_provider_name,
        services=state.services,
    )
    await release_lease(
        state.redis,
        state.task_id,
        state.lease_token,
    )


async def _publish_generation_started(state: GenerationRunState) -> None:
    state.renewer = asyncio.create_task(
        lease_renewer(
            state.redis,
            state.task_id,
            state.lease_token,
            state.lease_lost,
            extra_lease_keys=[image_task_provider_key(state.task_id)],
            image_provider_name=state.reserved_provider_name,
            weighted_permit=state.weighted_permit,
        )
    )
    await state.services.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_STARTED,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "attempt": state.attempt,
            "provider": (None if state.is_dual_race else state.upstream_provider_label),
            "route": state.image_route,
            "lease_reacquired": bool(state.lease_reacquired),
            **state.queue_metadata_payload,
        },
    )
    if state.lease_reacquired:
        await _publish_lease_reacquired(state)
    await _initialize_inflight_snapshot(state)
    await kick_image_queue(state.redis, services=state.services)


async def _publish_lease_reacquired(state: GenerationRunState) -> None:
    await state.services.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": GenerationStage.QUEUED.value,
            "substage": LEASE_REACQUIRED_SUBSTAGE,
        },
    )


async def _initialize_inflight_snapshot(state: GenerationRunState) -> None:
    fields = {
        "mode": "dual_race" if state.is_dual_race else "single",
        "route": state.image_route or "",
        "task_id": state.task_id,
    }
    if not state.is_dual_race and state.reserved_provider_name:
        fields["provider"] = state.reserved_provider_name
    await inflight_set_fields(
        state.redis,
        state.task_id,
        fields,
        services=state.services,
    )


async def _cleanup_failed_setup(state: GenerationRunState) -> None:
    state.task_outcome = "setup_failed"
    await cancel_renewer_task(state.renewer)
    state.renewer = None
    cleanup = asyncio.ensure_future(
        release_generation_runtime_resources(
            state.redis,
            task_id=state.task_id,
            lease_token=state.lease_token,
            provider_name=state.reserved_provider_name,
            weighted_permit=state.weighted_permit,
            clear_avoided_providers=True,
            services=state.services,
        )
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        cleanup.add_done_callback(
            lambda _task: logger.debug(
                "generation late setup cleanup finished task=%s",
                state.task_id,
            )
        )


async def _cleanup_generation_run(state: GenerationRunState) -> None:
    if state.renewer is not None:
        await cancel_renewer_task(state.renewer)
    cleanup = asyncio.ensure_future(_critical_release_cleanup(state))
    cancelled = False
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        cancelled = True
        cleanup.add_done_callback(
            lambda _task: logger.debug(
                "generation late critical cleanup finished task=%s",
                state.task_id,
            )
        )
    _observe_task_duration(state)
    if cancelled:
        raise asyncio.CancelledError()


async def _critical_release_cleanup(state: GenerationRunState) -> None:
    await consume_image_iter_close_result(
        state.image_iter,
        task_id=state.task_id,
    )
    if state.resource_lease is None:
        state.resource_lease = GenerationResourceLease(
            services=state.services,
            redis=state.redis,
            task_id=state.task_id,
            lease_token=state.lease_token,
            provider_name=state.reserved_provider_name,
            weighted_permit=state.weighted_permit,
            clear_avoided_providers=state.task_outcome != "retry",
        )
    await state.resource_lease.close()


def _observe_task_duration(state: GenerationRunState) -> None:
    try:
        duration = asyncio.get_event_loop().time() - state.task_start
        task_duration_seconds.labels(
            kind="generation",
            outcome=safe_outcome(state.task_outcome),
        ).observe(duration)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "_build_image_iterator",
    "_fail_queued_generation",
    "run_generation",
]
