"""Provider and model failover for prompt enhancement streams."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from lumen_core.providers import ProviderDefinition

from ...audit import AuditPersistenceError
from ...task_billing import EnhanceBillingContext, EnhanceUsageCapture
from .upstream import EnhanceAttempt, EnhanceProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamRuntime:
    stream_one: Callable[..., AsyncIterator[str]]
    charge: Callable[[EnhanceBillingContext, EnhanceUsageCapture], Awaitable[None]]
    release: Callable[..., Awaitable[None]]
    release_after_cancel: Callable[..., Awaitable[None]]
    settle_default: Callable[..., Awaitable[None]]
    settle_default_after_cancel: Callable[..., Awaitable[None]]


@dataclass
class _FailoverState:
    last_error: str = "upstream_error"
    settled: bool = False
    # 任一候选的 POST 已真正发出(请求字节已写入 socket,进入响应阶段):
    # 取消/断流时按「上游可能已扣费」结算;未发出 = 可证明上游未产生费用,
    # 取消时应释放 hold。由 upstream 在 client.stream 上下文内回调置位——
    # 候选启动(代理解析/连接建立)不等于已发送,不能提前置位。
    dispatched: bool = False
    # 上游成本可知性:True = 已交付内容 / 失败点不可知 / 读超时等已送达证据;
    # False = 所有候选均「可证明上游未产生费用」(连接层未送达、非 2xx 拒绝)。
    upstream_cost_possible: bool = False


@dataclass
class _CandidateState:
    emitted: bool = False
    succeeded: bool = False
    dispatched: bool = False
    provider_error: EnhanceProviderError | None = None
    internal_error: bool = False


def _error_chunk(error: str) -> str:
    return f"data: {json.dumps({'error': error})}\n\n"


def _stream_kwargs(
    *,
    default_system_prompt: str,
    system_prompt: str,
    content: list[dict[str, Any]] | None,
    metadata: dict[str, str] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if system_prompt != default_system_prompt:
        kwargs["system_prompt"] = system_prompt
    if content is not None:
        kwargs["content"] = content
    if metadata is not None:
        kwargs["metadata"] = metadata
    return kwargs


async def _candidate_chunks(
    text: str,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
    capture: EnhanceUsageCapture,
    candidate: _CandidateState,
    *,
    runtime: StreamRuntime,
    stream_kwargs: dict[str, Any],
    on_dispatched: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    try:
        async for chunk in runtime.stream_one(
            text,
            provider,
            attempt,
            capture,
            on_dispatched=on_dispatched,
            **stream_kwargs,
        ):
            candidate.emitted = True
            yield chunk
        candidate.succeeded = True
    except EnhanceProviderError as exc:
        candidate.provider_error = exc
    except (GeneratorExit, asyncio.CancelledError):
        raise
    except Exception:
        candidate.internal_error = True
        logger.exception(
            "enhance provider exception provider=%s attempt=%s",
            provider.name,
            attempt.name,
        )


def _candidate_error(candidate: _CandidateState) -> str:
    if candidate.provider_error is not None:
        return (
            "timeout"
            if str(candidate.provider_error) == "timeout"
            else "upstream_error"
        )
    return "internal"


def _candidate_should_stop(candidate: _CandidateState) -> bool:
    if candidate.provider_error is not None:
        return candidate.emitted or not candidate.provider_error.retryable
    return candidate.emitted


def _release_reason(candidate: _CandidateState) -> str:
    if candidate.provider_error is not None:
        return "provider_error_after_emit" if candidate.emitted else "provider_error"
    return "internal_error_after_emit"


def _candidate_upstream_cost_possible(candidate: _CandidateState) -> bool:
    """候选失败后,上游是否「可能已产生费用」。

    纯转嫁铁律与 lumen_core.upstream_billing 决策表一致:只有能**证明**上游
    未扣费(no_upstream_cost=True:连接层未送达、非 2xx 显式拒绝)才允许
    release;已产出内容、内部异常、读超时、处理中失败等一律不可知 → 必须
    按默认金额结算,不得 fail-open 释放 hold。
    """
    if candidate.emitted:
        return True
    if candidate.internal_error:
        return True
    error = candidate.provider_error
    if error is None:
        return True
    return not error.no_upstream_cost


def _log_provider_failure(
    candidate: _CandidateState,
    *,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
    remaining: int,
) -> None:
    if candidate.provider_error is None:
        return
    logger.warning(
        (
            "enhance provider failed provider=%s attempt=%s "
            "remaining=%d retryable=%s err=%s"
        ),
        provider.name,
        attempt.name,
        remaining,
        candidate.provider_error.retryable,
        candidate.provider_error,
    )


async def _success_chunk(
    billing: EnhanceBillingContext | None,
    capture: EnhanceUsageCapture,
    *,
    runtime: StreamRuntime,
) -> str:
    if billing is None:
        return "data: [DONE]\n\n"
    try:
        await runtime.charge(billing, capture)
    except AuditPersistenceError:
        # 审计与钱包变更必须同事务。审计失败时回滚待提交结算，且不得走
        # 默认金额降级结算，否则会在缺失审计的同时提交一笔不同金额。
        billing.settle_outcome.attempted = True
        rollback = getattr(billing.db, "rollback", None)
        if callable(rollback):
            await rollback()
        logger.exception("prompt enhance billing audit failed")
        return _error_chunk("billing_failed")
    except Exception:
        # 内容已完整交付、上游必然已计费:计费失败不得 fail-open 释放 hold,
        # 改为按默认金额(hold)结算,差额交给对账而不是由平台吸收。
        logger.exception("prompt enhance billing charge failed")
        await runtime.settle_default(billing, reason="charge_failed")
        return _error_chunk("billing_failed")
    return "data: [DONE]\n\n"


async def _stream_candidates(
    text: str,
    providers: list[ProviderDefinition],
    billing: EnhanceBillingContext | None,
    attempts: tuple[EnhanceAttempt, ...],
    state: _FailoverState,
    *,
    runtime: StreamRuntime,
    stream_kwargs: dict[str, Any],
) -> AsyncIterator[str]:
    candidates = [(attempt, provider) for attempt in attempts for provider in providers]
    for index, (attempt, provider) in enumerate(candidates, start=1):
        capture = EnhanceUsageCapture()
        candidate = _CandidateState()

        def _mark_dispatched(
            candidate: _CandidateState = candidate,
            state: _FailoverState = state,
        ) -> None:
            # POST 已真正发出(upstream 进入 client.stream 响应阶段时回调):
            # 之后取消/断流必须按「上游可能已扣费」结算,不得 fail-open 释放。
            candidate.dispatched = True
            state.dispatched = True

        async for chunk in _candidate_chunks(
            text,
            provider,
            attempt,
            capture,
            candidate,
            runtime=runtime,
            stream_kwargs=stream_kwargs,
            on_dispatched=_mark_dispatched,
        ):
            yield chunk
        if candidate.succeeded:
            success_chunk = await _success_chunk(billing, capture, runtime=runtime)
            state.settled = True
            yield success_chunk
            return
        state.last_error = _candidate_error(candidate)
        state.upstream_cost_possible = (
            state.upstream_cost_possible or _candidate_upstream_cost_possible(candidate)
        )
        _log_provider_failure(
            candidate,
            provider=provider,
            attempt=attempt,
            remaining=len(candidates) - index,
        )
        if _candidate_should_stop(candidate):
            reason = _release_reason(candidate)
            if _candidate_upstream_cost_possible(candidate):
                await runtime.settle_default(billing, reason=reason)
            else:
                await runtime.release(billing, reason=reason)
            state.settled = True
            yield _error_chunk(state.last_error)
            return
    if state.upstream_cost_possible:
        await runtime.settle_default(billing, reason="no_success")
    else:
        await runtime.release(billing, reason="no_success")
    state.settled = True
    yield _error_chunk(state.last_error)


async def stream_enhance(
    text: str,
    providers: list[ProviderDefinition],
    billing: EnhanceBillingContext | None,
    *,
    attempts: tuple[EnhanceAttempt, ...],
    runtime: StreamRuntime,
    default_system_prompt: str,
    system_prompt: str,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    state = _FailoverState()
    kwargs = _stream_kwargs(
        default_system_prompt=default_system_prompt,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
    )
    try:
        async for chunk in _stream_candidates(
            text,
            providers,
            billing,
            attempts,
            state,
            runtime=runtime,
            stream_kwargs=kwargs,
        ):
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        # 客户端断流/GC 关闭:仅当任一候选的 POST 已真正发出(dispatched)或
        # 已有已送达证据(upstream_cost_possible)时,上游才可能已按 token
        # 计费——按默认金额结算,不能 fail-open 释放。若请求阻塞在代理解析/
        # 连接建立等发送前阶段即被取消,POST 从未送达,可证明未扣费,释放 hold。
        # aclose() may be triggered by async-generator GC finalization after
        # the request session is already closed; settle/release via a detached
        # fresh-session task so the hold is never orphaned.
        if (
            not state.settled
            and billing is not None
            and billing.settle_outcome.attempted
        ):
            logger.warning(
                "prompt enhance cancellation recovery skipped after billing "
                "finalization attempt request_id=%s",
                billing.request_id,
            )
        elif not state.settled:
            if state.dispatched or state.upstream_cost_possible:
                await runtime.settle_default_after_cancel(
                    billing, reason="stream_cancelled"
                )
            else:
                await runtime.release_after_cancel(billing, reason="stream_cancelled")
        raise
