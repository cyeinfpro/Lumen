"""Runtime composition and lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .adapters.filesystem_artifacts import FilesystemArtifactStore
from .adapters.http_upstream import HttpUpstreamGateway
from .adapters.sqlite_jobs import SQLiteJobHeartbeat, SQLiteJobRepository
from .application.job_service import JobService
from .application.queue_supervisor import QueueSupervisor
from .application.reference_service import ReferenceService
from .config import ImageJobSettings
from .credential_vault import CredentialVault


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
    credential_vault: CredentialVault
    legacy_auth_requests_total: int = 0
    started: bool = False

    async def startup(self) -> None:
        if self.started:
            return
        try:
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
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        # H-16：不能因为 started=False 就提前返回。startup() 是分步的，
        # upstream.startup() 成功之后 queue.startup() 再抛异常（或者 lifespan
        # 在 startup 中途被取消），started 仍是 False，但 httpx.AsyncClient
        # 已经建好了——提前返回就把连接池永久泄漏在进程里。queue.shutdown()
        # 与 upstream.shutdown() 本身都是幂等的，重复调用是安全的。
        try:
            await self.queue.shutdown()
        finally:
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
            "image_job_queue_reserved": state.reserved,
            "image_job_queued_known": state.queued_known,
            "image_job_inflight": state.inflight,
            "image_job_workers_alive": state.workers_alive,
            "image_job_workers_expected": state.workers_expected,
            "image_job_background_tasks_alive": state.background_alive,
            "image_job_accepting": int(state.accepting),
            "image_job_shutdown": int(state.shutdown),
            "image_job_legacy_auth_requests_total": (self.legacy_auth_requests_total),
            **{f"image_job_{key}": value for key, value in self.queue.metrics.items()},
            # H-19：业务维度指标。jobs_uncertain_total 是纯转嫁下的资金风险量表
            # ——每 +1 代表一笔「上游可能已扣费但没交付」的待对账工单。
            **{f"image_job_{key}": value for key, value in self.jobs.outcomes.items()},
        }
        return "".join(f"{name} {value}\n" for name, value in values.items())


def create_runtime(
    settings: ImageJobSettings | None = None,
) -> ImageJobRuntime:
    resolved = settings or ImageJobSettings.from_env()
    credential_vault = CredentialVault(
        active_key_id=resolved.credential_active_key_id,
        master_secret=resolved.credential_master_secret.get_secret_value(),
    )
    repository = SQLiteJobRepository(resolved, credential_vault)
    artifacts = FilesystemArtifactStore(resolved, repository)
    upstream = HttpUpstreamGateway(
        resolved,
        heartbeat=SQLiteJobHeartbeat(repository),
    )
    queue = QueueSupervisor(
        queue_max=resolved.queue_max,
        concurrency=resolved.concurrency,
        graceful_shutdown_s=resolved.timeouts.graceful_shutdown_s,
        reconcile_interval_s=resolved.stuck_reconcile_interval_s,
        retention_interval_s=resolved.retention_sweep_interval_s,
    )
    jobs = JobService(
        resolved,
        repository,
        upstream,
        queue,
        credential_vault,
    )
    references = ReferenceService(artifacts, resolved.max_ref_bytes)
    queue.bind(
        processor=jobs.process,
        reconcile=jobs.reconcile,
        # H-17：不传 retention 的话 QueueSupervisor.startup() 会直接跳过保留期
        # 清扫协程，磁盘和 jobs 表就永远只增不减。
        retention=jobs.retention.run_pass,
    )
    return ImageJobRuntime(
        settings=resolved,
        jobs=jobs,
        references=references,
        queue=queue,
        repository=repository,
        upstream=upstream,
        artifacts=artifacts,
        credential_vault=credential_vault,
    )
