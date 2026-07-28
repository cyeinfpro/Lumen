"""Per-runtime queue and task supervision."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..domain.queue_state import QueueState


LOG = logging.getLogger("image-job.queue")


class QueueSupervisor:
    def __init__(
        self,
        *,
        queue_max: int,
        concurrency: int,
        graceful_shutdown_s: float,
        reconcile_interval_s: float,
        retention_interval_s: float,
    ) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_max)
        self.concurrency = concurrency
        self.graceful_shutdown_s = graceful_shutdown_s
        self.reconcile_interval_s = reconcile_interval_s
        self.retention_interval_s = retention_interval_s
        self.queued_ids: set[str] = set()
        self.inflight: set[str] = set()
        self._reservations: dict[str, object] = {}
        self.lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self.workers: dict[int, asyncio.Task[None]] = {}
        self.background: dict[str, asyncio.Task[None]] = {}
        self.processor: Callable[[str], Awaitable[None]] | None = None
        self.reconcile_callback: Callable[[], Awaitable[None]] | None = None
        self.retention_callback: Callable[[], Awaitable[None]] | None = None
        self.started = False
        self.last_worker_heartbeat: float | None = None
        self.last_reconcile: float | None = None
        self.metrics = {
            "worker_restarts_total": 0,
            "worker_failures_total": 0,
            "jobs_started_total": 0,
            "jobs_completed_total": 0,
            "attempts_finished_total": 0,
            "processor_success_total": 0,
            "processor_crash_total": 0,
        }

    def bind(
        self,
        *,
        processor: Callable[[str], Awaitable[None]],
        reconcile: Callable[[], Awaitable[None]],
        retention: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.processor = processor
        self.reconcile_callback = reconcile
        self.retention_callback = retention

    async def enqueue(self, job_id: str) -> str:
        async with self.lock:
            if self.shutdown_event.is_set():
                return "full"
            if job_id in self.queued_ids:
                return "queued"
            if job_id in self.inflight:
                return "inflight"
            if job_id in self._reservations:
                return "queued"
            if not self._has_capacity_locked():
                return "full"
            try:
                self.queue.put_nowait(job_id)
            except asyncio.QueueFull:
                return "full"
            self.queued_ids.add(job_id)
            return "enqueued"

    def _has_capacity_locked(self) -> bool:
        return self.queue.qsize() + len(self._reservations) < self.queue.maxsize

    async def _try_reserve(self, job_id: str) -> tuple[str, object | None]:
        async with self.lock:
            if self.shutdown_event.is_set():
                return "full", None
            if job_id in self.queued_ids:
                return "queued", None
            if job_id in self.inflight:
                return "inflight", None
            if job_id in self._reservations:
                return "queued", None
            if not self._has_capacity_locked():
                return "full", None
            token = object()
            self._reservations[job_id] = token
            return "reserved", token

    async def _release_reservation(self, job_id: str, token: object) -> None:
        async with self.lock:
            if self._reservations.get(job_id) is token:
                self._reservations.pop(job_id, None)

    async def _commit_reservation(self, job_id: str, token: object) -> str:
        async with self.lock:
            if self._reservations.get(job_id) is not token:
                if job_id in self.queued_ids:
                    return "queued"
                if job_id in self.inflight:
                    return "inflight"
                return "persisted"
            self._reservations.pop(job_id, None)
            if self.shutdown_event.is_set():
                return "persisted"
            try:
                self.queue.put_nowait(job_id)
            except asyncio.QueueFull:
                LOG.error(
                    "reserved image-job queue slot disappeared for %s; "
                    "leaving the durable row for reconciliation",
                    job_id,
                )
                return "persisted"
            self.queued_ids.add(job_id)
            return "enqueued"

    async def persist_and_enqueue(
        self,
        job_id: str,
        persist: Callable[[], Awaitable[None]],
    ) -> str:
        status, token = await self._try_reserve(job_id)
        if token is None:
            return status
        try:
            await persist()
        except BaseException:
            await asyncio.shield(self._release_reservation(job_id, token))
            raise
        return await asyncio.shield(self._commit_reservation(job_id, token))

    async def startup(self) -> None:
        if self.started:
            return
        if self.processor is None or self.reconcile_callback is None:
            raise RuntimeError("QueueSupervisor callbacks are not bound")
        self.shutdown_event.clear()
        self.started = True
        for worker_id in range(1, self.concurrency + 1):
            self._start_worker(worker_id)
        self.background["reconcile"] = asyncio.create_task(
            self._periodic(
                "reconcile",
                self.reconcile_interval_s,
                self._run_reconcile,
            ),
            name="image-job-reconciler",
        )
        if self.retention_callback is not None:
            self.background["retention"] = asyncio.create_task(
                self._periodic(
                    "retention",
                    self.retention_interval_s,
                    self.retention_callback,
                ),
                name="image-job-retention",
            )

    def _start_worker(self, worker_id: int) -> None:
        task = asyncio.create_task(
            self._worker_loop(worker_id),
            name=f"image-job-worker-{worker_id}",
        )
        self.workers[worker_id] = task
        task.add_done_callback(
            lambda completed, wid=worker_id: self._worker_done(wid, completed)
        )

    def _worker_done(
        self,
        worker_id: int,
        task: asyncio.Task[None],
    ) -> None:
        if self.shutdown_event.is_set():
            return
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.metrics["worker_failures_total"] += 1
            LOG.error(
                "image-job worker %d crashed; replacing it",
                worker_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        self.metrics["worker_restarts_total"] += 1
        self._start_worker(worker_id)

    async def _worker_loop(self, worker_id: int) -> None:
        assert self.processor is not None
        while not self.shutdown_event.is_set():
            self.last_worker_heartbeat = time.monotonic()
            try:
                job_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            async with self.lock:
                self.queued_ids.discard(job_id)
                self.inflight.add(job_id)
            self.metrics["jobs_started_total"] += 1
            try:
                await self.processor(job_id)
            except asyncio.CancelledError:
                raise
            except BaseException:
                self.metrics["processor_crash_total"] += 1
                raise
            else:
                self.metrics["processor_success_total"] += 1
                self.metrics["jobs_completed_total"] += 1
            finally:
                async with self.lock:
                    self.inflight.discard(job_id)
                self.queue.task_done()
                self.metrics["attempts_finished_total"] += 1

    async def _run_reconcile(self) -> None:
        assert self.reconcile_callback is not None
        await self.reconcile_callback()
        self.last_reconcile = time.monotonic()

    async def _periodic(
        self,
        name: str,
        interval: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        while not self.shutdown_event.is_set():
            try:
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("%s background iteration failed", name)
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=interval,
                )
            except TimeoutError:
                pass

    async def shutdown(self) -> None:
        if not self.started:
            return
        self.shutdown_event.set()
        deadline = time.monotonic() + self.graceful_shutdown_s
        while time.monotonic() < deadline:
            async with self.lock:
                if not self.inflight:
                    break
            await asyncio.sleep(0.05)
        tasks = [*self.workers.values(), *self.background.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.background.clear()
        self.started = False

    async def snapshot(self) -> QueueState:
        async with self.lock:
            inflight = len(self.inflight)
            queued_known = len(self.queued_ids)
            reserved = len(self._reservations)
            has_capacity = self._has_capacity_locked()
        workers_alive = sum(not task.done() for task in self.workers.values())
        background_alive = sum(not task.done() for task in self.background.values())
        shutdown = self.shutdown_event.is_set()
        return QueueState(
            accepting=(
                self.started
                and not shutdown
                and has_capacity
                and workers_alive == self.concurrency
            ),
            shutdown=shutdown,
            queue_size=self.queue.qsize(),
            queue_max=self.queue.maxsize,
            reserved=reserved,
            queued_known=queued_known,
            inflight=inflight,
            workers_alive=workers_alive,
            workers_expected=self.concurrency,
            background_alive=background_alive,
            last_worker_heartbeat=self.last_worker_heartbeat,
            last_reconcile=self.last_reconcile,
        )
