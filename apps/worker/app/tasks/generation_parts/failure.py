from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    EV_GEN_PROGRESS,
    EV_GEN_RETRYING,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    task_channel,
)
from lumen_core.models import Generation, Message
from lumen_core.upstream_billing import decide_image_failure_billing

from ...retry import RetryDecision, is_moderation_block
from ...task_cancellation import force_next_cancellation_check
from .diagnostics import (
    build_generation_diagnostics,
    request_event_provider_from_attempts,
    safe_generation_error_summary,
    sanitize_generation_upstream_request,
)
from .errors import StaleGenerationAttempt
from .event_delivery import stage_generation_failure_event
from .execution_boundary import (
    SIDECAR_EXECUTION_KEY,
    release_or_settle_generation,
    release_would_absorb_upstream_cost,
)
from .lease import cancel_renewer_task, is_cancelled, release_lease
from .lifecycle import finalize_running_generation_cancel
from .queue import (
    avoid_provider_for_task,
    enqueue_generation_once,
    get_avoided_providers,
    image_queue_not_before_key,
    is_dual_race_sentinel,
    redis_text,
)
from .retry_state import (
    MAX_ATTEMPTS,
    MODERATION_RETRY_CAP,
    RUNNING_GENERATION_STATUSES,
    classify_exception,
    decide_moderation_retry_upgrade,
    ensure_generation_updated,
    generation_attempt_update,
    mark_generation_attempt_failed,
    mark_generation_attempt_retrying,
    maybe_requeue_stale_generation_attempt,
    retry_delay_seconds,
    retry_not_before_ttl,
    safe_generation_error_details,
)
from .run_state import GenerationRunState
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GenerationFailure:
    decision: Any
    error_code: str
    error_message: str
    error_details: dict[str, Any]
    safe_error_summary: str
    diagnostics: dict[str, Any]
    upstream_request: dict[str, Any]
    moderation_upgrade: bool = False
    effective_max_attempts: int = 0
    exc: BaseException | None = None


async def _finish_durable_cancel_if_requested(
    state: GenerationRunState,
    reason: BaseException,
    g: RunGenerationDeps,
) -> bool:
    force_next_cancellation_check(state.task_id)
    if not await is_cancelled(state.redis, state.task_id):
        return False
    state.task_outcome = await finalize_running_generation_cancel(
        state.redis,
        task_id=state.task_id,
        message_id=state.message_id,
        user_id=state.user_id,
        attempt=state.attempt,
        reason=reason,
        services=g,
    )
    return True


async def handle_lease_lost(
    state: GenerationRunState,
    exc: BaseException,
    g: RunGenerationDeps,
) -> None:
    if await _finish_durable_cancel_if_requested(state, exc, g):
        return
    logger.warning(
        "generation lease lost task=%s attempt=%s err=%s",
        state.task_id,
        state.attempt,
        exc,
    )
    if state.attempt >= MAX_ATTEMPTS:
        await mark_generation_attempt_failed(
            state.redis,
            task_id=state.task_id,
            message_id=state.message_id,
            user_id=state.user_id,
            attempt=state.attempt,
            error_code="lease_lost_max_attempts",
            error_message="lease lost after max attempts",
            retriable=False,
            services=g,
        )
        state.task_outcome = "failed"
        return
    delay = retry_delay_seconds(state.attempt)
    requeued = await mark_generation_attempt_retrying(
        state.redis,
        task_id=state.task_id,
        message_id=state.message_id,
        user_id=state.user_id,
        attempt=state.attempt,
        error_code="lease_lost",
        error_message="generation lease lost; task will be retried",
        delay=delay,
        reason="lease_lost",
        max_attempts=MAX_ATTEMPTS,
        replace_dispatch=state.dispatch_identity,
        services=g,
    )
    state.task_outcome = "retry" if requeued else "lease_lost"


async def handle_stale_attempt(
    state: GenerationRunState,
    exc: BaseException,
    g: RunGenerationDeps,
) -> None:
    if await _finish_durable_cancel_if_requested(state, exc, g):
        return
    logger.info(
        "generation stale attempt task=%s attempt=%s err=%s",
        state.task_id,
        state.attempt,
        exc,
    )
    requeued = await maybe_requeue_stale_generation_attempt(
        state.redis,
        task_id=state.task_id,
        attempt=state.attempt,
        reason=type(exc).__name__,
        replace_dispatch=state.dispatch_identity,
        services=g,
    )
    state.task_outcome = "retry" if requeued else "stale_attempt"


async def handle_cancel(
    state: GenerationRunState,
    exc: BaseException,
    g: RunGenerationDeps,
) -> None:
    state.task_outcome = await finalize_running_generation_cancel(
        state.redis,
        task_id=state.task_id,
        message_id=state.message_id,
        user_id=state.user_id,
        attempt=state.attempt,
        reason=exc,
        services=g,
    )


async def handle_generation_exception(
    state: GenerationRunState,
    exc: Exception,
    g: RunGenerationDeps,
) -> None:
    if await _finish_durable_cancel_if_requested(state, exc, g):
        return
    failure = await _build_failure(state, exc, g)
    failure = await _apply_moderation_retry_policy(state, exc, failure, g)
    should_retry = (
        failure.decision.retriable and state.attempt < failure.effective_max_attempts
    )
    state.task_outcome = "retry" if should_retry else "failed"
    if should_retry:
        await _retry_generation(state, failure, g)
        return
    await _fail_generation_terminal(state, failure, g)


async def _build_failure(
    state: GenerationRunState,
    exc: Exception,
    g: RunGenerationDeps,
) -> GenerationFailure:
    decision = classify_exception(exc, state.has_partial)
    payload = getattr(exc, "payload", None)
    recovery_only = bool(
        isinstance(payload, dict) and payload.get("recovery_only") is True
    )
    accepted_execution = bool(
        getattr(state, "sidecar_execution", None)
        or (isinstance(payload, dict) and payload.get(SIDECAR_EXECUTION_KEY))
    )
    if recovery_only:
        decision = RetryDecision(True, "sidecar recovery only")
    elif accepted_execution:
        decision = RetryDecision(False, "sidecar execution already accepted")
    _byok_terminal, runtime_byok_error = g.credentials.classify_error(exc)
    if state.user_api_credential_id and runtime_byok_error:
        await g.credentials.record_runtime_error(
            state.user_api_credential_id,
            exc,
        )
        decision = RetryDecision(False, f"byok {runtime_byok_error}")
    _log_generation_failure(state, exc, decision, g)
    error_code, error_message = _generation_error_identity(
        state,
        exc,
        runtime_byok_error,
        g,
    )
    error_details = safe_generation_error_details(exc)
    safe_summary = safe_generation_error_summary(
        code=str(error_code) if error_code else None,
        message=error_message,
        status_code=getattr(exc, "status_code", None),
    )
    diagnostics = _error_diagnostics(state, safe_summary, g)
    upstream_request = _error_upstream_request(
        state,
        diagnostics,
        safe_summary,
        g,
    )
    return GenerationFailure(
        decision=decision,
        error_code=str(error_code),
        error_message=error_message,
        error_details=error_details,
        safe_error_summary=safe_summary,
        diagnostics=diagnostics,
        upstream_request=upstream_request,
        effective_max_attempts=MAX_ATTEMPTS,
        exc=exc,
    )


def _log_generation_failure(
    state: GenerationRunState,
    exc: Exception,
    decision: Any,
    g: RunGenerationDeps,
) -> None:
    error_code = getattr(exc, "error_code", None) or type(exc).__name__
    status = getattr(exc, "status_code", None)
    provider = (getattr(exc, "payload", None) or {}).get("provider", "")
    logger.warning(
        "generation failed task=%s attempt=%s retriable=%s reason=%s "
        "error_code=%s http_status=%s provider=%s",
        state.task_id,
        state.attempt,
        decision.retriable,
        decision.reason,
        error_code,
        status,
        provider,
    )
    logger.debug(
        "generation exc trace task=%s",
        state.task_id,
        exc_info=True,
    )


def _generation_error_identity(
    state: GenerationRunState,
    exc: Exception,
    runtime_byok_error: str | None,
    g: RunGenerationDeps,
) -> tuple[str, str]:
    if state.user_api_credential_id and runtime_byok_error:
        return (
            g.credentials.generation_error_code(runtime_byok_error),
            g.credentials.error_message(runtime_byok_error),
        )
    error_code = (
        "timeout"
        if isinstance(exc, TimeoutError)
        else getattr(exc, "error_code", None) or type(exc).__name__
    )
    return str(error_code), str(exc)[:2000]


def _error_diagnostics(
    state: GenerationRunState,
    safe_summary: dict[str, Any],
    g: RunGenerationDeps,
) -> dict[str, Any]:
    provider = (
        None
        if is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    )
    return build_generation_diagnostics(
        requested_params=state.requested_params_for_diag,
        provider=provider,
        upstream_route=state.image_route,
        provider_attempts=state.provider_attempt_log,
        upstream_duration_ms=state.upstream_duration_ms,
        duration_ms=int(max(0.0, time.monotonic() - state.task_start) * 1000),
        debug_id=state.task_id,
        error_summary=safe_summary,
        expose_provider_diagnostics=g.queue.expose_provider_diagnostics,
    )


def _error_upstream_request(
    state: GenerationRunState,
    diagnostics: dict[str, Any],
    safe_summary: str,
    g: RunGenerationDeps,
) -> dict[str, Any]:
    request = dict(state.gen_upstream_request_snapshot or {})
    request.update(
        {
            "upstream_route": state.image_route,
            "generation_diagnostics": diagnostics,
            "requested_params": state.requested_params_for_diag,
            "debug_id": state.task_id,
            "safe_error_summary": safe_summary,
        }
    )
    if state.provider_attempt_log:
        request["provider_attempts"] = state.provider_attempt_log[:12]
    if state.upstream_duration_ms is not None:
        request["upstream_duration_ms"] = state.upstream_duration_ms
    state_execution = getattr(state, "sidecar_execution", None)
    if isinstance(state_execution, dict):
        request[SIDECAR_EXECUTION_KEY] = dict(state_execution)
    provider = (
        None
        if is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    ) or request_event_provider_from_attempts(
        state.provider_attempt_log,
        redis_text=redis_text,
    )
    if provider:
        request["request_event_provider"] = provider
    else:
        request.pop("request_event_provider", None)
    return sanitize_generation_upstream_request(
        request,
        expose_provider_diagnostics=g.queue.expose_provider_diagnostics,
    )


async def _apply_moderation_retry_policy(
    state: GenerationRunState,
    exc: Exception,
    failure: GenerationFailure,
    g: RunGenerationDeps,
) -> GenerationFailure:
    if not _can_upgrade_moderation_retry(state, exc, failure, g):
        return failure
    enabled_count = await _enabled_provider_count(g)
    avoided = (
        await get_avoided_providers(
            state.redis,
            state.task_id,
            services=g,
        )
        if enabled_count > 1
        else set()
    )
    upgraded = decide_moderation_retry_upgrade(
        base_decision=failure.decision,
        err_code=getattr(exc, "error_code", None),
        err_msg=failure.error_message,
        is_dual_race=state.is_dual_race,
        reserved_provider_name=state.reserved_provider_name,
        enabled_provider_count=enabled_count,
        already_avoided_count=len(avoided),
    )
    if upgraded is None:
        return failure
    _log_moderation_upgrade(state, enabled_count, len(avoided), g)
    failure.decision = upgraded
    failure.moderation_upgrade = True
    failure.effective_max_attempts = max(
        state.attempt + 1,
        min(MODERATION_RETRY_CAP, max(1, enabled_count)),
    )
    return failure


def _can_upgrade_moderation_retry(
    state: GenerationRunState,
    exc: Exception,
    failure: GenerationFailure,
    g: RunGenerationDeps,
) -> bool:
    return bool(
        not failure.decision.retriable
        and not is_dual_race_sentinel(state.reserved_provider_name)
        and state.reserved_provider_name
        and is_moderation_block(
            getattr(exc, "error_code", None),
            failure.error_message,
        )
    )


async def _enabled_provider_count(g: RunGenerationDeps) -> int:
    try:
        from ...provider_pool import get_pool

        pool = await get_pool()
        return len(pool.enabled_provider_names())
    except Exception:  # noqa: BLE001
        return 0


def _log_moderation_upgrade(
    state: GenerationRunState,
    enabled_count: int,
    avoided_count: int,
    g: RunGenerationDeps,
) -> None:
    logger.info(
        "moderation retry upgrade task=%s attempt=%s from_provider=%s "
        "enabled=%d avoided=%d cap=%d",
        state.task_id,
        state.attempt,
        state.reserved_provider_name,
        enabled_count,
        avoided_count,
        MODERATION_RETRY_CAP,
    )


async def _retry_generation(
    state: GenerationRunState,
    failure: GenerationFailure,
    g: RunGenerationDeps,
) -> None:
    await _avoid_failed_provider(state, g)
    delay = retry_delay_seconds(state.attempt)
    if not await _persist_retry_state(state, failure, g):
        return
    await cancel_renewer_task(state.renewer)
    state.renewer = None
    await release_lease(state.redis, state.task_id, state.lease_token)
    if not await _enqueue_retry(state, failure, delay, g):
        return
    await _publish_retry_events(state, failure, delay, g)


async def _avoid_failed_provider(
    state: GenerationRunState, g: RunGenerationDeps
) -> None:
    if isinstance(getattr(state, "sidecar_execution", None), dict):
        return
    provider = state.reserved_provider_name
    if provider and not is_dual_race_sentinel(provider):
        await avoid_provider_for_task(
            state.redis,
            state.task_id,
            provider,
            services=g,
        )


async def _persist_retry_state(
    state: GenerationRunState,
    failure: GenerationFailure,
    g: RunGenerationDeps,
) -> bool:
    try:
        async with g.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    state.task_id,
                    state.attempt,
                    statuses=RUNNING_GENERATION_STATUSES,
                ).values(
                    status=GenerationStatus.QUEUED.value,
                    progress_stage=GenerationStage.QUEUED,
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                    upstream_request=failure.upstream_request,
                )
            )
            ensure_generation_updated(
                result,
                state.task_id,
                state.attempt,
            )
            await session.commit()
        return True
    except StaleGenerationAttempt as exc:
        logger.info(
            "generation retry stale attempt task=%s attempt=%s err=%s",
            state.task_id,
            state.attempt,
            exc,
        )
        state.task_outcome = "stale_attempt"
        return False


async def _enqueue_retry(
    state: GenerationRunState,
    failure: GenerationFailure,
    delay: float,
    g: RunGenerationDeps,
) -> bool:
    try:
        await state.redis.set(
            image_queue_not_before_key(state.task_id),
            str(time.time() + delay),
            ex=retry_not_before_ttl(delay),
        )
        enqueued = await enqueue_generation_once(
            state.redis,
            state.task_id,
            attempt=state.attempt + 1,
            defer_by=delay,
            job_try=state.attempt + 1,
            replace_dispatch=state.dispatch_identity,
            services=g,
        )
        if not enqueued:
            raise RuntimeError("generation retry dispatch was not accepted")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "re-enqueue failed task=%s err=%s",
            state.task_id,
            exc,
        )
        await mark_generation_attempt_failed(
            state.redis,
            task_id=state.task_id,
            message_id=state.message_id,
            user_id=state.user_id,
            attempt=state.attempt,
            error_code="retry_enqueue_failed",
            error_message=f"failed to enqueue retry: {exc}"[:2000],
            retriable=False,
            statuses=(
                GenerationStatus.QUEUED.value,
                GenerationStatus.RUNNING.value,
            ),
            services=g,
        )
        state.task_outcome = "failed"
        return False


async def _publish_retry_events(
    state: GenerationRunState,
    failure: GenerationFailure,
    delay: float,
    g: RunGenerationDeps,
) -> None:
    if failure.moderation_upgrade:
        await g.events.publish(
            state.redis,
            state.user_id,
            state.channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": state.task_id,
                "message_id": state.message_id,
                "stage": GenerationStage.RENDERING.value,
                "substage": GenerationStage.PROVIDER_SELECTED.value,
                "provider_failover": True,
                "from_provider": state.reserved_provider_name,
                "reason": "moderation_retry",
                "route": "image",
            },
        )
    await g.events.publish(
        state.redis,
        state.user_id,
        task_channel(state.task_id),
        EV_GEN_RETRYING,
        {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "attempt": state.attempt,
            "max_attempts": failure.effective_max_attempts,
            "retry_delay_seconds": delay,
            "error_code": failure.error_code,
            "error_message": failure.error_message,
            **(
                {"error_details": failure.error_details}
                if failure.error_details
                else {}
            ),
        },
    )


async def _fail_generation_terminal(
    state: GenerationRunState,
    failure: GenerationFailure,
    g: RunGenerationDeps,
) -> None:
    try:
        async with g.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    state.task_id,
                    state.attempt,
                    statuses=RUNNING_GENERATION_STATUSES,
                ).values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                    upstream_request=failure.upstream_request,
                )
            )
            ensure_generation_updated(
                result,
                state.task_id,
                state.attempt,
            )
            await _mark_message_and_release_billing(
                session,
                state,
                failure.error_code,
                g,
                exc=failure.exc,
            )
            delivery = stage_generation_failure_event(
                session,
                state.user_id,
                state.channel,
                generation_id=state.task_id,
                message_id=state.message_id,
                code=failure.error_code,
                message=failure.error_message,
                diagnostics=failure.diagnostics,
                safe_error_summary=failure.safe_error_summary,
                error_details=failure.error_details,
            )
            await session.commit()
            await g.billing.flush_after_commit(session)
    except StaleGenerationAttempt as exc:
        logger.info(
            "generation terminal stale attempt task=%s attempt=%s err=%s",
            state.task_id,
            state.attempt,
            exc,
        )
        state.task_outcome = "stale_attempt"
        return
    await g.events.deliver(state.redis, delivery)


async def _mark_message_and_release_billing(
    session: Any,
    state: GenerationRunState,
    error_code: str,
    g: RunGenerationDeps,
    *,
    exc: BaseException | None = None,
) -> None:
    message = await session.get(Message, state.message_id)
    if message is not None and message.status != MessageStatus.CANCELED:
        message.status = MessageStatus.FAILED
    generation = await session.get(Generation, state.task_id)
    if generation is None:
        return
    state_execution = getattr(state, "sidecar_execution", None)
    if isinstance(state_execution, dict):
        generation.upstream_request = {
            **(
                dict(generation.upstream_request)
                if isinstance(generation.upstream_request, dict)
                else {}
            ),
            SIDECAR_EXECUTION_KEY: dict(state_execution),
        }
    decision = decide_image_failure_billing(error_code)
    if decision.released:
        # 决策表 release 前提「非 unknown 码 = 适配层已证明上游未计费」只对
        # UpstreamError 成立;本地失败(artifact commit 未被采纳、存储/DB 错误等)
        # 已收到当前 dispatch 的响应时上游必然已计费,必须结算而不是退款。
        if release_would_absorb_upstream_cost(exc, generation):
            await g.billing.settle_unknown_upstream(
                session,
                generation,
                reason=error_code,
                knowledge="incurred",
            )
            return
        await release_or_settle_generation(
            g.billing,
            session,
            generation,
            reason=error_code,
        )
        return
    await g.billing.settle_unknown_upstream(
        session,
        generation,
        reason=error_code,
        knowledge=str(decision.knowledge),
    )
