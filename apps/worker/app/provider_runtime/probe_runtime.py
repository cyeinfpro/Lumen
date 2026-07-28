"""Explicit runtime dependencies for provider health probes."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from lumen_core.byok import (
    build_provider_probe_request,
    extract_response_output_text,
    extract_sse_output_text,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
)

from .contracts import ProviderConfig


class ProbeResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> object: ...


class ProbeHttpClient(Protocol):
    async def __aenter__(self) -> ProbeHttpClient: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None: ...

    async def post(self, url: str, **kwargs: object) -> ProbeResponse: ...


class ProbeHttpClientFactory(Protocol):
    def __call__(self, **kwargs: object) -> ProbeHttpClient: ...


class ProbeTimeoutFactory(Protocol):
    def __call__(self, timeout: float) -> object: ...


def ewma(previous: float | None, sample: float, alpha: float) -> float:
    if previous is None:
        return float(sample)
    return (alpha * float(sample)) + ((1.0 - alpha) * previous)


@dataclass(frozen=True)
class ProviderProbeRuntime:
    monotonic: Callable[[], float]
    wall_time: Callable[[], float]
    logger: logging.Logger
    async_client_factory: ProbeHttpClientFactory
    timeout_factory: ProbeTimeoutFactory
    build_probe_request: Callable[[], dict[str, Any]]
    extract_response_text: Callable[[object], str]
    extract_sse_text: Callable[[str], str]
    endpoint_allowed: Callable[[ProviderConfig, str | None], bool]
    resolve_proxy_url: Callable[
        [ProviderProxyDefinition],
        Awaitable[str | None],
    ]
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


def build_provider_probe_runtime() -> ProviderProbeRuntime:
    """Build probe dependencies for one provider-pool instance."""
    return ProviderProbeRuntime(
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


__all__ = [
    "ProbeHttpClient",
    "ProbeHttpClientFactory",
    "ProbeResponse",
    "ProbeTimeoutFactory",
    "ProviderProbeRuntime",
    "build_provider_probe_runtime",
    "ewma",
]
