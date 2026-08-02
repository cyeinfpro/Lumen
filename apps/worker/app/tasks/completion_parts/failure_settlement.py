"""Cancellation, retry, and terminal failure settlement for completions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lumen_core.chat_tools import ToolStatus
from lumen_core.constants import (
    EV_COMP_FAILED,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    MessageStatus,
)

from ...reconciliation.task_domains import (
    CompletionDispatchResultUnknown,
    completion_cancel_requires_unknown_settlement,
    ensure_completion_execution_current,
    settle_completion_cancel_unknown,
    settle_completion_result_unknown,
)
from .contracts import CompletionCommand, CompletionOutcome, CompletionServices
from .execution import CompletionExecution


async def _cancel_completion_row(
    state: CompletionExecution,
) -> tuple[str, str, dict[str, Any]] | None:
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
                state.ports.persistence.Completion.cancel_requested_at.is_not(None),
            )
            .values(
                status=CompletionStatus.CANCELED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=EC.CANCELLED.value,
                error_message="cancelled by user",
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion cancel superseded task={state.request.task_id} "
                f"attempt_epoch={state.preparation.attempt_epoch}"
            )
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status not in (
            MessageStatus.SUCCEEDED,
            MessageStatus.FAILED,
            MessageStatus.CANCELED,
        ):
            tool_calls = state.usage.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        completion = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if completion is not None:
            await state.ports.billing._settle_cancelled_completion_billing(
                session,
                completion,
                has_partial=state.streaming.has_partial,
                input_list=state.streaming.input_list
                if state.usage.request_sent
                else None,
                instructions=state.streaming.instructions
                if state.usage.request_sent
                else None,
                usage_is_finalized=True,
                accumulated_text=state.streaming.accumulated_text,
                tokens_in=state.usage.usage_totals.tokens_in,
                tokens_out=state.usage.usage_totals.tokens_out,
                cache_read_tokens=state.usage.usage_totals.cache_read_tokens,
                cache_creation_tokens=state.usage.usage_totals.cache_creation_tokens,
                cache_creation_5m_tokens=state.usage.usage_totals.cache_creation_5m_tokens,
                cache_creation_1h_tokens=state.usage.usage_totals.cache_creation_1h_tokens,
                reasoning_tokens=state.usage.usage_totals.reasoning_tokens,
                image_output_tokens=state.usage.usage_totals.image_output_tokens,
                tool_images=state.streaming.tool_images,
                reserved_tool_image_budget_micro=(
                    state.streaming.reserved_tool_image_budget_micro
                ),
                reason=EC.CANCELLED.value,
            )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code="cancelled",
                message="cancelled by user",
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
        return delivery


async def settle_cancelled_completion(state: CompletionExecution) -> None:
    state.usage.usage_totals.finish_round(
        output_text=state.streaming.accumulated_text[state.usage.round_text_start :],
        reasoning_text=state.streaming.accumulated_thinking[
            state.usage.round_thinking_start :
        ],
        tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
            state.usage.tool_tracker.content()
        ),
    )
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(ToolStatus.CANCELLED.value),
    )
    delivery: tuple[str, str, dict[str, Any]] | None = None
    try:
        delivery = await _cancel_completion_row(state)
    except state.ports.retry._CompletionEpochSuperseded as exc:
        state.ports.events.logger.info(
            "completion cancel skipped by newer epoch task=%s attempt_epoch=%s err=%s",
            state.request.task_id,
            state.preparation.attempt_epoch,
            exc,
        )
        state.settlement.task_outcome = "superseded"
        return
    except Exception as exc:  # noqa: BLE001
        state.ports.events.logger.warning(
            "completion cancel DB update failed task=%s err=%s",
            state.request.task_id,
            exc,
        )
    if delivery is not None:
        await state.ports.events._deliver_completion_event(
            state.request.redis, delivery
        )
    state.settlement.task_outcome = "failed"


def _failure_details(
    state: CompletionExecution,
    exc: BaseException,
) -> tuple[Any, str, str]:
    decision = state.ports.retry._classify_exception(exc, state.streaming.has_partial)
    _, byok_error = state.ports.billing.classify_user_credential_error(exc)
    if state.preparation.user_api_credential_id and byok_error:
        decision = state.ports.retry.RetryDecision(False, f"byok {byok_error}")
        err_code = state.ports.billing.byok_error_to_generation_code(byok_error)
        err_msg = state.ports.billing.byok_error_message(byok_error)
    else:
        err_code = (
            getattr(exc, "error_code", None)
            or getattr(exc, "code", None)
            or type(exc).__name__
        )
        err_msg = str(getattr(exc, "message", None) or exc)[:2000]
    return decision, str(err_code), err_msg


async def _mark_retry_queued(
    state: CompletionExecution,
    *,
    err_code: str,
    err_msg: str,
) -> bool:
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
                state.ports.persistence.Completion.cancel_requested_at.is_(None),
            )
            .values(
                status=CompletionStatus.QUEUED.value,
                progress_stage=CompletionStage.QUEUED,
                error_code=err_code,
                error_message=err_msg,
            )
        )
        await session.commit()
        if state.ports.persistence.affected_rows(result) == 0:
            state.ports.events.logger.info(
                "completion retry skipped by newer epoch task=%s attempt_epoch=%s",
                state.request.task_id,
                state.preparation.attempt_epoch,
            )
            state.settlement.task_outcome = "superseded"
            return False
    return True


async def _settle_retry_enqueue_failure(
    state: CompletionExecution,
    *,
    enqueue_msg: str,
) -> None:
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error=enqueue_msg,
        ),
    )
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status
                == CompletionStatus.QUEUED.value,
            )
            .values(
                status=CompletionStatus.FAILED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code="retry_enqueue_failed",
                error_message=enqueue_msg,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            await session.commit()
            state.settlement.task_outcome = "superseded"
            return
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            message.status = MessageStatus.FAILED
        completion = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if completion is not None:
            await state.ports.billing.worker_billing.release_completion(
                session,
                completion,
                reason="retry_enqueue_failed",
            )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code="retry_enqueue_failed",
                message=enqueue_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "failed"


async def _settle_terminal_failure(
    state: CompletionExecution,
    *,
    err_code: str,
    err_msg: str,
) -> None:
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error=err_msg,
        ),
    )
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
                state.ports.persistence.Completion.cancel_requested_at.is_(None),
            )
            .values(
                status=CompletionStatus.FAILED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=err_code,
                error_message=err_msg,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            await session.commit()
            state.settlement.task_outcome = "superseded"
            return
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            tool_calls = state.usage.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        if (
            state.streaming.has_partial
            or state.streaming.tool_loop_truncated
            or any(state.usage.usage_totals.values())
        ):
            completion = await session.get(
                state.ports.persistence.Completion, state.request.task_id
            )
            if completion is not None:
                if (
                    state.streaming.tool_images
                    and state.usage.usage_totals.image_output_tokens <= 0
                    and state.streaming.reserved_tool_image_budget_micro > 0
                ):
                    state.usage.usage_totals.image_output_tokens = await state.ports.billing._fallback_completion_tool_image_tokens(
                        session,
                        completion,
                        budget_micro=state.streaming.reserved_tool_image_budget_micro,
                    )
                    state.usage.usage_totals.tokens_out = max(
                        state.usage.usage_totals.tokens_out,
                        state.usage.usage_totals.image_output_tokens,
                    )
                state.usage.usage_totals.apply_to(completion)
                await state.ports.billing._settle_failed_completion_billing(
                    session,
                    completion,
                    usage_values=state.usage.usage_totals.values(),
                    reason=str(err_code),
                )
        else:
            completion = await session.get(
                state.ports.persistence.Completion, state.request.task_id
            )
            if completion is not None:
                await state.ports.billing.worker_billing.release_completion(
                    session,
                    completion,
                    reason=str(err_code),
                )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code=err_code,
                message=err_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "failed"


async def handle_completion_failure(
    state: CompletionExecution,
    exc: BaseException,
) -> None:
    if isinstance(exc, CompletionDispatchResultUnknown):
        await settle_completion_result_unknown(state)
        return
    try:
        await state.ports.retry._raise_if_completion_cancelled(
            state.request.redis,
            state.request.task_id,
            "cancelled while settling completion failure",
        )
    except state.ports.retry._TaskCancelled:
        if state.usage.usage_totals is None or state.usage.tool_tracker is None:
            state.settlement.task_outcome = "cancelled"
            return
        if await completion_cancel_requires_unknown_settlement(state):
            await settle_completion_cancel_unknown(state)
        else:
            await settle_cancelled_completion(state)
        return
    if state.streaming.has_partial or state.streaming.tool_loop_truncated:
        state.usage.usage_totals.finish_round(
            output_text=state.streaming.accumulated_text[
                state.usage.round_text_start :
            ],
            reasoning_text=state.streaming.accumulated_thinking[
                state.usage.round_thinking_start :
            ],
            tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
                state.usage.tool_tracker.content()
            ),
        )
    if isinstance(exc, state.ports.retry._ToolIdleTimeout):
        await state.ports.tools._publish_completion_tool_updates(
            redis=state.request.redis,
            user_id=state.preparation.user_id,
            channel=state.request.channel,
            task_id=state.request.task_id,
            message_id=state.preparation.message_id,
            attempt=state.preparation.attempt,
            attempt_epoch=state.preparation.attempt_epoch,
            tool_tracker=state.usage.tool_tracker,
            updates=state.usage.tool_tracker.finalize_active(
                ToolStatus.TIMED_OUT.value,
                error="tool call idle timeout",
            ),
        )
        exc = state.ports.upstream.UpstreamError(
            "tool call idle timeout",
            error_code=EC.TIMEOUT.value,
            status_code=200,
        )
    state.ports.events.upstream_calls_total.labels(
        kind="completion", outcome="error"
    ).inc()
    decision, err_code, err_msg = _failure_details(state, exc)
    _, byok_error = state.ports.billing.classify_user_credential_error(exc)
    if state.preparation.user_api_credential_id and byok_error:
        await state.ports.billing.record_user_credential_runtime_error(
            state.preparation.user_api_credential_id,
            exc,
        )
    state.ports.events.logger.warning(
        "completion failed task=%s attempt=%s retriable=%s reason=%s "
        "error_code=%s http_status=%s",
        state.request.task_id,
        state.preparation.attempt,
        decision.retriable,
        decision.reason,
        err_code,
        getattr(exc, "status_code", None),
    )
    state.ports.events.logger.debug(
        "completion exc trace task=%s", state.request.task_id, exc_info=True
    )
    if (
        decision.retriable
        and state.preparation.attempt < state.ports.retry._MAX_ATTEMPTS
    ):
        state.settlement.task_outcome = "retry"
        delay_index = min(
            state.preparation.attempt - 1,
            len(state.ports.retry.RETRY_BACKOFF_SECONDS) - 1,
        )
        delay = state.ports.retry.RETRY_BACKOFF_SECONDS[delay_index]
        if not await _mark_retry_queued(
            state,
            err_code=err_code,
            err_msg=err_msg,
        ):
            return
        try:
            await state.request.redis.enqueue_job(
                "run_completion",
                state.request.task_id,
                _defer_by=delay,
                _job_try=state.preparation.attempt + 1,
            )
        except Exception as enqueue_exc:  # noqa: BLE001
            state.ports.events.logger.error(
                "re-enqueue failed task=%s err=%s",
                state.request.task_id,
                enqueue_exc,
            )
            await _settle_retry_enqueue_failure(
                state,
                enqueue_msg=f"failed to enqueue retry: {enqueue_exc}"[:2000],
            )
        return
    await _settle_terminal_failure(
        state,
        err_code=err_code,
        err_msg=err_msg,
    )


async def handle_completion_run_failure(
    command: CompletionCommand,
    state: CompletionExecution,
    services: CompletionServices,
    failure: BaseException,
) -> None:
    """Settle a failure from the top-level completion execution lifecycle."""

    if isinstance(failure, CompletionDispatchResultUnknown):
        try:
            await settle_completion_result_unknown(state)
        except state.ports.retry._CompletionEpochSuperseded as stale:
            state.ports.events.logger.info(
                "completion result_unknown superseded task=%s err=%s",
                command.task_id,
                stale,
            )
            services.events.record_outcome(state, CompletionOutcome.SUPERSEDED)
    elif services.lease_retry.is_lease_lost(failure):
        state.ports.events.logger.warning(
            "completion lease lost task=%s attempt=%s err=%s",
            command.task_id,
            state.preparation.attempt,
            failure,
        )
        services.events.record_outcome(state, CompletionOutcome.LEASE_LOST)
    elif services.lease_retry.is_superseded(failure):
        state.ports.events.logger.info(
            "completion worker superseded task=%s err=%s",
            command.task_id,
            failure,
        )
        services.events.record_outcome(state, CompletionOutcome.SUPERSEDED)
    elif services.lease_retry.is_cancelled(failure):
        try:
            await ensure_completion_execution_current(state)
        except state.ports.retry._CompletionEpochSuperseded as stale:
            state.ports.events.logger.info(
                "completion cancel settlement superseded task=%s err=%s",
                command.task_id,
                stale,
            )
            services.events.record_outcome(state, CompletionOutcome.SUPERSEDED)
        else:
            state.ports.events.logger.info(
                "completion cancelled by user task=%s reason=%s",
                command.task_id,
                failure,
            )
            if await completion_cancel_requires_unknown_settlement(state):
                await settle_completion_cancel_unknown(state)
            else:
                await services.billing.settle_cancelled(state)
    elif isinstance(failure, Exception):
        try:
            await ensure_completion_execution_current(state)
        except state.ports.retry._CompletionEpochSuperseded as stale:
            state.ports.events.logger.info(
                "completion failure settlement superseded task=%s err=%s",
                command.task_id,
                stale,
            )
            services.events.record_outcome(state, CompletionOutcome.SUPERSEDED)
        else:
            await services.billing.settle_failure(state, failure)
    else:
        raise failure


__all__ = [
    "handle_completion_failure",
    "settle_cancelled_completion",
]
