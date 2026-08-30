"""Agent Worker orchestration across DB, Runtime, provider, and billing fences."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from lumen_core.agent_content_safety import agent_content_safety_decision
from lumen_core.agent_protocol_safety import agent_text_boundary_error
from lumen_core.agent_dispatch import provider_dispatch_evidence_count
from lumen_core.agent_events import AgentRunStatus
from lumen_core.model_entities import AgentProviderCall, AgentRun, Message

from ...agent_context import (
    AgentContextBuild,
    AgentContextError,
    build_agent_context,
    resolve_agent_chat_provider,
)
from ...agent_runtime_client import (
    AgentRuntimeClient,
    AgentRuntimeClientError,
    AgentRuntimeEvent,
)
from ...byok_runtime import record_user_credential_runtime_error
from ...config import settings
from ...db import SessionLocal
from ...locks import owned_redis_lock
from ...observability import (
    agent_partial_runs_total,
    agent_generation_links_total,
    agent_limits_total,
    agent_provider_usage_tokens_total,
    agent_reference_images,
    agent_run_duration_seconds,
    agent_runs_total,
    agent_runtime_disconnects_total,
    agent_runtime_requests_total,
    agent_tool_calls_total,
    agent_tool_duration_seconds,
    agent_turns_histogram,
    get_tracer,
)
from ...provider_pool import agent_provider_attempt
from ...provider_runtime.errors import UpstreamError
from ...services.billing_cache import get_billing_cache
from .. import auto_title
from .contracts import AGENT_USAGE_KEYS, AgentClaim, AgentRuntimeAccumulator
from .persistence import (
    AgentRunFinalization,
    claim_agent_run,
    finalize_agent_run,
    flush_agent_text,
    load_claimed_run,
    publish_agent_event_fast_path,
    record_runtime_checkpoint,
    update_dispatch_state,
)
from .terminal_policy import terminal_request as _terminal_request


logger = logging.getLogger(__name__)
tracer = get_tracer("lumen.worker.agent")
_AGENT_LOCK_TTL_SECONDS = 60
_CANCEL_POLL_SECONDS = 0.25
_PROVIDER_HEALTH_NEUTRAL_CODES = frozenset(
    {
        "agent_output_truncated",
        "agent_safety_budget_reached",
        "agent_tool_failed",
        "agent_tool_limit_reached",
        "agent_image_limit_reached",
        "agent_reference_not_found",
        "agent_tool_result_unknown",
        "agent_runtime_shutdown",
    }
)


async def _watch_cancel(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
    requested: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            if await redis.get(f"agent:{run_id}:cancel"):
                requested.set()
                return
        except Exception:
            logger.warning("agent cancel signal read failed run=%s", run_id)
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(
                        AgentRun.status,
                        AgentRun.execution_epoch,
                        AgentRun.cancel_requested_at,
                    ).where(AgentRun.id == run_id)
                )
            ).first()
        if (
            row is None
            or row.status != AgentRunStatus.RUNNING.value
            or row.execution_epoch != execution_epoch
            or row.cancel_requested_at is not None
        ):
            requested.set()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=_CANCEL_POLL_SECONDS)
        except TimeoutError:
            pass


async def _flush_if_needed(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
    accumulator: AgentRuntimeAccumulator,
    force: bool = False,
) -> bool:
    async with accumulator.flush_lock:
        if (
            not accumulator.pending_delta
            and not accumulator.text_reset_pending
            and not accumulator.blocks_dirty
        ):
            return True
        now = asyncio.get_running_loop().time()
        if (
            not force
            and not accumulator.text_reset_pending
            and not accumulator.force_next_delta
            and (
                len(accumulator.pending_delta) < settings.agent_text_flush_chars
                and now - accumulator.last_flush_at < settings.agent_text_flush_seconds
            )
        ):
            return True
        delta = accumulator.pending_delta
        text_snapshot = accumulator.text
        replace = accumulator.text_reset_pending
        output_revision = accumulator.output_revision
        output_runtime_seq = accumulator.output_runtime_seq
        blocks = copy.deepcopy(accumulator.blocks)
        current = await flush_agent_text(
            redis,
            run_id=run_id,
            execution_epoch=execution_epoch,
            text=text_snapshot,
            delta=delta,
            replace=replace,
            blocks=blocks,
            output_revision=output_revision,
            output_runtime_seq=output_runtime_seq,
            snapshot_only=(accumulator.blocks_dirty and not delta and not replace),
        )
        if current:
            if replace and accumulator.output_revision == output_revision:
                accumulator.text_reset_pending = False
            if accumulator.force_next_delta and delta:
                accumulator.force_next_delta = False
            if accumulator.pending_delta.startswith(delta):
                accumulator.pending_delta = accumulator.pending_delta[len(delta) :]
            if accumulator.output_runtime_seq == output_runtime_seq:
                accumulator.blocks_dirty = False
            accumulator.last_flush_at = now
        return current


async def _periodic_text_flush(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
    accumulator: AgentRuntimeAccumulator,
    cancel_requested: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.agent_text_flush_seconds
            )
            return
        except TimeoutError:
            pass
        try:
            current = await _flush_if_needed(
                redis,
                run_id=run_id,
                execution_epoch=execution_epoch,
                accumulator=accumulator,
                force=True,
            )
        except Exception:
            logger.warning("periodic Agent text flush failed run=%s", run_id)
            cancel_requested.set()
            return
        if not current:
            cancel_requested.set()
            return


async def _prepare_context(
    redis: Any,
    *,
    run_id: str,
    execution_epoch: int,
) -> tuple[AgentContextBuild, Any | None]:
    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if (
            run is None
            or run.status != AgentRunStatus.RUNNING.value
            or run.execution_epoch != execution_epoch
        ):
            raise AgentContextError("agent_stale_execution_epoch")
        pool, provider = await resolve_agent_chat_provider(db, run)
        build = await build_agent_context(
            db,
            run=run,
            provider=provider,
            redis=redis,
        )
        await db.commit()
    current = await update_dispatch_state(
        run_id,
        execution_epoch,
        state="context_ready",
        extra={
            "provider_name": build.provider.name,
            "provider_api": build.request.provider.api,
            "reference_count": len(build.request.references),
            "history_count": len(build.request.history),
            "pi_compaction_restored": build.pi_compaction_restored,
            "pi_compaction_degraded": build.pi_compaction_degraded,
            "memory_state": build.memory_state,
            "history_plan": build.history_plan,
            "runtime_wire_plan": build.wire_plan,
        },
    )
    if not current:
        raise AgentContextError("agent_stale_execution_epoch")
    async with SessionLocal() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                run is None
                or run.status != AgentRunStatus.RUNNING.value
                or run.execution_epoch != execution_epoch
            ):
                raise AgentContextError("agent_stale_execution_epoch")
            run.provider_name = build.provider.name
    return build, pool


def _preserve_partial_text(
    requested_status: Literal["succeeded", "partial", "failed", "cancelled"],
    accumulator: AgentRuntimeAccumulator,
) -> Literal["succeeded", "partial", "failed", "cancelled"]:
    if requested_status == "failed" and accumulator.text.strip():
        return "partial"
    return requested_status


async def _post_terminal_hooks(
    _ctx: dict[str, Any],
    *,
    redis: Any,
    conversation_id: str | None,
) -> None:
    if conversation_id:
        await auto_title.maybe_enqueue_auto_title(redis, conversation_id)


async def _finalize(
    ctx: dict[str, Any],
    *,
    redis: Any,
    claim: AgentClaim,
    accumulator: AgentRuntimeAccumulator,
    build: AgentContextBuild | None,
    requested_status: Literal["succeeded", "partial", "failed", "cancelled"],
    error_code: str | None,
    knowledge: Literal["actual", "proven_absent", "unknown"],
    reason: str,
) -> str:
    finalization = AgentRunFinalization(
        run_id=claim.run_id,
        execution_epoch=claim.execution_epoch,
        requested_status=requested_status,
        text=accumulator.text,
        blocks=accumulator.blocks,
        output_revision=accumulator.output_revision,
        output_runtime_seq=accumulator.output_runtime_seq,
        usage=accumulator.usage,
        turn_count=accumulator.turn_count,
        runtime_tool_count=accumulator.runtime_tool_call_count,
        error_code=error_code,
        knowledge=knowledge,
        reason=reason,
        used_memory_summary=build.used_memory_summary if build else (),
    )
    status, billing_result, conversation_id = await finalize_agent_run(
        redis,
        request=finalization,
    )
    if billing_result is not None and billing_result.balance_after is not None:
        cache = get_billing_cache()
        if cache is not None:
            try:
                async with SessionLocal() as db:
                    run = await db.get(AgentRun, claim.run_id)
                    if run is not None:
                        await cache.set_balance(
                            run.user_id, billing_result.balance_after
                        )
            except Exception:
                logger.warning("agent balance cache refresh failed", exc_info=True)
    if billing_result is not None and status in {
        AgentRunStatus.SUCCEEDED.value,
        AgentRunStatus.PARTIAL.value,
    }:
        await _post_terminal_hooks(
            ctx,
            redis=redis,
            conversation_id=conversation_id,
        )
    if billing_result is not None and status in {
        AgentRunStatus.SUCCEEDED.value,
        AgentRunStatus.PARTIAL.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.CANCELLED.value,
    }:
        provider_label = build.provider.name if build else "unknown"
        model_label = build.request.provider.model if build else "unknown"
        if build is None:
            try:
                async with SessionLocal() as db:
                    persisted = await db.get(AgentRun, claim.run_id)
                    if persisted is not None:
                        provider_label = persisted.provider_name or "unknown"
                        model_label = persisted.model or "unknown"
            except Exception:
                logger.warning("Agent metric labels could not be loaded")
        agent_runs_total.labels(
            status=status,
            provider=provider_label,
            model=model_label,
        ).inc()
        agent_run_duration_seconds.labels(status=status).observe(
            max(
                0.0,
                asyncio.get_running_loop().time() - accumulator.started_monotonic,
            )
        )
        agent_turns_histogram.observe(max(0, accumulator.turn_count))
        if status == AgentRunStatus.PARTIAL.value:
            partial_reason = (
                "tool_result_unknown"
                if error_code == "agent_tool_result_unknown"
                else "output_truncated"
                if error_code == "agent_output_truncated"
                else "safety_budget"
                if error_code == "agent_safety_budget_reached"
                else "run_timeout"
                if error_code == "agent_run_timeout"
                else "text_before_failure"
                if accumulator.text.strip()
                else "side_effect_before_failure"
            )
            agent_partial_runs_total.labels(reason=partial_reason).inc()
        for kind in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_1h_tokens",
            "reasoning_tokens",
        ):
            value = int(accumulator.usage.get(kind) or 0)
            if value > 0:
                agent_provider_usage_tokens_total.labels(kind=kind).inc(value)
    return status


async def _recover_result_unknown(
    ctx: dict[str, Any],
    *,
    redis: Any,
    claim: AgentClaim,
) -> None:
    run = await load_claimed_run(claim.run_id, claim.execution_epoch)
    dispatch = run.dispatch_jsonb if isinstance(run.dispatch_jsonb, dict) else {}
    terminal = dispatch.get("runtime_terminal")
    accumulator = AgentRuntimeAccumulator()
    persisted_usage = run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {}
    for key in AGENT_USAGE_KEYS:
        value = persisted_usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            accumulator.usage[key] = max(0, value)
    accumulator.turn_count = max(0, int(run.turn_count or 0))
    accumulator.runtime_tool_call_count = max(0, int(run.tool_call_count or 0))
    accumulator.provider_dispatch_count = provider_dispatch_evidence_count(dispatch)
    accumulator.provider_completed_count = max(
        0, int(dispatch.get("provider_completed_count") or 0)
    )
    accumulator.provider_response_statuses = [
        value
        for value in dispatch.get("provider_response_statuses", [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    accumulator.output_revision = max(0, int(run.output_revision or 0))
    accumulator.output_runtime_seq = max(0, int(run.output_runtime_seq or 0))
    async with SessionLocal() as db:
        message = await db.get(Message, run.assistant_message_id)
        message_content = (
            message.content
            if message is not None and isinstance(message.content, dict)
            else {}
        )
        message_revision = message_content.get("output_revision")
        message_runtime_seq = message_content.get("output_runtime_seq")
        message_tuple_matches = (
            message_revision == accumulator.output_revision
            and message_runtime_seq == accumulator.output_runtime_seq
        ) or (
            message_revision is None
            and message_runtime_seq is None
            and accumulator.output_revision == 0
            and accumulator.output_runtime_seq == 0
        )
        if message_tuple_matches:
            durable_text = message_content.get("text")
            if isinstance(durable_text, str):
                accumulator.text = durable_text
        transcript = (
            run.transcript_jsonb if isinstance(run.transcript_jsonb, dict) else {}
        )
        transcript_matches = (
            transcript.get("projection") == "ordered_blocks"
            and transcript.get("output_revision") == accumulator.output_revision
            and transcript.get("output_runtime_seq") == accumulator.output_runtime_seq
            and isinstance(transcript.get("blocks"), list)
        )
        if transcript_matches:
            accumulator.blocks = copy.deepcopy(transcript["blocks"])
            if not message_tuple_matches:
                accumulator.text = "\n\n".join(
                    str(block.get("text") or "")
                    for block in accumulator.blocks
                    if isinstance(block, dict)
                    and block.get("kind") == "text"
                    and str(block.get("text") or "")
                )
        provider_calls = list(
            (
                await db.execute(
                    select(AgentProviderCall).where(
                        AgentProviderCall.agent_run_id == run.id,
                        AgentProviderCall.execution_epoch == run.execution_epoch,
                    )
                )
            )
            .scalars()
            .all()
        )
        for provider_call in provider_calls:
            ordinal = int(provider_call.dispatch_ordinal)
            accumulator.provider_dispatch_ordinals.add(ordinal)
            if provider_call.result_state == "exact":
                accumulator.provider_completed_ordinals.add(ordinal)
                accumulator.exact_usage_ordinals.add(ordinal)
            elif provider_call.result_state == "missing":
                accumulator.no_charge_ordinals.add(ordinal)
    if isinstance(terminal, dict):
        usage = terminal.get("usage")
        if isinstance(usage, dict):
            for key in AGENT_USAGE_KEYS:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    accumulator.usage[key] = max(
                        accumulator.usage.get(key, 0),
                        max(0, value),
                    )
        accumulator.turn_count = max(
            accumulator.turn_count,
            int(terminal.get("turn_count") or 0),
        )
        accumulator.runtime_tool_call_count = max(
            accumulator.runtime_tool_call_count,
            int(terminal.get("tool_call_count") or 0),
        )
        accumulator.provider_dispatch_count = max(
            accumulator.provider_dispatch_count,
            int(terminal.get("provider_dispatch_count") or 0),
        )
        accumulator.provider_completed_count = max(
            accumulator.provider_completed_count,
            int(terminal.get("provider_completed_count") or 0),
        )
        accumulator.terminal_status = str(terminal.get("status") or "failed")
        error = terminal.get("error_code")
        accumulator.terminal_error_code = error if isinstance(error, str) else None
        requested, code, knowledge, reason = _terminal_request(accumulator)
    else:
        requested, code, knowledge, reason = (
            "failed",
            "agent_result_unknown",
            "unknown",
            "worker_recovered_post_dispatch",
        )
    requested = _preserve_partial_text(requested, accumulator)
    await _finalize(
        ctx,
        redis=redis,
        claim=claim,
        accumulator=accumulator,
        build=None,
        requested_status=requested,
        error_code=code,
        knowledge=knowledge,
        reason=reason,
    )


async def _record_byok_terminal_failure(
    *,
    run_id: str,
    status_code: int | None,
) -> None:
    if status_code is None:
        return
    async with SessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        credential_id = run.user_api_credential_id if run is not None else None
    if not credential_id:
        return
    await record_user_credential_runtime_error(
        credential_id,
        UpstreamError(
            "Agent provider request failed",
            status_code=status_code,
            error_code="agent_provider_error",
        ),
    )


@dataclass(slots=True)
class _PreparedExecution:
    ctx: dict[str, Any]
    redis: Any
    runtime_client: AgentRuntimeClient
    claim: AgentClaim
    accumulator: AgentRuntimeAccumulator
    build: AgentContextBuild
    pool: Any | None


@dataclass(slots=True)
class _BackgroundTasks:
    cancel_requested: asyncio.Event
    cancel_stop: asyncio.Event
    flush_stop: asyncio.Event
    cancel_watcher: asyncio.Task[None]
    periodic_flusher: asyncio.Task[None]

    @classmethod
    def start(cls, state: _PreparedExecution) -> "_BackgroundTasks":
        cancel_requested = asyncio.Event()
        cancel_stop = asyncio.Event()
        flush_stop = asyncio.Event()
        return cls(
            cancel_requested=cancel_requested,
            cancel_stop=cancel_stop,
            flush_stop=flush_stop,
            cancel_watcher=asyncio.create_task(
                _watch_cancel(
                    state.redis,
                    run_id=state.claim.run_id,
                    execution_epoch=state.claim.execution_epoch,
                    requested=cancel_requested,
                    stop=cancel_stop,
                )
            ),
            periodic_flusher=asyncio.create_task(
                _periodic_text_flush(
                    state.redis,
                    run_id=state.claim.run_id,
                    execution_epoch=state.claim.execution_epoch,
                    accumulator=state.accumulator,
                    cancel_requested=cancel_requested,
                    stop=flush_stop,
                )
            ),
        )

    async def close(self) -> None:
        self.cancel_stop.set()
        self.flush_stop.set()
        for task in (self.cancel_watcher, self.periodic_flusher):
            task.cancel()
        await asyncio.gather(
            self.cancel_watcher,
            self.periodic_flusher,
            return_exceptions=True,
        )


async def _handle_runtime_event(
    state: _PreparedExecution,
    event: AgentRuntimeEvent,
    cancel_requested: asyncio.Event,
) -> None:
    compaction_error = (
        agent_text_boundary_error(event.summary or "")
        if event.type == "compaction.completed"
        else None
    )
    if event.type == "text.delta" and event.delta:
        candidate = f"{state.accumulator.text}{event.delta}"
        if agent_content_safety_decision(candidate).blocked:
            retained = state.accumulator.text
            state.accumulator.text = retained
            state.accumulator.pending_delta = ""
            state.accumulator.blocks = (
                [{"kind": "text", "turn": 1, "text": retained}] if retained else []
            )
            state.accumulator.output_revision += 1
            state.accumulator.output_runtime_seq = event.seq
            state.accumulator.text_reset_pending = True
            cancel_requested.set()
            raise AgentRuntimeClientError("content_policy_violation")
    state.accumulator.apply(event)
    if event.type == "limit.reached":
        agent_limits_total.labels(reason=event.reason or "unknown").inc()
    if event.type in {"tool.succeeded", "tool.failed"}:
        outcome = "succeeded" if event.type == "tool.succeeded" else "failed"
        agent_tool_calls_total.labels(
            name=(event.name or "unknown"),
            mode=event.mode or "unknown",
            status=outcome,
        ).inc()
        duration = state.accumulator.consume_tool_duration(event)
        if duration is not None:
            agent_tool_duration_seconds.labels(
                name=event.name or "unknown",
                mode=event.mode or "unknown",
                status=outcome,
            ).observe(duration)
        if event.type == "tool.succeeded" and event.generation_ids:
            agent_generation_links_total.labels(
                mode=event.mode or "unknown",
                status="accepted",
            ).inc(len(event.generation_ids))
        with tracer.start_as_current_span("agent.tool.result") as span:
            span.set_attribute("lumen.agent_run_id", state.claim.run_id)
            span.set_attribute(
                "lumen.agent_tool_call_id",
                event.tool_call_id or "unknown",
            )
            span.set_attribute("lumen.agent_tool_name", event.name or "unknown")
            span.set_attribute("lumen.agent_tool_outcome", outcome)
            if event.generation_ids:
                span.set_attribute("lumen.generation_ids", event.generation_ids)
    checkpointed = await record_runtime_checkpoint(
        state.claim.run_id,
        state.claim.execution_epoch,
        event,
    )
    flushed = checkpointed and await _flush_if_needed(
        state.redis,
        run_id=state.claim.run_id,
        execution_epoch=state.claim.execution_epoch,
        accumulator=state.accumulator,
        force=event.type.startswith("tool."),
    )
    if not flushed:
        cancel_requested.set()
        raise AgentRuntimeClientError("agent_stale_execution_epoch")
    if compaction_error is not None:
        cancel_requested.set()
        raise AgentRuntimeClientError(compaction_error)


async def _finish_runtime_success(state: _PreparedExecution, attempt: Any) -> None:
    await _flush_if_needed(
        state.redis,
        run_id=state.claim.run_id,
        execution_epoch=state.claim.execution_epoch,
        accumulator=state.accumulator,
        force=True,
    )
    requested, code, knowledge, reason = _terminal_request(state.accumulator)
    requested = _preserve_partial_text(requested, state.accumulator)
    agent_runtime_requests_total.labels(outcome=requested).inc()
    if attempt is not None and requested == "succeeded" and knowledge != "unknown":
        attempt.report_success()
    elif (
        attempt is not None
        and state.accumulator.provider_dispatch_count > 0
        and (knowledge == "unknown" or code not in _PROVIDER_HEALTH_NEUTRAL_CODES)
    ):
        attempt.report_failure()
    if requested in {"failed", "partial"} and state.build.provider.name.startswith(
        "user:"
    ):
        statuses = state.accumulator.provider_response_statuses
        await _record_byok_terminal_failure(
            run_id=state.claim.run_id,
            status_code=statuses[-1] if statuses else None,
        )
    await _finalize(
        state.ctx,
        redis=state.redis,
        claim=state.claim,
        accumulator=state.accumulator,
        build=state.build,
        requested_status=requested,
        error_code=code,
        knowledge=knowledge,
        reason=reason,
    )


async def _finish_runtime_failure(
    state: _PreparedExecution,
    error: AgentRuntimeClientError,
) -> None:
    await _flush_if_needed(
        state.redis,
        run_id=state.claim.run_id,
        execution_epoch=state.claim.execution_epoch,
        accumulator=state.accumulator,
        force=True,
    )
    if error.delivery == "proven_absent":
        await update_dispatch_state(
            state.claim.run_id,
            state.claim.execution_epoch,
            state="proven_absent",
            extra={"runtime_error_code": error.code},
        )
    dispatched = state.accumulator.provider_dispatch_count > 0
    knowledge: Literal["actual", "proven_absent", "unknown"] = (
        "proven_absent"
        if error.delivery == "proven_absent" and not dispatched
        else "unknown"
        if state.accumulator.has_unresolved_dispatch
        else "actual"
        if state.accumulator.has_exact_usage
        else "proven_absent"
        if state.accumulator.response_proves_no_cost
        else "unknown"
    )
    agent_runtime_disconnects_total.labels(
        phase="after_provider" if dispatched else "before_provider"
    ).inc()
    requested_status: Literal["succeeded", "partial", "failed", "cancelled"] = (
        "cancelled" if error.code == "agent_cancelled" else "failed"
    )
    requested_status = _preserve_partial_text(
        requested_status,
        state.accumulator,
    )
    agent_runtime_requests_total.labels(outcome=requested_status).inc()
    await _finalize(
        state.ctx,
        redis=state.redis,
        claim=state.claim,
        accumulator=state.accumulator,
        build=state.build,
        requested_status=requested_status,
        error_code=error.code,
        knowledge=knowledge,
        reason="runtime_client_failure",
    )


async def _run_prepared_execution(state: _PreparedExecution) -> None:
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("lumen.agent_run_id", state.claim.run_id)
        span.set_attribute(
            "lumen.agent_session_id",
            state.build.request.agent_session_id,
        )
        span.set_attribute("lumen.execution_epoch", state.claim.execution_epoch)
        span.set_attribute("lumen.trace_id", state.build.request.trace_id)
        span.set_attribute("lumen.provider", state.build.provider.name)
        span.set_attribute("lumen.model", state.build.request.provider.model)
        span.set_attribute(
            "lumen.user_id_hash",
            hashlib.sha256(state.build.request.user_id.encode("utf-8")).hexdigest()[:16]
            if hasattr(state.build.request, "user_id")
            else hashlib.sha256(state.claim.run_id.encode("utf-8")).hexdigest()[:16],
        )
        background = _BackgroundTasks.start(state)
        attempt_context = (
            agent_provider_attempt(
                state.pool,
                state.build.provider,
                state.build.request.provider.model,
            )
            if state.pool is not None
            else nullcontext(None)
        )
        try:
            with attempt_context as attempt:

                async def fence_runtime_request() -> None:
                    started = await update_dispatch_state(
                        state.claim.run_id,
                        state.claim.execution_epoch,
                        state="starting",
                    )
                    if started:
                        return
                    background.cancel_requested.set()
                    raise AgentRuntimeClientError(
                        "agent_stale_execution_epoch",
                        delivery="proven_absent",
                    )

                try:
                    async for event in state.runtime_client.stream(
                        state.build.request,
                        cancel_requested=background.cancel_requested,
                        on_request_starting=fence_runtime_request,
                    ):
                        await _handle_runtime_event(
                            state,
                            event,
                            background.cancel_requested,
                        )
                except AgentRuntimeClientError as exc:
                    await _finish_runtime_failure(state, exc)
                else:
                    await _finish_runtime_success(state, attempt)
        finally:
            span.set_attribute("lumen.agent_turn_count", state.accumulator.turn_count)
            span.set_attribute(
                "lumen.agent_tool_call_count",
                state.accumulator.runtime_tool_call_count,
            )
            span.set_attribute(
                "lumen.agent_terminal_status",
                state.accumulator.terminal_status or "unknown",
            )
            await background.close()


async def _prepare_claimed_execution(
    ctx: dict[str, Any],
    redis: Any,
    runtime_client: AgentRuntimeClient,
    claim: AgentClaim,
) -> _PreparedExecution | None:
    accumulator = AgentRuntimeAccumulator(
        last_flush_at=asyncio.get_running_loop().time()
    )
    try:
        build, pool = await _prepare_context(
            redis,
            run_id=claim.run_id,
            execution_epoch=claim.execution_epoch,
        )
    except (AgentContextError, UpstreamError) as exc:
        code = getattr(exc, "code", None) or getattr(
            exc, "error_code", "agent_context_unavailable"
        )
        await _finalize(
            ctx,
            redis=redis,
            claim=claim,
            accumulator=accumulator,
            build=None,
            requested_status="failed",
            error_code=str(code)[:64],
            knowledge="proven_absent",
            reason="context_preflight_failed",
        )
        return None
    agent_reference_images.observe(len(build.request.references))
    return _PreparedExecution(
        ctx=ctx,
        redis=redis,
        runtime_client=runtime_client,
        claim=claim,
        accumulator=accumulator,
        build=build,
        pool=pool,
    )


async def orchestrate_agent_run(ctx: dict[str, Any], run_id: str) -> None:
    redis = ctx.get("redis")
    runtime_client = ctx.get("agent_runtime_client")
    if redis is None:
        raise TypeError("ctx['redis'] is required for run_agent")
    if not isinstance(runtime_client, AgentRuntimeClient):
        raise TypeError("ctx['agent_runtime_client'] must be AgentRuntimeClient")
    async with owned_redis_lock(
        redis,
        key=f"lock:agent-run:{run_id}",
        ttl_s=_AGENT_LOCK_TTL_SECONDS,
        log=logger,
    ) as acquired:
        if not acquired:
            return
        claim, started_event = await claim_agent_run(run_id)
        if started_event is not None:
            await publish_agent_event_fast_path(redis, started_event)
        if claim.action in {"missing", "terminal"}:
            return
        if claim.action == "result_unknown":
            await _recover_result_unknown(ctx, redis=redis, claim=claim)
            return
        state = await _prepare_claimed_execution(ctx, redis, runtime_client, claim)
        if state is not None:
            await _run_prepared_execution(state)


__all__ = ["orchestrate_agent_run"]
