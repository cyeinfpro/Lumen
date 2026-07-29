"""Provider adapter composition used by the context-summary facade."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from . import upstream


def build_runtime(
    *,
    get_pool: Callable[[], Awaitable[Any]],
    classify_retriable: Callable[..., Any],
    responses_call: Callable[..., Awaitable[Any]],
    response_body: Callable[..., dict[str, Any]],
    parse_response: Callable[..., tuple[str, dict[str, Any]]],
    provider_kwargs: Callable[..., dict[str, Any]],
    empty_output_error: Callable[[], Exception],
    logger: logging.Logger,
    retry_attempts: int,
    retry_backoff_s: float,
) -> upstream.SummaryUpstreamRuntime:
    return upstream.SummaryUpstreamRuntime(
        get_pool=get_pool,
        classify_retriable=classify_retriable,
        responses_call=responses_call,
        response_body=response_body,
        parse_response=parse_response,
        provider_kwargs=provider_kwargs,
        empty_output_error=empty_output_error,
        logger=logger,
        retry_attempts=retry_attempts,
        retry_backoff_s=retry_backoff_s,
    )


async def call_summary_upstream(
    input_text: str,
    target_tokens: int,
    model: str,
    *,
    instructions: str,
    extra_instruction: str | None,
    timeout_s: float,
    image_upstream_runtime: Any,
    runtime_factory: Callable[..., upstream.SummaryUpstreamRuntime],
) -> str | None:
    from ...provider_pool import get_pool
    from ...retry import is_retriable as classify_retriable
    from ...upstream_parts.entrypoints import responses_call

    return await upstream.call_summary_upstream(
        input_text,
        target_tokens,
        model,
        instructions=instructions,
        extra_instruction=extra_instruction,
        timeout_s=timeout_s,
        runtime=runtime_factory(
            get_pool=get_pool,
            classify_retriable=classify_retriable,
            responses_call=partial(
                responses_call,
                runtime=image_upstream_runtime,
            ),
        ),
    )


async def run_provider_attempt(
    *,
    pool: Any,
    provider: Any,
    input_text: str,
    target_tokens: int,
    model: str,
    instructions: str,
    timeout_s: float,
    responses_call: Callable[..., Awaitable[Any]],
    runtime_factory: Callable[..., upstream.SummaryUpstreamRuntime],
) -> upstream.SummaryProviderAttemptResult:
    from ...provider_pool import get_pool
    from ...retry import is_retriable as classify_retriable

    return await upstream._run_provider_attempt(
        pool=pool,
        provider=provider,
        input_text=input_text,
        target_tokens=target_tokens,
        model=model,
        instructions=instructions,
        timeout_s=timeout_s,
        runtime=runtime_factory(
            get_pool=get_pool,
            classify_retriable=classify_retriable,
            responses_call=responses_call,
        ),
    )
