from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from arq.connections import ArqRedis

from ..redis_client import ReconnectingRedis
from ..services.billing_cache import BillingCacheService
from ..services.admin_model_cache import AdminModelCache
from ..services.poster_styles.tagging_runtime import PosterTaggingRuntime
from .lifecycle import LifecycleDiagnostics, RuntimeLifecycle


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
