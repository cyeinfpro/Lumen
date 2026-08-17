from __future__ import annotations

import json
from typing import Any

import pytest

from lumen_core.volcano_assets import volcano_asset_operation_key


class _Redis:
    def __init__(
        self,
        operations: list[dict[str, Any]],
        *,
        active_operation_locks: set[str] | None = None,
    ) -> None:
        self.values = {
            volcano_asset_operation_key(str(operation["id"])): json.dumps(operation)
            for operation in operations
        }
        self.active_operation_locks = active_operation_locks or set()
        self.enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.recovery_token: str | None = None

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and self.recovery_token is not None:
            return False
        self.recovery_token = value
        return True

    async def scan_iter(self, *, match: str):
        assert match == "video-assets:operation:*"
        for key in list(self.values):
            yield key

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def exists(self, key: str) -> int:
        return int(key in self.active_operation_locks)

    async def enqueue_job(
        self,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        self.enqueued.append((name, args, kwargs))
        return object()

    async def eval(self, _script: str, _numkeys: int, *_parts: Any) -> int:
        self.recovery_token = None
        return 1

    def operation(self, operation_id: str) -> dict[str, Any]:
        return json.loads(self.values[volcano_asset_operation_key(operation_id)])


def _operation(**changes: Any) -> dict[str, Any]:
    return {
        "id": "operation-1",
        "action": "create_asset",
        "status": "queued",
        "progress_stage": "queued",
        "attempt": 1,
        "delivery_generation": 0,
        "delivery_enqueued": True,
        "retryable": True,
        "retry_after_seconds": None,
        "retry_not_before": None,
        "user_id": "user-1",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "completed_at": None,
        "result": None,
        "error": None,
        **changes,
    }


async def _install_cas(
    monkeypatch: pytest.MonkeyPatch,
    recovery: Any,
) -> None:
    async def compare_and_set(
        redis: _Redis,
        operation_id: str,
        *,
        owner_user_id: str,
        expected_status: str,
        expected_attempt: int,
        replacement: dict[str, Any],
        expected_progress_stage: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        current = redis.operation(operation_id)
        assert current["user_id"] == owner_user_id
        if (
            current["status"] != expected_status
            or int(current["attempt"]) != expected_attempt
            or (
                expected_progress_stage is not None
                and current["progress_stage"] != expected_progress_stage
            )
        ):
            return False, current
        redis.values[volcano_asset_operation_key(operation_id)] = json.dumps(
            replacement
        )
        return True, replacement

    monkeypatch.setattr(
        recovery,
        "compare_and_set_volcano_asset_operation",
        compare_and_set,
    )


@pytest.mark.asyncio
async def test_recovery_resumes_legacy_ambiguous_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_asset_recovery as recovery

    await _install_cas(monkeypatch, recovery)
    redis = _Redis(
        [
            _operation(
                status="failed",
                progress_stage="failed",
                completed_at="2026-08-14T00:01:00+00:00",
                submit_started_at="2026-08-14T00:00:30+00:00",
                submit_outcome_uncertain=True,
                error={
                    "code": "volcano_asset_create_reconcile_ambiguous",
                    "message": "ambiguous",
                    "retryable": True,
                },
            )
        ]
    )

    recovered = await recovery.reconcile_volcano_asset_operations({"redis": redis})

    assert recovered == 1
    stored = redis.operation("operation-1")
    assert stored["status"] == "queued"
    assert stored["progress_stage"] == "reconciling_submit"
    assert stored["delivery_generation"] == 1
    assert stored["delivery_enqueued"] is True
    assert redis.enqueued[0][0] == "process_volcano_asset_operation"


@pytest.mark.asyncio
async def test_recovery_resumes_legacy_terminal_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_asset_recovery as recovery

    await _install_cas(monkeypatch, recovery)
    redis = _Redis(
        [
            _operation(
                status="failed",
                progress_stage="failed",
                completed_at="2026-08-14T00:01:00+00:00",
                submit_started_at=None,
                submit_outcome_uncertain=False,
                error={
                    "code": "volcano_asset_rate_limited",
                    "message": "rate limited",
                    "retryable": True,
                },
            )
        ]
    )

    recovered = await recovery.reconcile_volcano_asset_operations({"redis": redis})

    assert recovered == 1
    stored = redis.operation("operation-1")
    assert stored["status"] == "queued"
    assert stored["progress_stage"] == "waiting_rate_limit"
    assert stored["delivery_generation"] == 1
    assert stored["delivery_enqueued"] is True
    assert redis.enqueued[0][0] == "process_volcano_asset_operation"


@pytest.mark.asyncio
async def test_recovery_requeues_stale_unconfirmed_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_asset_recovery as recovery

    await _install_cas(monkeypatch, recovery)
    redis = _Redis([_operation(delivery_enqueued=False)])

    recovered = await recovery.reconcile_volcano_asset_operations({"redis": redis})

    assert recovered == 1
    assert redis.operation("operation-1")["delivery_generation"] == 1


@pytest.mark.asyncio
async def test_recovery_skips_running_operation_with_live_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_asset_recovery as recovery

    await _install_cas(monkeypatch, recovery)
    redis = _Redis(
        [_operation(status="running", progress_stage="submitting")],
        active_operation_locks={"video-assets:operation-lock:operation-1"},
    )

    recovered = await recovery.reconcile_volcano_asset_operations({"redis": redis})

    assert recovered == 0
    assert redis.enqueued == []


def test_worker_registers_volcano_asset_recovery_cron() -> None:
    from app.main import WorkerSettings
    from app.tasks import volcano_asset_recovery as recovery

    assert any(
        item.coroutine is recovery.reconcile_volcano_asset_operations
        for item in WorkerSettings.cron_jobs
    )
