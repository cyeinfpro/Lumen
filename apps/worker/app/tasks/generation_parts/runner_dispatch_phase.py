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
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)

from ...observability import get_tracer, upstream_calls_total
from ...provider_runtime.errors import UpstreamError
from ...upstream_parts import GeneratedImageResult
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .lease import is_cancelled
from .progress import ImageProgressPublisher
from .request_options import (
    image_request_options,
    image_requested_count,
    prompt_with_aspect_ratio_constraint,
    validate_resolved_size,
)
from .retry_state import (
    RUNNING_GENERATION_STATUSES,
    anext_image_with_guards,
)
from .run_state import GenerationRunState
from .runner_phase_services import DispatchGenerationServices
from .services import (
    GenerationProviderContext,
    GenerationProviderEditRequest,
    GenerationProviderRequest,
)


logger = logging.getLogger(f"{__package__}.runner")
tracer = get_tracer("lumen.worker.generation")


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
    state.prompt_for_upstream = prompt_with_aspect_ratio_constraint(
        state.prompt,
        state.aspect_ratio,
    )
    await load_references_and_mask(state)
    normalize_mask(state)
    state.stage_timer.add_elapsed("normalize", started)
    await publish_stream_started(state)
    state.progress_publisher = ImageProgressPublisher(
        state,
        state.services,
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
            "stage": GenerationStage.RENDERING.value,
            "substage": GenerationStage.STREAM_STARTED.value,
        },
    )


async def dispatch_upstream_request(state: GenerationRunState) -> None:
    async with asyncio.timeout_at(state.task_deadline):
        await raise_if_pre_upstream_interrupted(state)
        await record_generation_upstream_marker(state, response_received=False)
        with tracer.start_as_current_span("upstream.generate_image") as span:
            annotate_upstream_span(state, span)
            try:
                await call_upstream(state)
                upstream_calls_total.labels(
                    kind="generation",
                    outcome="ok",
                ).inc()
            except Exception:
                upstream_calls_total.labels(
                    kind="generation",
                    outcome="error",
                ).inc()
                raise


async def raise_if_pre_upstream_interrupted(
    state: GenerationRunState,
) -> None:
    if state.lease_lost.is_set():
        raise LeaseLost("generation lease renewer failed")
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
    await record_generation_upstream_marker(state, response_received=True)
    state.b64_result, state.revised_prompt = first_pair
    state.upstream_duration_ms = int(max(0.0, time.monotonic() - started) * 1000)
    state.stage_timer.set_ms("render", state.upstream_duration_ms)
    record_winner_provider(state)
    await consume_batch_extra_pairs(state)


async def record_generation_upstream_marker(
    state: GenerationRunState,
    *,
    response_received: bool,
) -> None:
    services = DispatchGenerationServices.from_deps(state.services)
    recorded_at = datetime.now(timezone.utc).isoformat()
    async with services.store.session() as session:
        current = (
            await session.execute(
                select(Generation)
                .where(
                    Generation.id == state.task_id,
                    Generation.attempt == state.attempt,
                    Generation.status.in_(RUNNING_GENERATION_STATUSES),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise StaleGenerationAttempt(
                f"generation marker stale task={state.task_id} attempt={state.attempt}"
            )
        marker = (
            mark_upstream_response_received
            if response_received
            else mark_upstream_dispatch_started
        )
        request = marker(
            current,
            at=recorded_at,
            attempt=state.attempt,
        )
        current.upstream_request = request
        await session.commit()
        state.gen_upstream_request_snapshot = dict(request)


def build_image_iterator(state: GenerationRunState) -> Any:
    provider = state.services.provider
    options = state.image_request_options
    provider_override = None if state.is_dual_race else state.reserved_provider
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
            quota_attempt_epoch=state.attempt,
            sidecar_execution=getattr(state, "sidecar_execution", None),
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
        return
    for batch_index in range(2, state.requested_image_count + 1):
        extra_pair = await next_batch_extra_pair(state, batch_index)
        if extra_pair is None:
            break
        state.batch_extra_pairs.append((batch_index, extra_pair))


def should_consume_batch_extras(state: GenerationRunState) -> bool:
    return bool(
        state.requested_image_count > 1
        and state.image_iter is not None
        and state.actual_upstream_source in {"image2_direct", "image2_edit_direct"}
    )


async def next_batch_extra_pair(
    state: GenerationRunState,
    batch_index: int,
) -> GeneratedImageResult | None:
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
        logger.warning(
            "image2 n extra iter failed task=%s index=%s err=%r",
            state.task_id,
            batch_index,
            exc,
        )
        return None
    if pair is None:
        logger.warning(
            "image2 n returned fewer images task=%s requested=%s actual=%s",
            state.task_id,
            state.requested_image_count,
            batch_index - 1,
        )
    return pair


__all__ = [
    "build_image_iterator",
    "dispatch_upstream_request",
    "initialize_execution_state",
    "prepare_upstream_request",
]
