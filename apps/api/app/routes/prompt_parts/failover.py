"""Provider and model failover for prompt enhancement streams."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from lumen_core.providers import ProviderDefinition

from ...task_billing import EnhanceBillingContext, EnhanceUsageCapture
from .upstream import EnhanceAttempt, EnhanceProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamRuntime:
    stream_one: Callable[..., AsyncIterator[str]]
    charge: Callable[[EnhanceBillingContext, EnhanceUsageCapture], Awaitable[None]]
    release: Callable[..., Awaitable[None]]
    release_after_cancel: Callable[..., Awaitable[None]]


@dataclass
class _FailoverState:
    last_error: str = "upstream_error"
    settled: bool = False


@dataclass
class _CandidateState:
    emitted: bool = False
    succeeded: bool = False
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
) -> AsyncIterator[str]:
    try:
        async for chunk in runtime.stream_one(
            text,
            provider,
            attempt,
            capture,
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
    except Exception:
        logger.exception("prompt enhance billing charge failed")
        await runtime.release(billing, reason="charge_failed")
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
        async for chunk in _candidate_chunks(
            text,
            provider,
            attempt,
            capture,
            candidate,
            runtime=runtime,
            stream_kwargs=stream_kwargs,
        ):
            yield chunk
        if candidate.succeeded:
            yield await _success_chunk(billing, capture, runtime=runtime)
            state.settled = True
            return
        state.last_error = _candidate_error(candidate)
        _log_provider_failure(
            candidate,
            provider=provider,
            attempt=attempt,
            remaining=len(candidates) - index,
        )
        if _candidate_should_stop(candidate):
            await runtime.release(billing, reason=_release_reason(candidate))
            state.settled = True
            yield _error_chunk(state.last_error)
            return
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
    except asyncio.CancelledError:
        if not state.settled:
            await runtime.release_after_cancel(billing, reason="stream_cancelled")
        raise
    except GeneratorExit:
        if not state.settled:
            await runtime.release(billing, reason="stream_cancelled")
        raise
