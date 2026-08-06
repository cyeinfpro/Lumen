from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
if str(IMAGE_JOB_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGE_JOB_DIR))

from image_job.application.queue_supervisor import QueueSupervisor  # noqa: E402
from image_job.contracts import JobProcessOutcome  # noqa: E402


def _queue(*, queue_max: int = 1) -> QueueSupervisor:
    return QueueSupervisor(
        queue_max=queue_max,
        concurrency=1,
        graceful_shutdown_s=0,
        reconcile_interval_s=60,
        retention_interval_s=60,
    )


@pytest.mark.asyncio
async def test_persist_runs_outside_queue_lock_without_overbooking() -> None:
    queue = _queue(queue_max=2)
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def persist() -> None:
        persist_started.set()
        await release_persist.wait()

    pending = asyncio.create_task(queue.persist_and_enqueue("job-1", persist))
    await asyncio.wait_for(persist_started.wait(), timeout=1)

    assert await asyncio.wait_for(queue.enqueue("job-2"), timeout=0.1) == "enqueued"
    assert await queue.enqueue("job-3") == "full"
    state = await asyncio.wait_for(queue.snapshot(), timeout=0.1)
    assert state.queue_size == 1
    assert state.reserved == 1

    release_persist.set()
    assert await pending == "enqueued"
    assert queue.queue.qsize() == 2


@pytest.mark.asyncio
async def test_concurrent_persist_reservations_do_not_exceed_queue_capacity() -> None:
    queue = _queue()
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()
    second_persisted = False

    async def first_persist() -> None:
        persist_started.set()
        await release_persist.wait()

    async def second_persist() -> None:
        nonlocal second_persisted
        second_persisted = True

    first = asyncio.create_task(
        queue.persist_and_enqueue("job-1", first_persist)
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1)

    assert await queue.persist_and_enqueue("job-2", second_persist) == "full"
    assert second_persisted is False

    release_persist.set()
    assert await first == "enqueued"


@pytest.mark.asyncio
async def test_failed_or_cancelled_persist_releases_reserved_slot() -> None:
    queue = _queue()

    async def fail_persist() -> None:
        raise RuntimeError("disk unavailable")

    with pytest.raises(RuntimeError, match="disk unavailable"):
        await queue.persist_and_enqueue("job-failed", fail_persist)

    persist_started = asyncio.Event()

    async def blocked_persist() -> None:
        persist_started.set()
        await asyncio.Event().wait()

    blocked = asyncio.create_task(
        queue.persist_and_enqueue("job-cancelled", blocked_persist)
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked

    assert await queue.persist_and_enqueue(
        "job-next",
        _no_op_persist,
    ) == "enqueued"


@pytest.mark.asyncio
async def test_shutdown_after_persist_leaves_durable_job_for_reconcile() -> None:
    queue = _queue()
    persisted = False

    async def persist_then_shutdown() -> None:
        nonlocal persisted
        persisted = True
        queue.shutdown_event.set()

    result = await queue.persist_and_enqueue("job-1", persist_then_shutdown)

    assert persisted is True
    assert result == "persisted"
    assert queue.queue.empty()
    assert (await queue.snapshot()).reserved == 0

    queue.shutdown_event.clear()
    assert await queue.enqueue("job-1") == "enqueued"


@pytest.mark.asyncio
async def test_successful_processor_attempt_has_distinct_metrics() -> None:
    queue = _queue()
    processed = asyncio.Event()

    async def processor(_job_id: str) -> JobProcessOutcome:
        processed.set()
        return JobProcessOutcome.SUCCEEDED

    async def reconcile() -> None:
        return None

    queue.bind(processor=processor, reconcile=reconcile)
    await queue.startup()
    try:
        assert await queue.enqueue("job-1") == "enqueued"
        await asyncio.wait_for(processed.wait(), timeout=1)
        await asyncio.wait_for(queue.queue.join(), timeout=1)

        assert queue.metrics["attempts_finished_total"] == 1
        assert queue.metrics["processor_success_total"] == 1
        assert queue.metrics["processor_succeeded_total"] == 1
        assert queue.metrics["processor_failed_total"] == 0
        assert queue.metrics["processor_uncertain_total"] == 0
        assert queue.metrics["processor_skipped_total"] == 0
        assert queue.metrics["processor_crash_total"] == 0
        assert queue.metrics["jobs_completed_total"] == 1
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "metric"),
    [
        (JobProcessOutcome.SUCCEEDED, "processor_succeeded_total"),
        (JobProcessOutcome.FAILED, "processor_failed_total"),
        (JobProcessOutcome.UNCERTAIN, "processor_uncertain_total"),
        (JobProcessOutcome.SKIPPED_FENCE_LOST, "processor_skipped_total"),
        (JobProcessOutcome.SKIPPED_NOT_QUEUED, "processor_skipped_total"),
    ],
)
async def test_queue_metrics_follow_business_outcome(
    outcome: JobProcessOutcome,
    metric: str,
) -> None:
    queue = _queue()
    processed = asyncio.Event()

    async def processor(_job_id: str) -> JobProcessOutcome:
        processed.set()
        return outcome

    async def reconcile() -> None:
        return None

    queue.bind(processor=processor, reconcile=reconcile)
    await queue.startup()
    try:
        assert await queue.enqueue("job-1") == "enqueued"
        await asyncio.wait_for(processed.wait(), timeout=1)
        await asyncio.wait_for(queue.queue.join(), timeout=1)

        assert queue.metrics[metric] == 1
        assert queue.metrics["processor_succeeded_total"] == int(
            outcome is JobProcessOutcome.SUCCEEDED
        )
        assert queue.metrics["processor_success_total"] == int(
            outcome is JobProcessOutcome.SUCCEEDED
        )
        assert queue.metrics["jobs_completed_total"] == int(
            outcome is JobProcessOutcome.SUCCEEDED
        )
        assert queue.metrics["processor_crash_total"] == 0
    finally:
        await queue.shutdown()


async def _no_op_persist() -> None:
    return None
