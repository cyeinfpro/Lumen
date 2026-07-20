from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SummaryProviderAttemptResult:
    text: str | None = None
    usage: dict[str, Any] | None = None
    error: Exception | None = None
    provider_failed: bool = False


@dataclass(frozen=True)
class SummaryUpstreamRuntime:
    get_pool: Callable[[], Awaitable[Any]]
    classify_retriable: Callable[..., Any]
    responses_call: Callable[..., Awaitable[Any]]
    response_body: Callable[..., dict[str, Any]]
    parse_response: Callable[[Any], tuple[str, dict[str, Any]]]
    provider_kwargs: Callable[[Any, float], dict[str, Any]]
    empty_output_error: Callable[[], Exception]
    logger: logging.Logger
    retry_attempts: int
    retry_backoff_s: float


async def _run_provider_attempt(
    *,
    pool: Any,
    provider: Any,
    input_text: str,
    target_tokens: int,
    model: str,
    instructions: str,
    timeout_s: float,
    runtime: SummaryUpstreamRuntime,
) -> SummaryProviderAttemptResult:
    try:
        body = runtime.response_body(
            input_text,
            target_tokens=target_tokens,
            model=model,
            instructions=instructions,
        )
        kwargs = runtime.provider_kwargs(provider, timeout_s)
        with pool.text_attempt(provider) as provider_attempt:
            try:
                data = await runtime.responses_call(body, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                provider_attempt.report_failure()
                return SummaryProviderAttemptResult(
                    error=exc,
                    provider_failed=True,
                )
            provider_attempt.report_success()

        try:
            text, usage = runtime.parse_response(data)
            text = text.strip()
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning(
                "context_summary.local_parse_failed provider=%s err=%.300s",
                getattr(provider, "name", "<unknown>"),
                str(exc),
            )
            return SummaryProviderAttemptResult(error=exc)
        if text:
            return SummaryProviderAttemptResult(text=text, usage=usage)

        error = runtime.empty_output_error()
        runtime.logger.warning(
            "context_summary.local_parse_empty provider=%s",
            getattr(provider, "name", "<unknown>"),
        )
        return SummaryProviderAttemptResult(error=error)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning(
            "context_summary.local_attempt_failed provider=%s err=%.300s",
            getattr(provider, "name", "<unknown>"),
            str(exc),
        )
        return SummaryProviderAttemptResult(error=exc)


def _provider_failure_retriable(
    *,
    provider: Any,
    attempt: int,
    attempt_started: float,
    error: Exception,
    runtime: SummaryUpstreamRuntime,
) -> bool:
    decision = runtime.classify_retriable(
        getattr(error, "error_code", None),
        getattr(error, "status_code", None),
        error_message=str(error),
    )
    runtime.logger.warning(
        "context_summary.provider_attempt_failed provider=%s attempt=%d/%d elapsed=%.2fs retriable=%s code=%s status=%s err=%.300s",
        getattr(provider, "name", "<unknown>"),
        attempt + 1,
        runtime.retry_attempts,
        time.monotonic() - attempt_started,
        decision.retriable,
        getattr(error, "error_code", None),
        getattr(error, "status_code", None),
        str(error),
    )
    return bool(decision.retriable)


async def call_summary_upstream(
    input_text: str,
    target_tokens: int,
    model: str,
    *,
    instructions: str,
    extra_instruction: str | None,
    timeout_s: float,
    runtime: SummaryUpstreamRuntime,
) -> str | None:
    try:
        pool = await runtime.get_pool()
        providers = await pool.select(route="text")
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("context_summary.provider_pool_failed err=%s", exc)
        return None

    effective_instructions = _effective_instructions(
        instructions,
        extra_instruction,
    )
    return await _try_summary_providers(
        providers,
        pool=pool,
        input_text=input_text,
        target_tokens=target_tokens,
        model=model,
        instructions=effective_instructions,
        timeout_s=timeout_s,
        runtime=runtime,
    )


def _effective_instructions(
    instructions: str,
    extra_instruction: str | None,
) -> str:
    if not extra_instruction or not extra_instruction.strip():
        return instructions
    return (
        f"{instructions}\n\n### Additional Hints From User\n{extra_instruction.strip()}"
    )


async def _try_summary_providers(
    providers: Sequence[Any],
    *,
    pool: Any,
    input_text: str,
    target_tokens: int,
    model: str,
    instructions: str,
    timeout_s: float,
    runtime: SummaryUpstreamRuntime,
) -> str | None:
    last_exc: BaseException | None = None
    started = time.monotonic()
    for provider in providers:
        result, last_exc = await _try_provider(
            provider,
            pool=pool,
            input_text=input_text,
            target_tokens=target_tokens,
            model=model,
            instructions=instructions,
            timeout_s=timeout_s,
            runtime=runtime,
        )
        if result is not None:
            _warn_slow_upstream(
                provider,
                started=started,
                usage=result.usage,
                logger=runtime.logger,
            )
            return result.text

    runtime.logger.warning(
        "context_summary.all_providers_failed providers=%s last_code=%s last_status=%s last=%.300s",
        ",".join(getattr(p, "name", "<unknown>") for p in providers) or "<none>",
        getattr(last_exc, "error_code", None),
        getattr(last_exc, "status_code", None),
        str(last_exc) if last_exc else "",
    )
    return None


async def _try_provider(
    provider: Any,
    *,
    pool: Any,
    input_text: str,
    target_tokens: int,
    model: str,
    instructions: str,
    timeout_s: float,
    runtime: SummaryUpstreamRuntime,
) -> tuple[SummaryProviderAttemptResult | None, BaseException | None]:
    last_exc: BaseException | None = None
    for attempt in range(runtime.retry_attempts):
        attempt_started = time.monotonic()
        result = await _run_provider_attempt(
            pool=pool,
            provider=provider,
            input_text=input_text,
            target_tokens=target_tokens,
            model=model,
            instructions=instructions,
            timeout_s=timeout_s,
            runtime=runtime,
        )
        if result.text is not None:
            return result, last_exc
        last_exc = result.error
        if not _should_retry_provider(
            provider,
            attempt=attempt,
            attempt_started=attempt_started,
            result=result,
            runtime=runtime,
        ):
            break
        await asyncio.sleep(runtime.retry_backoff_s * (2**attempt))
    return None, last_exc


def _should_retry_provider(
    provider: Any,
    *,
    attempt: int,
    attempt_started: float,
    result: SummaryProviderAttemptResult,
    runtime: SummaryUpstreamRuntime,
) -> bool:
    if not result.provider_failed or result.error is None:
        return False
    if not _provider_failure_retriable(
        provider=provider,
        attempt=attempt,
        attempt_started=attempt_started,
        error=result.error,
        runtime=runtime,
    ):
        return False
    return attempt + 1 < runtime.retry_attempts


def _warn_slow_upstream(
    provider: Any,
    *,
    started: float,
    usage: dict[str, Any] | None,
    logger: logging.Logger,
) -> None:
    elapsed = time.monotonic() - started
    if elapsed <= 8.0:
        return
    logger.warning(
        "context_summary.slow_upstream provider=%s elapsed=%.2fs usage=%s",
        provider.name,
        elapsed,
        usage or {},
    )
