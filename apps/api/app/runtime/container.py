from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
from arq.connections import ArqRedis
from lumen_core.billing_cache import BillingCacheService
from lumen_core.volcano_asset_media import VideoTranscodeRuntime

from ..services.admin_model_cache import AdminModelCache
from ..services.poster_styles.tagging_runtime import PosterTaggingRuntime
from .lifecycle import LifecycleDiagnostics, RuntimeLifecycle

if TYPE_CHECKING:
    from ..arq_pool import ArqPoolState
    from ..billing_cache_state import BillingCacheState
    from ..byok_service import ByokSettingsCache
    from ..proxy_pool import ProxyPoolState
    from ..redis_client import ReconnectingRedis, RedisClientState
    from ..services.conversations.compaction import ContextSummaryAdapterRegistry
    from ..services.upload_pipeline import UploadBudget


class CapabilityStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    name: str
    status: CapabilityStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApiRuntimeDiagnostics:
    lifecycle: LifecycleDiagnostics
    capabilities: tuple[RuntimeCapability, ...]


@dataclass(slots=True)
class ApiProcessState:
    """Lifecycle-bound process state containers owned by FastAPI lifespan."""

    redis_state: RedisClientState
    billing_cache_state: BillingCacheState
    arq_pool_state: ArqPoolState
    byok_settings_cache: ByokSettingsCache
    proxy_pool_state: ProxyPoolState
    context_summary_adapters: ContextSummaryAdapterRegistry
    upload_budget: UploadBudget
    volcano_media: VideoTranscodeRuntime = field(default_factory=VideoTranscodeRuntime)


def new_api_process_state() -> ApiProcessState:
    """Create fresh lifecycle-bound process state containers without import cycles."""
    from ..arq_pool import ArqPoolState
    from ..billing_cache_state import BillingCacheState
    from ..byok_service import ByokSettingsCache
    from ..proxy_pool import ProxyPoolState
    from ..redis_client import RedisClientState
    from ..services.conversations.compaction import ContextSummaryAdapterRegistry
    from ..services.upload_pipeline import UploadBudget, UploadBudgetLimits

    return ApiProcessState(
        redis_state=RedisClientState(),
        billing_cache_state=BillingCacheState(),
        arq_pool_state=ArqPoolState(),
        byok_settings_cache=ByokSettingsCache(),
        proxy_pool_state=ProxyPoolState(),
        context_summary_adapters=ContextSummaryAdapterRegistry(),
        upload_budget=UploadBudget(UploadBudgetLimits.from_env()),
    )


def bind_api_process_state(lifecycle: RuntimeLifecycle) -> ApiProcessState:
    """Install fresh process-scoped state containers and register reverse cleanup."""
    from ..arq_pool import configure_arq_pool_state, reset_arq_pool_state
    from ..billing_cache_state import (
        configure_billing_cache_state,
        reset_billing_cache_state,
    )
    from ..byok_service import configure_byok_settings_cache, reset_byok_settings_cache
    from ..proxy_pool import configure_proxy_pool_state, shutdown_proxy_pool_state
    from ..redis_client import configure_redis_state, reset_redis_state
    from ..services.conversations.compaction import (
        configure_context_summary_adapters,
        reset_context_summary_adapters,
    )
    from ..services.upload_pipeline import (
        configure_process_upload_budget,
        reset_process_upload_budget,
    )

    state = new_api_process_state()
    configure_redis_state(state.redis_state)
    lifecycle.own("redis_state", lambda: reset_redis_state(state.redis_state))
    configure_billing_cache_state(state.billing_cache_state)
    lifecycle.own(
        "billing_cache_state",
        lambda: reset_billing_cache_state(state.billing_cache_state),
    )
    configure_arq_pool_state(state.arq_pool_state)
    lifecycle.own("arq_pool_state", lambda: reset_arq_pool_state(state.arq_pool_state))
    configure_byok_settings_cache(state.byok_settings_cache)
    lifecycle.own(
        "byok_settings_cache",
        lambda: reset_byok_settings_cache(state.byok_settings_cache),
    )
    configure_proxy_pool_state(state.proxy_pool_state)
    lifecycle.own(
        "proxy_pool_state",
        lambda: shutdown_proxy_pool_state(state.proxy_pool_state),
    )
    configure_context_summary_adapters(state.context_summary_adapters)
    lifecycle.own(
        "context_summary_adapters",
        lambda: reset_context_summary_adapters(state.context_summary_adapters),
    )
    configure_process_upload_budget(state.upload_budget)
    lifecycle.own(
        "upload_budget",
        lambda: reset_process_upload_budget(state.upload_budget),
    )
    lifecycle.own("volcano_media_runtime", state.volcano_media.reset)
    return state


@dataclass(slots=True)
class ApiRuntime:
    """Typed owner for API process resources created by FastAPI lifespan."""

    _redis: ReconnectingRedis
    _arq: ArqRedis
    _billing_cache: BillingCacheService
    _lifecycle: RuntimeLifecycle
    _admin_models: AdminModelCache = field(default_factory=AdminModelCache)
    _poster_tagging: PosterTaggingRuntime | None = None
    _http: httpx.AsyncClient | None = None
    _image_reconciler_enabled: bool = False
    _proxy_pool_enabled: bool = False
    _process_state: ApiProcessState = field(default_factory=new_api_process_state)

    def redis(self) -> ReconnectingRedis:
        return self._redis

    def event_redis(self) -> ReconnectingRedis:
        return self._redis

    def task_queue(self) -> ArqRedis:
        return self._arq

    def billing_cache(self) -> BillingCacheService:
        return self._billing_cache

    def admin_models(self) -> AdminModelCache:
        return self._admin_models

    def byok_settings_cache(self) -> ByokSettingsCache:
        return self._process_state.byok_settings_cache

    def proxy_pool_state(self) -> ProxyPoolState:
        return self._process_state.proxy_pool_state

    def context_summary_adapters(self) -> ContextSummaryAdapterRegistry:
        return self._process_state.context_summary_adapters

    def upload_budget(self) -> UploadBudget:
        return self._process_state.upload_budget

    def volcano_media(self) -> VideoTranscodeRuntime:
        return self._process_state.volcano_media

    def poster_tagging(self) -> PosterTaggingRuntime:
        if self._poster_tagging is None:
            raise RuntimeError("poster tagging capability is disabled")
        return self._poster_tagging

    def shared_http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("shared HTTP capability is disabled")
        return self._http

    def start(self, *, logger: logging.Logger | None = None) -> None:
        self._lifecycle.start()
        self.log_capabilities(logger or logging.getLogger(__name__))

    async def close(self) -> None:
        await self._lifecycle.close()

    def capability_matrix(self) -> tuple[RuntimeCapability, ...]:
        capabilities = (
            RuntimeCapability("redis", CapabilityStatus.ENABLED),
            RuntimeCapability("sse", CapabilityStatus.ENABLED, "redis-backed"),
            RuntimeCapability("arq", CapabilityStatus.ENABLED),
            RuntimeCapability("billing_cache", CapabilityStatus.ENABLED),
            RuntimeCapability("admin_model_cache", CapabilityStatus.ENABLED),
            RuntimeCapability(
                "image_reconciler",
                CapabilityStatus.ENABLED
                if self._image_reconciler_enabled
                else CapabilityStatus.DISABLED,
            ),
            RuntimeCapability(
                "poster_tagging",
                CapabilityStatus.ENABLED
                if self._poster_tagging is not None
                else CapabilityStatus.DISABLED,
            ),
            RuntimeCapability(
                "shared_http",
                CapabilityStatus.ENABLED
                if self._http is not None
                else CapabilityStatus.DISABLED,
            ),
            RuntimeCapability(
                "proxy_pool",
                CapabilityStatus.ENABLED
                if self._proxy_pool_enabled
                else CapabilityStatus.DISABLED,
            ),
        )
        if self._lifecycle.diagnostics().cleanup_failures:
            return tuple(
                RuntimeCapability(
                    capability.name,
                    CapabilityStatus.DEGRADED
                    if capability.status is CapabilityStatus.ENABLED
                    else capability.status,
                    capability.detail,
                )
                for capability in capabilities
            )
        return capabilities

    def diagnostics(self) -> ApiRuntimeDiagnostics:
        return ApiRuntimeDiagnostics(
            lifecycle=self._lifecycle.diagnostics(),
            capabilities=self.capability_matrix(),
        )

    def log_capabilities(self, logger: logging.Logger) -> None:
        matrix = " ".join(
            f"{capability.name}={capability.status.value}"
            for capability in self.capability_matrix()
        )
        logger.info("api.runtime capabilities %s", matrix)
