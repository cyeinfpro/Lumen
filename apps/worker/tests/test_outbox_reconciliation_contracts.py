from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen_core.models import MemoryExtractionRun
from app.locks import release_owned_lock
from app.outbox.contracts import FAILURE_MATRIX as OUTBOX_FAILURE_MATRIX
from app.reconciliation.contracts import (
    FAILURE_MATRIX as RECONCILIATION_FAILURE_MATRIX,
)
from app.reconciliation.contracts import DomainReconciler
from app.reconciliation.memory import (
    MEMORY_RECONCILER,
    cancel_memory_run,
    requeue_memory_run,
)
from app.reconciliation.task_domains import (
    COMPLETION_RECONCILER,
    GENERATION_RECONCILER,
)
from app.tasks import outbox


def _policy_by_state(matrix, key: str):
    for policy in matrix:
        if getattr(policy, "failure", None) == key:
            return policy
        if getattr(policy, "state", None) == key:
            return policy
    raise AssertionError(f"missing failure policy: {key}")


def _memory_run(*, status: str = "retryable") -> MemoryExtractionRun:
    now = datetime.now(timezone.utc)
    return MemoryExtractionRun(
        id="memory-run-contract",
        event_id="memory-extract:user-message:assistant-message",
        user_id="user-1",
        conversation_id="conversation-1",
        source_message_id="user-message",
        assistant_message_id="assistant-message",
        status=status,
        owner="worker-1",
        job_id="job-1",
        fence=1,
        attempt=1,
        recovery_count=0,
        claimed_at=now - timedelta(minutes=2),
        lease_expires_at=now - timedelta(minutes=1),
        retry_reason="transient",
        memory_writes=[],
        undo_operations=[],
        undo_status="none",
        created_at=now - timedelta(minutes=3),
        updated_at=now - timedelta(minutes=2),
    )


def test_failure_matrices_freeze_retry_and_fail_closed_invariants() -> None:
    transient = _policy_by_state(
        OUTBOX_FAILURE_MATRIX,
        "transient_delivery_error",
    )
    assert transient.published is False
    assert transient.retry is True
    assert transient.persist_dlq is True

    unknown = _policy_by_state(
        RECONCILIATION_FAILURE_MATRIX,
        "lease_unknown",
    )
    assert unknown.mutate_task is False
    assert unknown.release_hold is False
    assert unknown.stage_outbox is False


def test_domain_reconcilers_implement_shared_protocol() -> None:
    assert isinstance(GENERATION_RECONCILER, DomainReconciler)
    assert isinstance(COMPLETION_RECONCILER, DomainReconciler)
    assert isinstance(MEMORY_RECONCILER, DomainReconciler)


def test_memory_requeue_apply_is_idempotent() -> None:
    run = _memory_run()
    now = datetime.now(timezone.utc)

    assert requeue_memory_run(run, now=now) is True
    first_fence = run.fence
    first_recovery_count = run.recovery_count

    assert requeue_memory_run(run, now=now + timedelta(seconds=1)) is False
    assert run.status == "pending"
    assert run.fence == first_fence
    assert run.recovery_count == first_recovery_count


def test_memory_cancel_apply_is_idempotent() -> None:
    run = _memory_run(status="running")
    now = datetime.now(timezone.utc)

    assert cancel_memory_run(run, reason="deleted", now=now) is True
    first_fence = run.fence

    assert (
        cancel_memory_run(
            run,
            reason="different-reason",
            now=now + timedelta(seconds=1),
        )
        is False
    )
    assert run.status == "canceled"
    assert run.cancel_reason == "deleted"
    assert run.fence == first_fence


@pytest.mark.asyncio
async def test_owned_lock_release_is_owner_checked_and_idempotent() -> None:
    class Redis:
        def __init__(self) -> None:
            self.value = "owner-token"

        async def eval(
            self,
            _script: str,
            _keys: int,
            _key: str,
            token: str,
        ) -> int:
            if self.value != token:
                return 0
            self.value = None
            return 1

    redis = Redis()

    assert (
        await release_owned_lock(
            redis,
            key="lock:test",
            token="owner-token",
        )
        is True
    )
    assert (
        await release_owned_lock(
            redis,
            key="lock:test",
            token="owner-token",
        )
        is False
    )


def test_compatibility_entrypoint_stays_thin_and_keeps_cron_callables() -> None:
    source_lines = Path(outbox.__file__).read_text().splitlines()
    assert len(source_lines) < 250
    publisher, task_reconciler, memory_reconciler = outbox.cron_jobs
    assert [job.coroutine for job in outbox.cron_jobs] == [
        outbox.publish_outbox,
        outbox.reconcile_tasks,
        outbox.reconcile_memory_extractions,
    ]
    assert publisher.second == set(range(0, 60, 2))
    assert publisher.run_at_startup is True
    assert task_reconciler.minute == set(range(0, 60))
    assert task_reconciler.run_at_startup is False
    assert memory_reconciler.second == {20}
    assert memory_reconciler.run_at_startup is True
