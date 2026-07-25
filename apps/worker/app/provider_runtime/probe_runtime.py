"""Explicit runtime dependencies for provider health probes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import httpx
from lumen_core.byok import (
    build_provider_probe_request,
    extract_response_output_text,
    extract_sse_output_text,
)
from lumen_core.providers import endpoint_kind_allowed, resolve_provider_proxy_url


def ewma(previous: float | None, sample: float, alpha: float) -> float:
    if previous is None:
        return float(sample)
    return (alpha * float(sample)) + ((1.0 - alpha) * previous)


@dataclass(frozen=True)
class ProviderProbeRuntime:
    monotonic: Callable[[], float]
    wall_time: Callable[[], float]
    logger: logging.Logger
    async_client_factory: Callable[..., Any]
    timeout_factory: Callable[..., Any]
    build_probe_request: Callable[[], dict[str, Any]]
    extract_response_text: Callable[[Any], str]
    extract_sse_text: Callable[[str], str]
    endpoint_allowed: Callable[..., bool]
    resolve_proxy_url: Callable[..., Any]
    ewma: Callable[[float | None, float, float], float]
    circuit_failure_threshold: int = 3
    circuit_cooldown_base_s: float = 30.0
    circuit_cooldown_max_s: float = 300.0
    probe_timeout_s: float = 15.0
    probe_max_concurrency: int = 8
    image_circuit_failure_threshold: int = 3
    image_circuit_cooldown_s: float = 10.0
    image_rate_limited_default_s: float = 60.0
    endpoint_ewma_alpha: float = 0.25
    endpoint_failure_alpha: float = 0.35
    endpoint_recent_failure_window_s: float = 60.0
    image_routing_failure_penalty_ms: float = 5000.0
    image_routing_consecutive_failure_penalty_ms: float = 1000.0
    image_probe_prompt: str = "a small red apple on a white background"
    image_probe_size: str = "1024x1024"
    image_probe_quality: str = "low"
    image_probe_min_b64_len: int = 1000


DEFAULT_PROVIDER_PROBE_RUNTIME = ProviderProbeRuntime(
    monotonic=time.monotonic,
    wall_time=time.time,
    logger=logging.getLogger("app.provider_pool"),
    async_client_factory=httpx.AsyncClient,
    timeout_factory=httpx.Timeout,
    build_probe_request=build_provider_probe_request,
    extract_response_text=extract_response_output_text,
    extract_sse_text=extract_sse_output_text,
    endpoint_allowed=endpoint_kind_allowed,
    resolve_proxy_url=resolve_provider_proxy_url,
    ewma=ewma,
)

_PROBE_RUNTIME_OVERRIDE: ContextVar[ProviderProbeRuntime | None] = ContextVar(
    "provider_probe_runtime_override",
    default=None,
)


def provider_probe_runtime() -> ProviderProbeRuntime:
    return _PROBE_RUNTIME_OVERRIDE.get() or DEFAULT_PROVIDER_PROBE_RUNTIME


def bind_provider_probe_runtime(
    runtime: ProviderProbeRuntime,
) -> Token[ProviderProbeRuntime | None]:
    return _PROBE_RUNTIME_OVERRIDE.set(runtime)


def reset_provider_probe_runtime(
    token: Token[ProviderProbeRuntime | None],
) -> None:
    _PROBE_RUNTIME_OVERRIDE.reset(token)


@contextmanager
def use_provider_probe_runtime(
    runtime: ProviderProbeRuntime,
) -> Iterator[ProviderProbeRuntime]:
    token = bind_provider_probe_runtime(runtime)
    try:
        yield runtime
    finally:
        reset_provider_probe_runtime(token)


__all__ = [
    "DEFAULT_PROVIDER_PROBE_RUNTIME",
    "ProviderProbeRuntime",
    "bind_provider_probe_runtime",
    "ewma",
    "provider_probe_runtime",
    "reset_provider_probe_runtime",
    "use_provider_probe_runtime",
]
