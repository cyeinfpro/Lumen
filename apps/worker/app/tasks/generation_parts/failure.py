from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .runtime import GenerationRunState


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


async def handle_lease_lost(
    state: GenerationRunState,
    exc: BaseException,
    g: Any,
) -> None:
    g.logger.warning(
        "generation lease lost task=%s attempt=%s err=%s",
        state.task_id,
        state.attempt,
        exc,
    )
    if state.attempt >= g._MAX_ATTEMPTS:
        await g._mark_generation_attempt_failed(
            state.redis,
            task_id=state.task_id,
            message_id=state.message_id,
            user_id=state.user_id,
            attempt=state.attempt,
            error_code="lease_lost_max_attempts",
            error_message="lease lost after max attempts",
            retriable=False,
        )
        state.task_outcome = "failed"
        return
    delay = g._retry_delay_seconds(state.attempt)
    requeued = await g._mark_generation_attempt_retrying(
        state.redis,
        task_id=state.task_id,
        message_id=state.message_id,
        user_id=state.user_id,
        attempt=state.attempt,
        error_code="lease_lost",
        error_message="generation lease lost; task will be retried",
        delay=delay,
        reason="lease_lost",
        max_attempts=g._MAX_ATTEMPTS,
    )
    state.task_outcome = "retry" if requeued else "lease_lost"


async def handle_stale_attempt(
    state: GenerationRunState,
    exc: BaseException,
    g: Any,
) -> None:
    g.logger.info(
        "generation stale attempt task=%s attempt=%s err=%s",
        state.task_id,
        state.attempt,
        exc,
    )
    requeued = await g._maybe_requeue_stale_generation_attempt(
        state.redis,
        task_id=state.task_id,
        attempt=state.attempt,
        reason=type(exc).__name__,
    )
    state.task_outcome = "retry" if requeued else "stale_attempt"


async def handle_cancel(
    state: GenerationRunState,
    exc: BaseException,
    g: Any,
) -> None:
    state.task_outcome = await g._finalize_running_generation_cancel(
        state.redis,
        task_id=state.task_id,
        message_id=state.message_id,
        user_id=state.user_id,
        attempt=state.attempt,
        reason=exc,
    )


async def handle_generation_exception(
    state: GenerationRunState,
    exc: Exception,
    g: Any,
) -> None:
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
    g: Any,
) -> GenerationFailure:
    decision = g._classify_exception(exc, state.has_partial)
    _byok_terminal, runtime_byok_error = g.classify_user_credential_error(exc)
    if state.user_api_credential_id and runtime_byok_error:
        await g.record_user_credential_runtime_error(
            state.user_api_credential_id,
            exc,
        )
        decision = g.RetryDecision(False, f"byok {runtime_byok_error}")
    _log_generation_failure(state, exc, decision, g)
    error_code, error_message = _generation_error_identity(
        state,
        exc,
        runtime_byok_error,
        g,
    )
    error_details = g._safe_generation_error_details(exc)
    safe_summary = g._safe_generation_error_summary(
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
        effective_max_attempts=g._MAX_ATTEMPTS,
    )


def _log_generation_failure(
    state: GenerationRunState,
    exc: Exception,
    decision: Any,
    g: Any,
) -> None:
    error_code = getattr(exc, "error_code", None) or type(exc).__name__
    status = getattr(exc, "status_code", None)
    provider = (getattr(exc, "payload", None) or {}).get("provider", "")
    g.logger.warning(
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
    g.logger.debug(
        "generation exc trace task=%s",
        state.task_id,
        exc_info=True,
    )


def _generation_error_identity(
    state: GenerationRunState,
    exc: Exception,
    runtime_byok_error: str | None,
    g: Any,
) -> tuple[str, str]:
    if state.user_api_credential_id and runtime_byok_error:
        return (
            g.byok_error_to_generation_code(runtime_byok_error),
            g.byok_error_message(runtime_byok_error),
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
    g: Any,
) -> dict[str, Any]:
    provider = (
        None
        if g._is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    )
    return g._build_generation_diagnostics(
        requested_params=state.requested_params_for_diag,
        provider=provider,
        upstream_route=state.image_route,
        provider_attempts=state.provider_attempt_log,
        upstream_duration_ms=state.upstream_duration_ms,
        duration_ms=int(max(0.0, g.time.monotonic() - state.task_start) * 1000),
        debug_id=state.task_id,
        error_summary=safe_summary,
        expose_provider_diagnostics=g.settings.expose_provider_diagnostics,
    )


def _error_upstream_request(
    state: GenerationRunState,
    diagnostics: dict[str, Any],
    safe_summary: str,
    g: Any,
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
    provider = (
        None
        if g._is_dual_race_sentinel(state.reserved_provider_name)
        else state.reserved_provider_name
    ) or g._request_event_provider_from_attempts(state.provider_attempt_log)
    if provider:
        request["request_event_provider"] = provider
    else:
        request.pop("request_event_provider", None)
    return g._sanitize_generation_upstream_request(
        request,
        expose_provider_diagnostics=g.settings.expose_provider_diagnostics,
    )


async def _apply_moderation_retry_policy(
    state: GenerationRunState,
    exc: Exception,
    failure: GenerationFailure,
    g: Any,
) -> GenerationFailure:
    if not _can_upgrade_moderation_retry(state, exc, failure, g):
        return failure
    enabled_count = await _enabled_provider_count(g)
    avoided = (
        await g._get_avoided_providers(state.redis, state.task_id)
        if enabled_count > 1
        else set()
    )
    upgraded = g._decide_moderation_retry_upgrade(
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
        min(g._MODERATION_RETRY_CAP, max(1, enabled_count)),
    )
    return failure


def _can_upgrade_moderation_retry(
    state: GenerationRunState,
    exc: Exception,
    failure: GenerationFailure,
    g: Any,
) -> bool:
    return bool(
        not failure.decision.retriable
        and not g._is_dual_race_sentinel(state.reserved_provider_name)
        and state.reserved_provider_name
        and g.is_moderation_block(
            getattr(exc, "error_code", None),
            failure.error_message,
        )
    )


async def _enabled_provider_count(g: Any) -> int:
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
    g: Any,
) -> None:
    g.logger.info(
        "moderation retry upgrade task=%s attempt=%s from_provider=%s "
        "enabled=%d avoided=%d cap=%d",
        state.task_id,
        state.attempt,
        state.reserved_provider_name,
        enabled_count,
        avoided_count,
        g._MODERATION_RETRY_CAP,
    )


async def _retry_generation(
    state: GenerationRunState,
    failure: GenerationFailure,
    g: Any,
) -> None:
    await _avoid_failed_provider(state, g)
    delay = g._retry_delay_seconds(state.attempt)
    if not await _persist_retry_state(state, failure, g):
        return
    await g._cancel_renewer_task(state.renewer)
    state.renewer = None
    await g._release_lease(state.redis, state.task_id, state.lease_token)
    if not await _enqueue_retry(state, failure, delay, g):
        return
    await _publish_retry_events(state, failure, delay, g)


async def _avoid_failed_provider(state: GenerationRunState, g: Any) -> None:
    provider = state.reserved_provider_name
    if provider and not g._is_dual_race_sentinel(provider):
        await g._avoid_provider_for_task(state.redis, state.task_id, provider)


async def _persist_retry_state(
    state: GenerationRunState,
    failure: GenerationFailure,
    g: Any,
) -> bool:
    try:
        async with g.SessionLocal() as session:
            result = await session.execute(
                g._generation_attempt_update(
                    state.task_id,
                    state.attempt,
                    statuses=g._RUNNING_GENERATION_STATUSES,
                ).values(
                    status=g.GenerationStatus.QUEUED.value,
                    progress_stage=g.GenerationStage.QUEUED,
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                    upstream_request=failure.upstream_request,
                )
            )
            g._ensure_generation_updated(
                result,
                state.task_id,
                state.attempt,
            )
            await session.commit()
        return True
    except g._StaleGenerationAttempt as exc:
        g.logger.info(
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
    g: Any,
) -> bool:
    try:
        await state.redis.set(
            g._image_queue_not_before_key(state.task_id),
            str(g.time.time() + delay),
            ex=g._retry_not_before_ttl(delay),
        )
        await state.redis.enqueue_job(
            "run_generation",
            state.task_id,
            _defer_by=delay,
            _job_try=state.attempt + 1,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        g.logger.error(
            "re-enqueue failed task=%s err=%s",
            state.task_id,
            exc,
        )
        await g._mark_generation_attempt_failed(
            state.redis,
            task_id=state.task_id,
            message_id=state.message_id,
            user_id=state.user_id,
            attempt=state.attempt,
            error_code="retry_enqueue_failed",
            error_message=f"failed to enqueue retry: {exc}"[:2000],
            retriable=False,
            statuses=(
                g.GenerationStatus.QUEUED.value,
                g.GenerationStatus.RUNNING.value,
            ),
        )
        state.task_outcome = "failed"
        return False


async def _publish_retry_events(
    state: GenerationRunState,
    failure: GenerationFailure,
    delay: float,
    g: Any,
) -> None:
    if failure.moderation_upgrade:
        await g.publish_event(
            state.redis,
            state.user_id,
            state.channel,
            g.EV_GEN_PROGRESS,
            {
                "generation_id": state.task_id,
                "message_id": state.message_id,
                "stage": g.GenerationStage.RENDERING.value,
                "substage": g.GenerationStage.PROVIDER_SELECTED.value,
                "provider_failover": True,
                "from_provider": state.reserved_provider_name,
                "reason": "moderation_retry",
                "route": "image",
            },
        )
    await g.publish_event(
        state.redis,
        state.user_id,
        g.task_channel(state.task_id),
        g.EV_GEN_RETRYING,
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
    g: Any,
) -> None:
    try:
        async with g.SessionLocal() as session:
            result = await session.execute(
                g._generation_attempt_update(
                    state.task_id,
                    state.attempt,
                    statuses=g._RUNNING_GENERATION_STATUSES,
                ).values(
                    status=g.GenerationStatus.FAILED.value,
                    progress_stage=g.GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                    upstream_request=failure.upstream_request,
                )
            )
            g._ensure_generation_updated(
                result,
                state.task_id,
                state.attempt,
            )
            await _mark_message_and_release_billing(
                session,
                state,
                failure.error_code,
                g,
            )
            delivery = g._stage_generation_failure_event(
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
            await g.worker_billing.flush_balance_cache_refreshes(session)
    except g._StaleGenerationAttempt as exc:
        g.logger.info(
            "generation terminal stale attempt task=%s attempt=%s err=%s",
            state.task_id,
            state.attempt,
            exc,
        )
        state.task_outcome = "stale_attempt"
        return
    await g._deliver_generation_event(state.redis, delivery)


async def _mark_message_and_release_billing(
    session: Any,
    state: GenerationRunState,
    error_code: str,
    g: Any,
) -> None:
    message = await session.get(g.Message, state.message_id)
    if message is not None and message.status != g.MessageStatus.CANCELED:
        message.status = g.MessageStatus.FAILED
    generation = await session.get(g.Generation, state.task_id)
    if generation is not None:
        await g.worker_billing.release_generation(
            session,
            generation,
            reason=error_code,
        )
