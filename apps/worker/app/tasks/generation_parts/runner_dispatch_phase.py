"""Upstream request preparation and dispatch phase."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_GEN_PROGRESS,
    GenerationAction,
    GenerationErrorCode as EC,
    GenerationStage,
)
from lumen_core.models import Generation
from lumen_core.sizing import resolve_size
from lumen_core.upstream_billing import (
    IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES,
    UPSTREAM_DISPATCH_PROVEN_NO_COST,
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
    UPSTREAM_RESPONSE_HTTP_ATTEMPTS,
    UPSTREAM_RESPONSE_REQUEST_ID,
    UPSTREAM_RESPONSE_STATUS_CODE,
    UPSTREAM_RESPONSE_TRACE_ID,
    has_stable_provider_idempotency_key,
    mark_upstream_dispatch_started,
    mark_upstream_dispatch_proven_no_cost,
    mark_upstream_dispatch_proven_undelivered,
    mark_upstream_response_received,
)

from ...observability import get_tracer, upstream_calls_total
from ...provider_runtime.errors import UpstreamCancelled, UpstreamError
from ...task_cancellation import force_next_cancellation_check
from ...upstream_parts import GeneratedImageResult
from ...upstream_parts.delivery_evidence import dispatch_receipt_reason
from .active_user_fence import lock_active_generation_user
from .bonus_obligation import record_dual_race_bonus_obligation
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .execution_boundary import sidecar_executions_from_request
from .lease import is_cancelled
from .progress import ImageProgressPublisher
from .request_options import (
    image_request_options,
    image_requested_count,
    prompt_with_aspect_ratio_constraint,
    prompt_with_native_transparency_constraint,
    validate_resolved_size,
)
from .retry_state import (
    RUNNING_GENERATION_STATUSES,
    anext_image_with_guards,
    ensure_generation_attempt_current,
    generation_dispatch_requires_unknown_settlement,
    generation_execution_epoch,
    generation_execution_identity,
)
from .run_state import GenerationRunState
from .runner_phase_services import DispatchGenerationServices
from .services import (
    GenerationProviderContext,
    GenerationProviderEditRequest,
    GenerationProviderRequest,
)
from .takeover_checkpoint import persist_generation_takeover_checkpoint


logger = logging.getLogger(f"{__package__}.runner")
tracer = get_tracer("lumen.worker.generation")


class _EpochGuardedProgressPublisher:
    def __init__(
        self,
        state: GenerationRunState,
        publisher: ImageProgressPublisher,
    ) -> None:
        self._state = state
        self._publisher = publisher

    async def __call__(self, event: dict[str, Any]) -> None:
        if event.get("type") == "dispatch_ready":
            await _record_generation_dispatch_ready(self._state)
            return
        if event.get("type") in {"response_ready", "response_received"}:
            await record_generation_upstream_marker(
                self._state,
                response_received=True,
                response_status_code=event.get(UPSTREAM_RESPONSE_STATUS_CODE),
                response_request_id=event.get(UPSTREAM_RESPONSE_REQUEST_ID),
                response_trace_id=event.get(UPSTREAM_RESPONSE_TRACE_ID),
                response_http_attempts=event.get(UPSTREAM_RESPONSE_HTTP_ATTEMPTS),
            )
            return
        if event.get("type") == "dual_race_bonus_ready":
            # The loser has already succeeded upstream. Persist its billing
            # obligation even if this worker just lost the parent lease; UI
            # progress still remains epoch-fenced below.
            await record_dual_race_bonus_obligation(
                self._state,
                event,
                lock_active_user=lock_active_generation_user,
            )
            return
        async with self._state.services.store.session() as session:
            await ensure_generation_attempt_current(
                session,
                self._state.task_id,
                self._state.attempt,
                execution_epoch=generation_execution_epoch(self._state),
            )
        await self._publisher(event)

    def pop_provider_used_event(self) -> dict[str, str]:
        return self._publisher.pop_provider_used_event()


def initialize_execution_state(state: GenerationRunState) -> None:
    state.has_partial = False
    state.image_iter = None
    state.provider_attempt_log.clear()
    state.upstream_duration_ms = None
    state.requested_image_count = image_requested_count(
        state.gen_upstream_request_snapshot
    )
    state.batch_extra_pairs.clear()
    from .diagnostics import image_requested_params_snapshot

    state.requested_params_for_diag = image_requested_params_snapshot(
        state.gen_upstream_request_snapshot,
        size=state.size_requested,
        aspect_ratio=state.aspect_ratio,
        action=state.action,
        input_count=len(state.input_image_ids),
        has_mask=bool(state.mask_image_id),
    )


async def prepare_upstream_request(state: GenerationRunState) -> None:
    started = time.monotonic()
    state.resolved = resolve_generation_size(state)
    state.image_request_options = image_request_options(
        state.generation.upstream_request,
        size=state.resolved.size,
    )
    state.prompt_for_upstream = prompt_with_native_transparency_constraint(
        prompt_with_aspect_ratio_constraint(
            state.prompt,
            state.aspect_ratio,
        ),
        state.image_request_options.get("background"),
    )
    await load_references_and_mask(state)
    normalize_mask(state)
    state.stage_timer.add_elapsed("normalize", started)
    await publish_stream_started(state)
    progress_publisher = ImageProgressPublisher(
        state,
        state.services,
    )
    state.progress_publisher = _EpochGuardedProgressPublisher(
        state,
        progress_publisher,
    )


def resolve_generation_size(state: GenerationRunState) -> Any:
    fixed_size = (
        state.size_requested
        if state.size_requested and "x" in state.size_requested
        else None
    )
    try:
        resolved = resolve_size(
            state.aspect_ratio,
            "fixed",
            fixed_size,
        )
        validate_resolved_size(
            resolved.size,
            state.aspect_ratio,
            validate_aspect_ratio=fixed_size is None,
        )
        return resolved
    except ValueError as exc:
        raise UpstreamError(
            f"invalid size_requested: {exc}",
            status_code=400,
            error_code=EC.INVALID_VALUE.value,
            payload={
                "size_requested": state.size_requested,
                "aspect_ratio": state.aspect_ratio,
            },
        ) from exc


async def load_references_and_mask(state: GenerationRunState) -> None:
    services = DispatchGenerationServices.from_deps(state.services)
    async with services.store.session() as session:
        state.references = await services.provider.load_reference_images(
            session,
            state.input_image_ids,
        )
        mask = None
        if state.mask_image_id and state.action == GenerationAction.EDIT:
            mask = await services.provider.load_mask_image(
                session,
                state.mask_image_id,
            )
    state.ref_for_body = (
        state.references if state.action == GenerationAction.EDIT else []
    )
    state.mask_bytes = mask


def normalize_mask(state: GenerationRunState) -> None:
    services = DispatchGenerationServices.from_deps(state.services)
    state.inpaint_size_override = None
    if state.mask_bytes is None or not state.ref_for_body:
        return
    reference_bytes = state.ref_for_body[0][1]
    state.mask_bytes = services.provider.resize_mask_to_reference(
        state.mask_bytes,
        reference_bytes,
    )
    reference_size = services.provider.reference_pixel_size(reference_bytes)
    if reference_size is not None:
        state.inpaint_size_override = services.provider.inpaint_size_from_reference(
            *reference_size
        )


async def publish_stream_started(state: GenerationRunState) -> None:
    services = DispatchGenerationServices.from_deps(state.services)
    await services.events.publish(
        state.redis,
        state.user_id,
        state.channel,
        EV_GEN_PROGRESS,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
            "execution_epoch": generation_execution_epoch(state),
            "attempt": state.attempt,
            "stage": GenerationStage.RENDERING.value,
            "substage": GenerationStage.STREAM_STARTED.value,
        },
    )


async def dispatch_upstream_request(state: GenerationRunState) -> None:
    async with asyncio.timeout_at(state.task_deadline):
        await raise_if_pre_upstream_interrupted(state)
        await _ensure_generation_user_active(state)
        with tracer.start_as_current_span("upstream.generate_image") as span:
            annotate_upstream_span(state, span)
            try:
                await call_upstream(state)
                upstream_calls_total.labels(
                    kind="generation",
                    outcome="ok",
                ).inc()
            except (Exception, UpstreamCancelled) as exc:
                upstream_calls_total.labels(
                    kind="generation",
                    outcome="error",
                ).inc()
                await _raise_dispatch_failure(state, exc)
                raise


async def _raise_dispatch_failure(
    state: GenerationRunState,
    exc: BaseException,
) -> None:
    if isinstance(exc, TaskCancelled):
        return
    receipt_reason = _dispatch_failure_receipt_reason(exc)
    if receipt_reason is not None:
        await record_generation_upstream_marker(
            state,
            response_received=False,
            proven_undelivered=(receipt_reason == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED),
            proven_no_cost=(receipt_reason == UPSTREAM_DISPATCH_PROVEN_NO_COST),
        )
        return
    if getattr(exc, "error_code", None) in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES:
        return
    if getattr(
        state, "image_iter", None
    ) is None and generation_dispatch_requires_unknown_settlement(state):
        try:
            await record_generation_upstream_marker(
                state,
                response_received=False,
                proven_undelivered=True,
            )
        except StaleGenerationAttempt:
            logger.info(
                "generation pre-dispatch receipt superseded task=%s attempt=%s",
                state.task_id,
                state.attempt,
            )
        return
    if _dispatch_failure_has_response(exc):
        await record_generation_upstream_marker(state, response_received=True)
        return
    if not generation_dispatch_requires_unknown_settlement(state):
        return
    if has_stable_provider_idempotency_key(state.gen_upstream_request_snapshot or {}):
        return
    raise UpstreamError(
        "upstream dispatch completed without a response receipt; result is unknown",
        status_code=getattr(exc, "status_code", None),
        error_code=EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        payload={
            "upstream_result_unknown": True,
            "execution_epoch": generation_execution_epoch(state),
            "attempt": state.attempt,
            "cause": type(exc).__name__,
        },
    ) from exc


def _dispatch_failure_receipt_reason(exc: BaseException) -> str | None:
    return dispatch_receipt_reason(exc)


def _dispatch_failure_proves_undelivered(exc: BaseException) -> bool:
    return _dispatch_failure_receipt_reason(exc) == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED


def _dispatch_failure_has_response(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code > 0


async def raise_if_pre_upstream_interrupted(
    state: GenerationRunState,
) -> None:
    if state.lease_lost.is_set():
        raise LeaseLost("generation lease renewer failed")
    force_next_cancellation_check(state.task_id)
    if await is_cancelled(state.redis, state.task_id):
        raise TaskCancelled("cancelled before upstream request")


def annotate_upstream_span(state: GenerationRunState, span: Any) -> None:
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


async def call_upstream(state: GenerationRunState) -> None:
    started = time.monotonic()
    await raise_if_pre_upstream_interrupted(state)
    state.image_iter = build_image_iterator(state)
    first_pair = await anext_image_with_guards(
        state.image_iter,
        state.lease_lost,
        redis=state.redis,
        task_id=state.task_id,
    )
    if first_pair is None:
        raise UpstreamError(
            "upstream image generator yielded no result",
            error_code=EC.NO_IMAGE_RETURNED.value,
            status_code=200,
        )
    state.b64_result, state.revised_prompt = first_pair
    state.upstream_duration_ms = int(max(0.0, time.monotonic() - started) * 1000)
    state.stage_timer.set_ms("render", state.upstream_duration_ms)
    record_winner_provider(state)
    await persist_generation_takeover_checkpoint(state)
    await consume_batch_extra_pairs(state)


async def _ensure_generation_user_active(state: GenerationRunState) -> None:
    services = DispatchGenerationServices.from_deps(state.services)
    async with services.store.session() as session:
        if not await lock_active_generation_user(
            session,
            user_id=state.user_id,
        ):
            raise TaskCancelled("account deleted before upstream dispatch")


async def _record_generation_dispatch_ready(state: GenerationRunState) -> None:
    lock = getattr(state, "dispatch_marker_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state.dispatch_marker_lock = lock
    async with lock:
        if getattr(state, "dispatch_marker_recorded", False):
            return
        await record_generation_upstream_marker(
            state,
            response_received=False,
            fence_active_user=True,
        )
        state.dispatch_marker_recorded = True


async def record_generation_upstream_marker(
    state: GenerationRunState,
    *,
    response_received: bool,
    proven_undelivered: bool = False,
    proven_no_cost: bool = False,
    fence_active_user: bool = False,
    response_status_code: int | None = None,
    response_request_id: str | None = None,
    response_trace_id: str | None = None,
    response_http_attempts: int | None = None,
) -> None:
    """Persist an upstream receipt in a short, ownership-fenced transaction."""

    if fence_active_user and (
        response_received or proven_undelivered or proven_no_cost
    ):
        raise ValueError("active-user fence is only valid for dispatch-start markers")
    if sum((response_received, proven_undelivered, proven_no_cost)) > 1:
        raise ValueError("upstream marker outcomes are mutually exclusive")
    if not response_received and any(
        value is not None
        for value in (
            response_status_code,
            response_request_id,
            response_trace_id,
            response_http_attempts,
        )
    ):
        raise ValueError("response diagnostics require a response receipt")
    services = DispatchGenerationServices.from_deps(state.services)
    recorded_at = datetime.now(timezone.utc).isoformat()
    async with services.store.session() as session:
        if fence_active_user and not await lock_active_generation_user(
            session,
            user_id=state.user_id,
        ):
            raise TaskCancelled("account deleted before upstream dispatch")
        ownership_conditions = [
            Generation.id == state.task_id,
            Generation.attempt == state.attempt,
            Generation.execution_epoch == generation_execution_epoch(state),
            Generation.status.in_(RUNNING_GENERATION_STATUSES),
        ]
        if fence_active_user:
            ownership_conditions.append(Generation.user_id == state.user_id)
        if not response_received and not proven_undelivered and not proven_no_cost:
            ownership_conditions.append(Generation.cancel_requested_at.is_(None))
        current = (
            await session.execute(
                select(Generation).where(*ownership_conditions).with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise StaleGenerationAttempt(
                f"generation marker stale task={state.task_id} attempt={state.attempt}"
            )
        if response_received:
            request = mark_upstream_response_received(
                current,
                at=recorded_at,
                attempt=state.attempt,
                execution_epoch=generation_execution_epoch(state),
                status_code=response_status_code,
                request_id=response_request_id,
                response_trace_id=response_trace_id,
                http_attempts=response_http_attempts,
            )
        else:
            marker = (
                mark_upstream_dispatch_proven_no_cost
                if proven_no_cost
                else mark_upstream_dispatch_proven_undelivered
                if proven_undelivered
                else mark_upstream_dispatch_started
            )
            request = marker(
                current,
                at=recorded_at,
                attempt=state.attempt,
                execution_epoch=generation_execution_epoch(state),
            )
        current.upstream_request = request
        await session.commit()
        state.gen_upstream_request_snapshot = dict(request)


def build_image_iterator(state: GenerationRunState) -> Any:
    provider = state.services.provider
    options = state.image_request_options
    provider_override = None if state.is_dual_race else state.reserved_provider
    persisted_executions = sidecar_executions_from_request(
        getattr(state, "gen_upstream_request_snapshot", None)
    )
    sidecar_execution: Any = (
        persisted_executions
        if len(persisted_executions) > 1
        else persisted_executions[0]
        if persisted_executions
        else getattr(state, "sidecar_execution", None)
    )
    request = GenerationProviderRequest(
        prompt=state.prompt_for_upstream,
        size=state.resolved.size,
        quality=str(options["render_quality"]),
        output_format=str(options["output_format"]),
        output_compression=options.get("output_compression"),
        background=str(options["background"]),
        moderation=str(options["moderation"]),
        n=state.requested_image_count,
        model=str(options["responses_model"]),
        progress_callback=state.progress_publisher,
        provider_override=provider_override,
        user_id=state.user_id,
        context=GenerationProviderContext(
            trace_id=state.trace_id,
            retry_attempt=state.attempt,
            quota_task_id=state.task_id,
            quota_attempt_epoch=generation_execution_identity(
                generation_execution_epoch(state),
                state.attempt,
            ),
            sidecar_execution=sidecar_execution,
        ),
    )
    if state.action != GenerationAction.EDIT:
        return provider.generate(request)
    if not state.ref_for_body:
        raise UpstreamError(
            "edit action requires at least one reference image",
            error_code=EC.INVALID_REQUEST_ERROR.value,
            status_code=400,
        )
    edit_request = GenerationProviderEditRequest(
        request=replace(
            request,
            size=state.inpaint_size_override or state.resolved.size,
        ),
        images=tuple(raw for _sha, raw in state.ref_for_body),
        mask=state.mask_bytes,
    )
    return provider.edit(edit_request)


def record_winner_provider(state: GenerationRunState) -> None:
    event = state.progress_publisher.pop_provider_used_event()
    state.actual_upstream_provider = event.get("provider")
    state.actual_upstream_route = event.get("route")
    state.actual_upstream_source = event.get("source")
    state.actual_upstream_endpoint = event.get("endpoint")


async def consume_batch_extra_pairs(state: GenerationRunState) -> None:
    if not should_consume_batch_extras(state):
        if state.requested_image_count > 1:
            raise UpstreamError(
                "upstream route cannot durably collect every requested image",
                status_code=200,
                error_code=EC.IMAGE_JOB_RESULT_UNKNOWN.value,
                payload={
                    "upstream_result_unknown": True,
                    "requested_count": state.requested_image_count,
                    "actual_count": 1,
                },
            )
        return
    for batch_index in range(2, state.requested_image_count + 1):
        extra_pair = await next_batch_extra_pair(state, batch_index)
        state.batch_extra_pairs.append((batch_index, extra_pair))
        await persist_generation_takeover_checkpoint(state)


def should_consume_batch_extras(state: GenerationRunState) -> bool:
    return bool(
        state.requested_image_count > 1
        and state.image_iter is not None
        and state.actual_upstream_source in {"image2_direct", "image2_edit_direct"}
    )


async def next_batch_extra_pair(
    state: GenerationRunState,
    batch_index: int,
) -> GeneratedImageResult:
    try:
        pair = await anext_image_with_guards(
            state.image_iter,
            state.lease_lost,
            redis=state.redis,
            task_id=state.task_id,
        )
    except (
        LeaseLost,
        TaskCancelled,
        asyncio.CancelledError,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            "image batch failed before every requested result was checkpointed",
            error_code=EC.IMAGE_JOB_RESULT_UNKNOWN.value,
            payload={
                "upstream_result_unknown": True,
                "batch_index": batch_index,
                "requested_count": state.requested_image_count,
            },
        ) from exc
    if pair is None:
        raise UpstreamError(
            "upstream returned fewer images than requested",
            status_code=200,
            error_code=EC.NO_IMAGE_RETURNED.value,
            payload={
                "upstream_result_unknown": True,
                "batch_index": batch_index,
                "requested_count": state.requested_image_count,
                "actual_count": batch_index - 1,
            },
        )
    return pair


__all__ = [
    "build_image_iterator",
    "dispatch_upstream_request",
    "initialize_execution_state",
    "prepare_upstream_request",
]
