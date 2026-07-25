"""Runtime composition and lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .adapters.filesystem_artifacts import FilesystemArtifactStore
from .adapters.http_upstream import HttpUpstreamGateway
from .adapters.sqlite_jobs import SQLiteJobRepository
from .application.job_service import JobService
from .application.queue_supervisor import QueueSupervisor
from .application.reference_service import ReferenceService
from .config import ImageJobSettings


LOG = logging.getLogger("image-job.runtime")


@dataclass
class ImageJobRuntime:
    settings: ImageJobSettings
    jobs: JobService
    references: ReferenceService
    queue: QueueSupervisor
    repository: SQLiteJobRepository
    upstream: HttpUpstreamGateway
    artifacts: FilesystemArtifactStore
    legacy_auth_requests_total: int = 0
    started: bool = False

    async def startup(self) -> None:
        if self.started:
            return
        self.settings.validate()
        if self.settings.allow_legacy_bearer:
            LOG.warning(
                "legacy Bearer authentication is enabled; remove it before v%s",
                self.settings.legacy_auth_removal_version,
            )
        await self.repository.initialize()
        await self.jobs.fail_interrupted()
        await self.upstream.startup()
        await self.jobs.reconcile()
        await self.queue.startup()
        self.started = True

    async def shutdown(self) -> None:
        if not self.started:
            return
        await self.queue.shutdown()
        await self.upstream.shutdown()
        self.started = False

    async def readiness(self) -> tuple[bool, tuple[str, ...]]:
        failures: list[str] = []
        state = await self.queue.snapshot()
        if not self.started:
            failures.append("runtime_not_started")
        if not state.accepting:
            failures.append("queue_not_accepting")
        if not await self.repository.readiness_probe():
            failures.append("repository_unavailable")
        if not await self.artifacts.readiness_probe():
            failures.append("artifacts_unavailable")
        return not failures, tuple(failures)

    async def metrics_text(self) -> str:
        state = await self.queue.snapshot()
        values: dict[str, Any] = {
            "image_job_queue_size": state.queue_size,
            "image_job_queue_capacity": state.queue_max,
            "image_job_queued_known": state.queued_known,
            "image_job_inflight": state.inflight,
            "image_job_workers_alive": state.workers_alive,
            "image_job_workers_expected": state.workers_expected,
            "image_job_background_tasks_alive": state.background_alive,
            "image_job_accepting": int(state.accepting),
            "image_job_shutdown": int(state.shutdown),
            "image_job_legacy_auth_requests_total": (self.legacy_auth_requests_total),
            **{f"image_job_{key}": value for key, value in self.queue.metrics.items()},
        }
        return "".join(f"{name} {value}\n" for name, value in values.items())


def create_runtime(
    settings: ImageJobSettings | None = None,
) -> ImageJobRuntime:
    resolved = settings or ImageJobSettings.from_env()
    repository = SQLiteJobRepository(resolved)
    artifacts = FilesystemArtifactStore(resolved, repository)
    upstream = HttpUpstreamGateway(resolved)
    queue = QueueSupervisor(
        queue_max=resolved.queue_max,
        concurrency=resolved.concurrency,
        graceful_shutdown_s=resolved.timeouts.graceful_shutdown_s,
        reconcile_interval_s=resolved.stuck_reconcile_interval_s,
        retention_interval_s=resolved.retention_sweep_interval_s,
    )
    jobs = JobService(resolved, repository, upstream, queue)
    upstream.processing.touch_running = jobs.persistence.touch_running
    references = ReferenceService(artifacts, resolved.max_ref_bytes)
    queue.bind(
        processor=jobs.process,
        reconcile=jobs.reconcile,
    )
    return ImageJobRuntime(
        settings=resolved,
        jobs=jobs,
        references=references,
        queue=queue,
        repository=repository,
        upstream=upstream,
        artifacts=artifacts,
    )
