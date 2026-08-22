from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from ..provider_runtime.upstream_services import ImageUpstreamRuntime
from ..agent_runtime_client import AgentRuntimeClient
from ..observability import MetricsServerRuntime
from ..runtime_settings import RuntimeSettingsCache
from ..storage_writes import StorageWriteCoordinator
from ..tasks.completion_parts.runtime import CompletionRuntime
from ..tasks.generation_parts.runtime import GenerationRuntime
from ..tasks.video_generation_parts.runtime import VideoGenerationRuntime
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


class WorkerRuntimeValues(TypedDict):
    runtime_settings_cache: RuntimeSettingsCache
    image_upstream_runtime: ImageUpstreamRuntime
    storage_write_coordinator: StorageWriteCoordinator
    generation_runtime: GenerationRuntime
    completion_runtime: CompletionRuntime
    video_generation_runtime: VideoGenerationRuntime
    metrics_server_runtime: MetricsServerRuntime
    agent_runtime_client: AgentRuntimeClient


@dataclass(frozen=True, slots=True)
class WorkerRuntimeDiagnostics:
    lifecycle: LifecycleDiagnostics
    capabilities: tuple[RuntimeCapability, ...]


@dataclass(slots=True)
class WorkerRuntime:
    """Typed owner for all task runtimes installed into an arq worker context."""

    _runtime_settings: RuntimeSettingsCache
    _image_upstream: ImageUpstreamRuntime
    _storage_writes: StorageWriteCoordinator
    _generation: GenerationRuntime
    _completion: CompletionRuntime
    _video: VideoGenerationRuntime
    _metrics_server: MetricsServerRuntime
    _agent_runtime: AgentRuntimeClient
    _lifecycle: RuntimeLifecycle

    def runtime_settings(self) -> RuntimeSettingsCache:
        return self._runtime_settings

    def image_upstream(self) -> ImageUpstreamRuntime:
        return self._image_upstream

    def storage_writes(self) -> StorageWriteCoordinator:
        return self._storage_writes

    def generation(self) -> GenerationRuntime:
        return self._generation

    def completion(self) -> CompletionRuntime:
        return self._completion

    def video(self) -> VideoGenerationRuntime:
        return self._video

    def metrics_server(self) -> MetricsServerRuntime:
        return self._metrics_server

    def agent_runtime(self) -> AgentRuntimeClient:
        return self._agent_runtime

    def context_values(self) -> WorkerRuntimeValues:
        return WorkerRuntimeValues(
            runtime_settings_cache=self._runtime_settings,
            image_upstream_runtime=self._image_upstream,
            storage_write_coordinator=self._storage_writes,
            generation_runtime=self._generation,
            completion_runtime=self._completion,
            video_generation_runtime=self._video,
            metrics_server_runtime=self._metrics_server,
            agent_runtime_client=self._agent_runtime,
        )

    def start(self, *, logger: logging.Logger | None = None) -> None:
        self._lifecycle.start()
        self.log_capabilities(logger or logging.getLogger(__name__))

    async def close(self) -> None:
        await self._lifecycle.close()

    def capability_matrix(self) -> tuple[RuntimeCapability, ...]:
        capabilities = (
            RuntimeCapability("runtime_settings", CapabilityStatus.ENABLED),
            RuntimeCapability("generation", CapabilityStatus.ENABLED),
            RuntimeCapability("completion", CapabilityStatus.ENABLED),
            RuntimeCapability("video", CapabilityStatus.ENABLED),
            RuntimeCapability("provider", CapabilityStatus.ENABLED),
            RuntimeCapability("http_transport", CapabilityStatus.ENABLED),
            RuntimeCapability("storage_writes", CapabilityStatus.ENABLED),
            RuntimeCapability("metrics_server", CapabilityStatus.ENABLED),
            RuntimeCapability(
                "agent_runtime",
                CapabilityStatus.ENABLED
                if self._agent_runtime.configured
                else CapabilityStatus.DISABLED,
                "configured" if self._agent_runtime.configured else "closed_by_default",
            ),
            RuntimeCapability(
                "postprocess_executor",
                CapabilityStatus.ENABLED
                if self._generation.postprocess_runtime.executor is not None
                else CapabilityStatus.DISABLED,
                "lazy",
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

    def diagnostics(self) -> WorkerRuntimeDiagnostics:
        return WorkerRuntimeDiagnostics(
            lifecycle=self._lifecycle.diagnostics(),
            capabilities=self.capability_matrix(),
        )

    def log_capabilities(self, logger: logging.Logger) -> None:
        matrix = " ".join(
            f"{capability.name}={capability.status.value}"
            for capability in self.capability_matrix()
        )
        logger.info("worker.runtime capabilities %s", matrix)
