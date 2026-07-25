from __future__ import annotations

from .runtime import generation_ports
import asyncio
from datetime import datetime, timezone
from typing import Any

from . import failure, success
from .progress import ImageProgressPublisher
from .queue_claim import GenerationResourceLease
from .runtime import GenerationRunState


async def run_generation(ctx: dict[str, Any], task_id: str) -> None:
    """Run one ARQ image-generation task through explicit lifecycle phases."""
    state = _new_run_state(ctx, task_id)
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
        await success.finalize_generation_success(state, generation_ports())
    except generation_ports()._LeaseLost as exc:
        await failure.handle_lease_lost(state, exc, generation_ports())
    except generation_ports()._StaleGenerationAttempt as exc:
        await failure.handle_stale_attempt(state, exc, generation_ports())
    except generation_ports()._TaskCancelled as exc:
        await failure.handle_cancel(state, exc, generation_ports())
    except Exception as exc:  # noqa: BLE001
        await failure.handle_generation_exception(
            state,
            exc,
            generation_ports(),
        )
    finally:
        await _cleanup_generation_run(state)


def _new_run_state(
    ctx: dict[str, Any],
    task_id: str,
) -> GenerationRunState:
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    task_start = asyncio.get_event_loop().time()
    return GenerationRunState(
        ctx=ctx,
        task_id=task_id,
        redis=redis,
        worker_id=worker_id,
        lease_token=f"{worker_id}:{generation_ports().new_uuid7()}",
        task_start=task_start,
        task_deadline=task_start + generation_ports()._RUN_GENERATION_TIMEOUT_S,
        channel=generation_ports().task_channel(task_id),
        trace_id=f"gen_{task_id}",
        stage_timer=generation_ports()._StageTimer(),
    )


async def _load_initial_generation(state: GenerationRunState) -> bool:
    async with generation_ports().SessionLocal() as session:
        generation = await _claim_generation_row(session, state.task_id)
        if generation is None:
            return False
        if _generation_cannot_start(generation):
            return False
        _load_generation_fields(state, generation)
        if not await _validate_conversation(state, session):
            return False
        if not await _validate_primary_input(state, session):
            return False
        existing = await generation_ports()._find_existing_generated_image(
            session,
            task_id=state.task_id,
            user_id=state.user_id,
        )
        if existing is not None:
            state.task_outcome = (
                await generation_ports()._settle_existing_generated_image(
                    session,
                    redis=state.redis,
                    task_id=state.task_id,
                    user_id=state.user_id,
                    message_id=state.message_id,
                    generation=generation,
                    existing_image=existing,
                    task_started_at=state.task_start,
                )
            )
            return False
        state.attempt, may_run = generation_ports()._bounded_next_attempt(
            generation.attempt
        )
        if not may_run:
            await _fail_max_attempts(state, session)
            return False
    return True


async def _claim_generation_row(session: Any, task_id: str) -> Any | None:
    generation = (
        await session.execute(
            generation_ports()
            .select(generation_ports().Generation)
            .where(generation_ports().Generation.id == task_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if generation is not None:
        return generation
    existing_id = (
        await session.execute(
            generation_ports()
            .select(generation_ports().Generation.id)
            .where(generation_ports().Generation.id == task_id)
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        generation_ports().logger.info(
            "generation initial claim skipped locked row task_id=%s",
            task_id,
        )
    else:
        generation_ports().logger.warning("generation not found task_id=%s", task_id)
    return None


def _generation_cannot_start(generation: Any) -> bool:
    if generation_ports().is_generation_terminal(generation.status):
        generation_ports().logger.info(
            "generation already terminal task_id=%s status=%s",
            generation.id,
            generation.status,
        )
        return True
    if generation.status == generation_ports().GenerationStatus.RUNNING.value:
        generation_ports().logger.info(
            "generation already running task_id=%s", generation.id
        )
        return True
    return False


def _load_generation_fields(
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
    state.trace_id = generation_ports()._generation_trace_id(
        state.task_id,
        state.gen_upstream_request_snapshot,
    )
    state.stage_timer.set_ms(
        "queue_wait",
        generation_ports()._queue_wait_ms(state.gen_created_at),
    )
    state.image_request_options = generation_ports()._image_request_options(
        generation.upstream_request,
        size=state.size_requested,
    )


async def _validate_conversation(
    state: GenerationRunState,
    session: Any,
) -> bool:
    try:
        await generation_ports()._ensure_generation_conversation_alive(
            session,
            message_id=state.message_id,
            user_id=state.user_id,
        )
        return True
    except generation_ports()._TaskCancelled as exc:
        await _cancel_queued_generation(state, session, str(exc))
        return False


async def _cancel_queued_generation(
    state: GenerationRunState,
    session: Any,
    message: str,
) -> None:
    result = await session.execute(
        generation_ports()
        ._generation_attempt_update(
            state.task_id,
            state.generation.attempt,
            statuses=(generation_ports().GenerationStatus.QUEUED.value,),
        )
        .values(
            status=generation_ports().GenerationStatus.CANCELED.value,
            progress_stage=generation_ports().GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            error_code=generation_ports().EC.CANCELLED.value,
            error_message=message,
        )
    )
    generation_ports()._ensure_generation_updated(
        result,
        state.task_id,
        state.generation.attempt,
    )
    row = await session.get(generation_ports().Message, state.message_id)
    if row is not None and row.status not in (
        generation_ports().MessageStatus.SUCCEEDED,
        generation_ports().MessageStatus.FAILED,
        generation_ports().MessageStatus.CANCELED,
    ):
        row.status = generation_ports().MessageStatus.FAILED
    await generation_ports().worker_billing.release_generation(
        session,
        state.generation,
        reason=generation_ports().EC.CANCELLED.value,
    )
    await session.commit()
    await generation_ports().worker_billing.flush_balance_cache_refreshes(session)
    await _publish_queued_failure(
        state,
        generation_ports().EC.CANCELLED.value,
        message,
    )
    state.task_outcome = "failed"


async def _validate_primary_input(
    state: GenerationRunState,
    session: Any,
) -> bool:
    if generation_ports()._primary_input_image_id_valid(
        state.primary_input_image_id,
        state.input_image_ids,
    ):
        return True
    await _fail_queued_generation(
        state,
        session,
        code=generation_ports().EC.INVALID_PARAM.value,
        message="primary_input_image_id must be included in input_image_ids",
        next_attempt=None,
    )
    return False


async def _fail_max_attempts(
    state: GenerationRunState,
    session: Any,
) -> None:
    await _fail_queued_generation(
        state,
        session,
        code="max_attempts_exceeded",
        message=f"generation exceeded max attempts ({generation_ports()._MAX_ATTEMPTS})",
        next_attempt=state.attempt,
    )
    _observe_task_duration(state)


async def _fail_queued_generation(
    state: GenerationRunState,
    session: Any,
    *,
    code: str,
    message: str,
    next_attempt: int | None,
) -> None:
    values: dict[str, Any] = {
        "status": generation_ports().GenerationStatus.FAILED.value,
        "progress_stage": generation_ports().GenerationStage.FINALIZING,
        "finished_at": datetime.now(timezone.utc),
        "error_code": code,
        "error_message": message,
    }
    if next_attempt is not None:
        values["attempt"] = next_attempt
    result = await session.execute(
        generation_ports()
        ._generation_attempt_update(
            state.task_id,
            state.generation.attempt,
            statuses=(generation_ports().GenerationStatus.QUEUED.value,),
        )
        .values(**values)
    )
    generation_ports()._ensure_generation_updated(
        result,
        state.task_id,
        state.generation.attempt,
    )
    row = await session.get(generation_ports().Message, state.message_id)
    if row is not None and row.status != generation_ports().MessageStatus.CANCELED:
        row.status = generation_ports().MessageStatus.FAILED
    generation = await session.get(generation_ports().Generation, state.task_id)
    if generation is not None:
        await generation_ports().worker_billing.release_generation(
            session,
            generation,
            reason=code,
        )
    await session.commit()
    await generation_ports().worker_billing.flush_balance_cache_refreshes(session)
    await _publish_queued_failure(state, code, message)
    state.task_outcome = "failed"


async def _publish_queued_failure(
    state: GenerationRunState,
    code: str,
    message: str,
) -> None:
    await generation_ports().publish_event(
        state.redis,
        state.user_id,
        generation_ports().task_channel(state.task_id),
        generation_ports().EV_GEN_FAILED,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "code": code,
            "message": message,
            "retriable": False,
        },
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
        state.raw_image_route = await generation_ports()._resolve_image_primary_route()
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
        async with generation_ports().SessionLocal() as session:
            state.user_runtime_provider = (
                await generation_ports().resolve_user_credential_runtime(
                    session,
                    credential_id,
                )
            )
        purposes = getattr(state.user_runtime_provider, "purposes", ()) or ()
        if "image" not in purposes:
            raise generation_ports().UpstreamError(
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
    byok_error = (
        generation_ports().classify_user_credential_error(exc)[1] or "invalid_api_key"
    )
    await generation_ports().record_user_credential_runtime_error(credential_id, exc)
    error_code = generation_ports().byok_error_to_generation_code(byok_error)
    error_message = generation_ports().byok_error_message(byok_error)
    try:
        async with generation_ports().SessionLocal() as session:
            await _persist_user_runtime_failure(
                state,
                session,
                error_code,
                error_message,
            )
    except generation_ports()._StaleGenerationAttempt:
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
        generation_ports()
        ._generation_attempt_update(
            state.task_id,
            state.loaded_attempt,
            statuses=(
                generation_ports().GenerationStatus.QUEUED.value,
                generation_ports().GenerationStatus.RUNNING.value,
            ),
        )
        .values(
            status=generation_ports().GenerationStatus.FAILED.value,
            progress_stage=generation_ports().GenerationStage.FINALIZING,
            attempt=state.loaded_attempt,
            finished_at=datetime.now(timezone.utc),
            error_code=error_code,
            error_message=error_message,
        )
    )
    generation_ports()._ensure_generation_updated(
        result,
        state.task_id,
        state.loaded_attempt,
    )
    message = await session.get(generation_ports().Message, state.message_id)
    if (
        message is not None
        and message.status != generation_ports().MessageStatus.CANCELED
    ):
        message.status = generation_ports().MessageStatus.FAILED
    generation = await session.get(generation_ports().Generation, state.task_id)
    if generation is not None:
        await generation_ports().worker_billing.release_generation(
            session,
            generation,
            reason=error_code,
        )
    await session.commit()
    await generation_ports().worker_billing.flush_balance_cache_refreshes(session)


def _apply_route_constraints(state: GenerationRunState) -> None:
    state.requires_mask_provider = (
        bool(state.mask_image_id)
        and state.action == generation_ports().GenerationAction.EDIT
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
        else generation_ports()._image_endpoint_kind_for_engine(state.image_route)
    )


async def _attach_provider_pool(state: GenerationRunState) -> None:
    try:
        from ...provider_pool import get_pool

        provider_pool = await get_pool()
        provider_pool.attach_redis(state.redis)
    except Exception:  # noqa: BLE001
        generation_ports().logger.debug(
            "provider_pool attach_redis failed", exc_info=True
        )


async def _reserve_provider_slot(state: GenerationRunState) -> bool:
    queue_metadata = generation_ports().generation_queue_metadata(
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
    state.reserved_provider_name = generation_ports()._redis_text(
        getattr(state.reserved_provider, "name", None)
    )
    state.upstream_provider_label = (
        "dual_race"
        if generation_ports()._is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    )
    return True


async def _reserve_provider(
    state: GenerationRunState,
    queue_metadata: dict[str, Any],
) -> int:
    provider_delay = 0
    try:
        started = generation_ports().time.monotonic()
        state.reserved_provider = await generation_ports()._reserve_image_queue_slot(
            state.redis,
            state.task_id,
            dual_race=state.is_dual_race,
            endpoint_kind=state.endpoint_kind,
            requires_mask=state.requires_mask_provider,
            provider_override=state.user_runtime_provider,
            queue_lane=queue_metadata.get("queue_lane"),
            size_bucket=queue_metadata.get("size_bucket"),
            cost_class=queue_metadata.get("cost_class"),
        )
        state.stage_timer.add_elapsed("provider_wait", started)
    except generation_ports().UpstreamError as exc:
        error_code = getattr(exc, "error_code", None)
        if error_code == generation_ports().EC.NO_MASK_CAPABLE_PROVIDER.value:
            raise
        if error_code != generation_ports().EC.ALL_ACCOUNTS_FAILED.value:
            raise
        provider_delay = generation_ports()._IMAGE_PROVIDER_UNAVAILABLE_RETRY_S
        await state.redis.set(
            generation_ports()._image_queue_not_before_key(state.task_id),
            str(generation_ports().time.time() + provider_delay),
            ex=provider_delay + generation_ports()._IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
        )
        await generation_ports()._enqueue_generation_once(
            state.redis,
            state.task_id,
            defer_by=provider_delay,
        )
    return provider_delay


async def _publish_provider_wait(
    state: GenerationRunState,
    provider_delay: int,
) -> None:
    await generation_ports()._clear_image_queue_enqueue_dedupe(
        state.redis,
        state.task_id,
    )
    await generation_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        generation_ports().EV_GEN_QUEUED,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": generation_ports().GenerationStage.QUEUED.value,
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
        await generation_ports()._acquire_lease(
            state.redis,
            state.task_id,
            state.lease_token,
        )
        return True
    except generation_ports()._LeaseLost as exc:
        generation_ports().logger.info(
            "generation lease already held task=%s err=%s",
            state.task_id,
            exc,
        )
        state.task_outcome = "lease_held"
        await generation_ports()._release_image_queue_slot(
            state.redis,
            task_id=state.task_id,
            provider_name=state.reserved_provider_name,
        )
        return False


async def _transition_generation_running(
    state: GenerationRunState,
) -> bool:
    async with generation_ports().SessionLocal() as session:
        current = (
            await session.execute(
                generation_ports()
                .select(generation_ports().Generation)
                .where(generation_ports().Generation.id == state.task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if current is None or generation_ports().is_generation_terminal(current.status):
            await _release_stale_claim(state)
            return False
        state.attempt, may_run = generation_ports()._bounded_next_attempt(
            current.attempt
        )
        if not may_run:
            await _release_stale_claim(state)
            return False
        running_request = _running_upstream_request(state, current)
        started_at = datetime.now(timezone.utc)
        state.queue_metadata_payload = generation_ports().generation_queue_metadata(
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
        running_request = generation_ports().merge_queue_metadata(
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
        generation_ports()
        .update(generation_ports().Generation)
        .where(
            generation_ports().Generation.id == state.task_id,
            generation_ports().Generation.attempt == current.attempt,
            generation_ports().Generation.status
            == generation_ports().GenerationStatus.QUEUED.value,
        )
        .values(
            status=generation_ports().GenerationStatus.RUNNING.value,
            progress_stage=generation_ports().GenerationStage.RENDERING,
            started_at=started_at,
            attempt=state.attempt,
            upstream_request=running_request,
            error_code=None,
            error_message=None,
        )
    )
    try:
        generation_ports()._ensure_generation_updated(
            result,
            state.task_id,
            current.attempt,
        )
    except generation_ports()._StaleGenerationAttempt:
        await _release_stale_claim(state)
        raise
    await session.commit()


async def _release_stale_claim(state: GenerationRunState) -> None:
    state.task_outcome = "stale_attempt"
    await generation_ports()._release_image_queue_slot(
        state.redis,
        task_id=state.task_id,
        provider_name=state.reserved_provider_name,
    )
    await generation_ports()._release_lease(
        state.redis,
        state.task_id,
        state.lease_token,
    )


async def _publish_generation_started(state: GenerationRunState) -> None:
    state.renewer = asyncio.create_task(
        generation_ports()._lease_renewer(
            state.redis,
            state.task_id,
            state.lease_token,
            state.lease_lost,
            extra_lease_keys=[
                generation_ports()._image_task_provider_key(state.task_id)
            ],
            image_provider_name=state.reserved_provider_name,
        )
    )
    await generation_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        generation_ports().EV_GEN_STARTED,
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
    await generation_ports()._kick_image_queue(state.redis)


async def _publish_lease_reacquired(state: GenerationRunState) -> None:
    await generation_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        generation_ports().EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": generation_ports().GenerationStage.QUEUED.value,
            "substage": generation_ports()._LEASE_REACQUIRED_SUBSTAGE,
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
    await generation_ports()._inflight_set_fields(
        state.redis,
        state.task_id,
        fields,
    )


async def _cleanup_failed_setup(state: GenerationRunState) -> None:
    state.task_outcome = "setup_failed"
    await generation_ports()._cancel_renewer_task(state.renewer)
    state.renewer = None
    cleanup = asyncio.ensure_future(
        generation_ports()._release_generation_runtime_resources(
            state.redis,
            task_id=state.task_id,
            lease_token=state.lease_token,
            provider_name=state.reserved_provider_name,
            clear_avoided_providers=True,
        )
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        cleanup.add_done_callback(
            lambda _task: generation_ports().logger.debug(
                "generation late setup cleanup finished task=%s",
                state.task_id,
            )
        )


def _initialize_execution_state(state: GenerationRunState) -> None:
    state.has_partial = False
    state.image_iter = None
    state.provider_attempt_log.clear()
    state.upstream_duration_ms = None
    state.requested_image_count = generation_ports()._image_requested_count(
        state.gen_upstream_request_snapshot
    )
    state.batch_extra_pairs.clear()
    state.requested_params_for_diag = (
        generation_ports()._image_requested_params_snapshot(
            state.gen_upstream_request_snapshot,
            size=state.size_requested,
            aspect_ratio=state.aspect_ratio,
            action=state.action,
            input_count=len(state.input_image_ids),
            has_mask=bool(state.mask_image_id),
        )
    )


async def _prepare_upstream_request(state: GenerationRunState) -> None:
    started = generation_ports().time.monotonic()
    state.resolved = _resolve_generation_size(state)
    state.image_request_options = generation_ports()._image_request_options(
        state.generation.upstream_request,
        size=state.resolved.size,
    )
    state.prompt_for_upstream = generation_ports()._prompt_with_aspect_ratio_constraint(
        state.prompt,
        state.aspect_ratio,
    )
    await _load_references_and_mask(state)
    _normalize_mask(state)
    state.stage_timer.add_elapsed("normalize", started)
    await _publish_stream_started(state)
    state.progress_publisher = ImageProgressPublisher(
        state,
        generation_ports(),
    )


def _resolve_generation_size(state: GenerationRunState) -> Any:
    fixed_size = (
        state.size_requested
        if state.size_requested and "x" in state.size_requested
        else None
    )
    try:
        resolved = generation_ports().resolve_size(
            state.aspect_ratio,
            "fixed",
            fixed_size,
        )
        generation_ports()._validate_resolved_size(
            resolved.size,
            state.aspect_ratio,
            validate_aspect_ratio=fixed_size is None,
        )
        return resolved
    except ValueError as exc:
        raise generation_ports().UpstreamError(
            f"invalid size_requested: {exc}",
            status_code=400,
            error_code=generation_ports().EC.INVALID_VALUE.value,
            payload={
                "size_requested": state.size_requested,
                "aspect_ratio": state.aspect_ratio,
            },
        ) from exc


async def _load_references_and_mask(state: GenerationRunState) -> None:
    async with generation_ports().SessionLocal() as session:
        state.references = await generation_ports()._load_reference_images(
            session,
            state.input_image_ids,
        )
        mask = None
        if (
            state.mask_image_id
            and state.action == generation_ports().GenerationAction.EDIT
        ):
            mask = await generation_ports()._load_mask_image(
                session,
                state.mask_image_id,
            )
    state.ref_for_body = (
        state.references
        if state.action == generation_ports().GenerationAction.EDIT
        else []
    )
    state.mask_bytes = mask


def _normalize_mask(state: GenerationRunState) -> None:
    state.inpaint_size_override = None
    if state.mask_bytes is None or not state.ref_for_body:
        return
    reference_bytes = state.ref_for_body[0][1]
    state.mask_bytes = generation_ports()._resize_mask_to_reference(
        state.mask_bytes,
        reference_bytes,
    )
    reference_size = generation_ports()._reference_pixel_size(reference_bytes)
    if reference_size is not None:
        state.inpaint_size_override = generation_ports()._inpaint_size_from_reference(
            *reference_size
        )


async def _publish_stream_started(state: GenerationRunState) -> None:
    await generation_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        generation_ports().EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "stage": generation_ports().GenerationStage.RENDERING.value,
            "substage": generation_ports().GenerationStage.STREAM_STARTED.value,
        },
    )


async def _dispatch_upstream_request(state: GenerationRunState) -> None:
    async with asyncio.timeout_at(state.task_deadline):
        await _raise_if_pre_upstream_interrupted(state)
        with generation_ports()._tracer.start_as_current_span(
            "upstream.generate_image"
        ) as span:
            _annotate_upstream_span(state, span)
            try:
                await _call_upstream(state)
                generation_ports().upstream_calls_total.labels(
                    kind="generation",
                    outcome="ok",
                ).inc()
            except Exception:
                generation_ports().upstream_calls_total.labels(
                    kind="generation",
                    outcome="error",
                ).inc()
                raise


async def _raise_if_pre_upstream_interrupted(
    state: GenerationRunState,
) -> None:
    if state.lease_lost.is_set():
        raise generation_ports()._LeaseLost("generation lease renewer failed")
    if await generation_ports()._is_cancelled(state.redis, state.task_id):
        raise generation_ports()._TaskCancelled("cancelled before upstream request")


def _annotate_upstream_span(state: GenerationRunState, span: Any) -> None:
    try:
        span.set_attribute("lumen.task_id", state.task_id)
        span.set_attribute("lumen.action", state.action)
        span.set_attribute(
            "lumen.size",
            state.inpaint_size_override or state.resolved.size,
        )
        if state.inpaint_size_override:
            span.set_attribute(
                "lumen.size_requested",
                state.resolved.size,
            )
        if state.reserved_provider_name:
            span.set_attribute(
                "lumen.provider",
                state.reserved_provider_name,
            )
    except Exception:  # noqa: BLE001
        pass


async def _call_upstream(state: GenerationRunState) -> None:
    retry_token = generation_ports().push_image_retry_attempt(state.attempt)
    trace_token = generation_ports().push_image_trace_id(state.trace_id)
    quota_token = generation_ports().push_image_quota_context(
        state.task_id,
        state.attempt,
    )
    started = generation_ports().time.monotonic()
    try:
        state.image_iter = _build_image_iterator(state)
        first_pair = await generation_ports()._anext_image_with_guards(
            state.image_iter,
            state.lease_lost,
            redis=state.redis,
            task_id=state.task_id,
        )
    finally:
        generation_ports().pop_image_quota_context(quota_token)
        generation_ports().pop_image_trace_id(trace_token)
        generation_ports().pop_image_retry_attempt(retry_token)
    if first_pair is None:
        raise generation_ports().UpstreamError(
            "upstream image generator yielded no result",
            error_code=generation_ports().EC.NO_IMAGE_RETURNED.value,
            status_code=200,
        )
    state.b64_result, state.revised_prompt = first_pair
    state.upstream_duration_ms = int(
        max(0.0, generation_ports().time.monotonic() - started) * 1000
    )
    state.stage_timer.set_ms("render", state.upstream_duration_ms)
    _record_winner_provider(state)
    await _consume_batch_extra_pairs(state)


def _build_image_iterator(state: GenerationRunState) -> Any:
    options = state.image_request_options
    provider_override = None if state.is_dual_race else state.reserved_provider
    common = {
        "prompt": state.prompt_for_upstream,
        "quality": str(options["render_quality"]),
        "output_format": str(options["output_format"]),
        "output_compression": options.get("output_compression"),
        "background": str(options["background"]),
        "moderation": str(options["moderation"]),
        "n": state.requested_image_count,
        "model": str(options["responses_model"]),
        "progress_callback": state.progress_publisher,
        "provider_override": provider_override,
        "user_id": state.user_id,
    }
    if state.action != generation_ports().GenerationAction.EDIT:
        return generation_ports().generate_image(size=state.resolved.size, **common)
    if not state.ref_for_body:
        raise generation_ports().UpstreamError(
            "edit action requires at least one reference image",
            error_code=generation_ports().EC.INVALID_REQUEST_ERROR.value,
            status_code=400,
        )
    return generation_ports().edit_image(
        size=state.inpaint_size_override or state.resolved.size,
        images=[raw for _sha, raw in state.ref_for_body],
        mask=state.mask_bytes,
        **common,
    )


def _record_winner_provider(state: GenerationRunState) -> None:
    event = state.progress_publisher.pop_provider_used_event()
    state.actual_upstream_provider = event.get("provider")
    state.actual_upstream_route = event.get("route")
    state.actual_upstream_source = event.get("source")
    state.actual_upstream_endpoint = event.get("endpoint")


async def _consume_batch_extra_pairs(state: GenerationRunState) -> None:
    if not _should_consume_batch_extras(state):
        return
    for batch_index in range(2, state.requested_image_count + 1):
        extra_pair = await _next_batch_extra_pair(state, batch_index)
        if extra_pair is None:
            break
        state.batch_extra_pairs.append((batch_index, extra_pair))


def _should_consume_batch_extras(state: GenerationRunState) -> bool:
    return bool(
        state.requested_image_count > 1
        and state.image_iter is not None
        and state.actual_upstream_source in {"image2_direct", "image2_edit_direct"}
    )


async def _next_batch_extra_pair(
    state: GenerationRunState,
    batch_index: int,
) -> tuple[str, str | None] | None:
    try:
        pair = await generation_ports()._anext_image_with_guards(
            state.image_iter,
            state.lease_lost,
            redis=state.redis,
            task_id=state.task_id,
        )
    except (
        generation_ports()._LeaseLost,
        generation_ports()._TaskCancelled,
        asyncio.CancelledError,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        generation_ports().logger.warning(
            "image2 n extra iter failed task=%s index=%s err=%r",
            state.task_id,
            batch_index,
            exc,
        )
        return None
    if pair is None:
        generation_ports().logger.warning(
            "image2 n returned fewer images task=%s requested=%s actual=%s",
            state.task_id,
            state.requested_image_count,
            batch_index - 1,
        )
    return pair


async def _cleanup_generation_run(state: GenerationRunState) -> None:
    if state.renewer is not None:
        await generation_ports()._cancel_renewer_task(state.renewer)
    cleanup = asyncio.ensure_future(_critical_release_cleanup(state))
    cancelled = False
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        cancelled = True
        cleanup.add_done_callback(
            lambda _task: generation_ports().logger.debug(
                "generation late critical cleanup finished task=%s",
                state.task_id,
            )
        )
    _observe_task_duration(state)
    if cancelled:
        raise asyncio.CancelledError()


async def _critical_release_cleanup(state: GenerationRunState) -> None:
    await generation_ports()._consume_image_iter_close_result(
        state.image_iter,
        task_id=state.task_id,
    )
    if state.resource_lease is None:
        state.resource_lease = GenerationResourceLease(
            redis=state.redis,
            task_id=state.task_id,
            lease_token=state.lease_token,
            provider_name=state.reserved_provider_name,
            clear_avoided_providers=state.task_outcome != "retry",
        )
    await state.resource_lease.close()


def _observe_task_duration(state: GenerationRunState) -> None:
    try:
        duration = asyncio.get_event_loop().time() - state.task_start
        generation_ports().task_duration_seconds.labels(
            kind="generation",
            outcome=generation_ports().safe_outcome(state.task_outcome),
        ).observe(duration)
    except Exception:  # noqa: BLE001
        pass
