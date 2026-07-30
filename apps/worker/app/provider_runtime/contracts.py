"""Provider runtime data contracts.

This module intentionally has no dependency on the upstream HTTP facade,
database runtime, or task layer.  Both provider selection and BYOK resolution
can depend on these contracts without forming an import cycle.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from lumen_core.providers import ProviderProxyDefinition
from lumen_core.providers_parts.definitions import DEFAULT_PROVIDER_PURPOSES


@dataclass(frozen=True)
class UpstreamTimeouts:
    connect_s: float
    read_s: float
    write_s: float
    pool_s: float


@dataclass(frozen=True)
class ImageJobEndpoint:
    base_url: str
    service_token: str = field(repr=False, compare=False)
    allow_private_http: bool = True


@dataclass(frozen=True)
class ImageProgressEvent:
    stage: str
    details: dict[str, Any] = field(default_factory=dict)


class RuntimeSettingsPort(Protocol):
    async def get_text(
        self,
        key: str,
        default: str | None = None,
    ) -> str | None: ...

    async def get_int(self, key: str, default: int) -> int: ...


class ProviderSelectorPort(Protocol):
    async def candidates(
        self,
        *,
        route: str,
        endpoint_kind: str | None,
        ignore_cooldown: bool,
    ) -> Sequence["ProviderConfig"]: ...


class ProgressPort(Protocol):
    async def emit(self, event: ImageProgressEvent) -> None: ...


@dataclass(frozen=True)
class ImageProbeRequest:
    prompt: str
    size: str
    quality: str
    provider: "ProviderConfig"


class ImageProbePort(Protocol):
    async def __call__(
        self,
        request: ImageProbeRequest,
    ) -> tuple[str, str | None]: ...


class ClockPort(Protocol):
    def monotonic(self) -> float: ...

    def utcnow(self) -> datetime: ...


class SupplierTransportPort(Protocol):
    async def close(self) -> None: ...


class ImageJobClientPort(Protocol):
    async def submit(
        self,
        payload: dict[str, Any],
        *,
        upstream_api_key: str,
        trace_id: str,
        before_attempt: Callable[[int], Awaitable[None]] | None = None,
    ) -> Any: ...

    async def poll(
        self,
        handle: Any,
        *,
        trace_id: str,
    ) -> Any: ...

    async def upload_reference(
        self,
        raw: bytes,
        mime: str,
        *,
        trace_id: str,
    ) -> Any: ...

    async def cancel(self, handle: Any, *, trace_id: str) -> None: ...

    async def close(self) -> None: ...


class ResultDownloadPort(Protocol):
    async def download(
        self,
        url: str,
        *,
        allowed_base_url: str | None = None,
    ) -> bytes: ...


class UpstreamMetricsPort(Protocol):
    def record_request(
        self,
        *,
        endpoint: str,
        status_code: int,
        duration_s: float,
    ) -> None: ...


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = field(repr=False, compare=False)
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_PURPOSES
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    image_rate_limit: str | None = None
    image_daily_quota: int | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: str = "url"
    image_concurrency: int = 1
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


@dataclass
class EndpointStat:
    """Per-provider, per-image-endpoint health and latency state."""

    last_success_at: float | None = None
    last_failure_at: float | None = None
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    success_count: int = 0
    success_mean_ms: float = 0.0
    latency_ewma_ms: float | None = None
    failure_ewma: float = 0.0


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    last_probe_at: float | None = None
    cooldown_until: float | None = None
    half_open_probe_inflight: bool = False
    half_open_probe_token: str | None = None
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    image_consecutive_failures: int = 0
    image_cooldown_until: float | None = None
    image_last_used_at: float | None = None
    image_last_attempted_at: float | None = None
    image_rate_limited_until: float | None = None
    endpoint_stats: dict[str, EndpointStat] = field(default_factory=dict)
    image_inflight: dict[str, int] = field(default_factory=dict)
    image_last_used_at_per_ek: dict[str, float] = field(default_factory=dict)
    image_last_attempted_at_per_ek: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedProvider:
    name: str
    base_url: str
    api_key: str = field(repr=False, compare=False)
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: str = "url"
    image_concurrency: int = 1
    image_rate_limit: str | None = None
    image_daily_quota: int | None = None
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_PURPOSES
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None
    text_circuit_state: str = field(default="closed", repr=False, compare=False)
    half_open_probe_token: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
