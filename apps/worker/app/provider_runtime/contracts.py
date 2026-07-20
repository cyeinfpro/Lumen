"""Provider runtime data contracts.

This module intentionally has no dependency on the upstream HTTP facade,
database runtime, or task layer.  Both provider selection and BYOK resolution
can depend on these contracts without forming an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lumen_core.providers import DEFAULT_PROVIDER_PURPOSES, ProviderProxyDefinition


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
