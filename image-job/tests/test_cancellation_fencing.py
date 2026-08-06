from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from image_job.app_factory import create_app
from image_job.config import ImageJobSettings, ImageJobTimeouts, SecretText
from image_job.runtime import create_runtime


SERVICE_TOKEN = "s" * 32
AUTH_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _settings(tmp_path: Path) -> ImageJobSettings:
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    return ImageJobSettings(
        data_dir=data_dir,
        refs_dir=data_dir / "refs",
        state_dir=state_dir,
        db_path=state_dir / "jobs.sqlite3",
        queue_max=2,
        concurrency=1,
        sidecar_token=SecretText(SERVICE_TOKEN),
        upstream_base_url="http://127.0.0.1:8081",
        public_base_url="https://images.example.test",
        timeouts=ImageJobTimeouts(graceful_shutdown_s=0),
        credential_active_key_id="test-v1",
        credential_master_secret=SecretText("test-master-secret-" + "x" * 32),
        stuck_reconcile_interval_s=60,
        retention_sweep_interval_s=60,
    )


def _payload() -> dict[str, object]:
    return {
        "request_type": "generations",
        "endpoint": "/v1/images/generations",
        "body": {"prompt": "cat"},
        "retention_days": 1,
    }


def _test_artifact() -> dict[str, object]:
    return {
        "url": "https://images.example.test/images/temp/test.png",
        "width": 1,
        "height": 1,
        "bytes": 1,
        "format": "png",
        "expires_at": "2026-08-07T00:00:00+00:00",
        "sha256": "0" * 64,
    }


async def _seed(runtime, job_id: str) -> None:
    await runtime.jobs.persistence.insert_job(
        job_id,
        _payload(),
        "Bearer upstream-key",
        owner_auth_header=f"Bearer {SERVICE_TOKEN}",
    )


async def _delete(runtime, job_id: str, headers=None) -> httpx.Response:
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.delete(
            f"/v1/image-jobs/{job_id}",
            headers=headers,
        )


@pytest.mark.asyncio
async def test_delete_requires_auth_and_returns_404(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()

    denied = await _delete(runtime, "missing")
    missing = await _delete(runtime, "missing", AUTH_HEADERS)

    assert denied.status_code == 401
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_delete_queued_job_cancels_before_dispatch(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await _seed(runtime, "job-queued")

    response = await _delete(runtime, "job-queued", AUTH_HEADERS)
    row = await runtime.repository.one(
        "SELECT * FROM jobs WHERE job_id = ?",
        ("job-queued",),
    )

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job-queued",
        "outcome": "cancelled_before_dispatch",
        "status": "cancelled",
        "outcome_uncertain": False,
    }
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["execution_token"] is None
    assert row["auth_ciphertext"] is None


@pytest.mark.asyncio
async def test_delete_running_job_records_uncertain_cancel_request(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await _seed(runtime, "job-running")
    execution_token = await runtime.jobs.persistence.mark_running("job-running")
    assert execution_token

    response = await _delete(runtime, "job-running", AUTH_HEADERS)
    row = await runtime.repository.one(
        "SELECT * FROM jobs WHERE job_id = ?",
        ("job-running",),
    )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": "job-running",
        "outcome": "cancel_requested",
        "status": "cancel_requested",
        "outcome_uncertain": True,
    }
    assert row is not None
    assert row["status"] == "cancel_requested"
    assert bool(row["outcome_uncertain"]) is True
    assert row["execution_token"] is None
    assert (
        await runtime.jobs.persistence.mark_succeeded(
            "job-running",
            execution_token=execution_token,
            upstream_status=200,
            elapsed_ms=1,
            images=[],
        )
        is False
    )


@pytest.mark.asyncio
async def test_delete_terminal_job_is_idempotent(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await _seed(runtime, "job-terminal")
    execution_token = await runtime.jobs.persistence.mark_running("job-terminal")
    assert execution_token
    assert await runtime.jobs.persistence.mark_failed(
        "job-terminal",
        execution_token=execution_token,
        error="failed",
    )

    response = await _delete(runtime, "job-terminal", AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["outcome"] == "already_terminal"
    assert response.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_delete_running_without_execution_fence_returns_409(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await _seed(runtime, "job-unfenced")
    await runtime.repository.execute(
        """
        UPDATE jobs
        SET status = 'running',
            started_at = updated_at,
            execution_token = NULL
        WHERE job_id = ?
        """,
        ("job-unfenced",),
    )

    response = await _delete(runtime, "job-unfenced", AUTH_HEADERS)

    assert response.status_code == 409
    assert response.json() == {
        "job_id": "job-unfenced",
        "outcome": "uncertain",
        "status": "running",
        "outcome_uncertain": True,
    }


@pytest.mark.asyncio
async def test_stale_execution_token_cannot_overwrite_new_attempt(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(
        replace(_settings(tmp_path), upstream_idempotency_guaranteed=True)
    )
    await runtime.repository.initialize()
    await _seed(runtime, "job-fenced")
    stale_token = await runtime.jobs.persistence.mark_running("job-fenced")
    assert stale_token
    assert await runtime.jobs.stale_jobs.recover(
        "job-fenced",
        execution_token=stale_token,
        requeue=True,
    )
    current_token = await runtime.jobs.persistence.mark_running("job-fenced")
    assert current_token
    assert current_token != stale_token

    assert not await runtime.jobs.persistence.touch_running(
        "job-fenced",
        stale_token,
    )
    assert not await runtime.jobs.persistence.mark_failed(
        "job-fenced",
        execution_token=stale_token,
        error="stale failure",
    )
    assert not await runtime.jobs.persistence.mark_cancelled(
        "job-fenced",
        execution_token=stale_token,
    )

    async def accept_artifacts(
        _job_id: str,
        images: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        return images

    runtime.jobs.persistence = replace(
        runtime.jobs.persistence,
        verify_artifacts=accept_artifacts,
    )
    assert await runtime.jobs.persistence.mark_succeeded(
        "job-fenced",
        execution_token=current_token,
        upstream_status=200,
        elapsed_ms=1,
        images=[_test_artifact()],
    )

    row = await runtime.repository.one(
        "SELECT status, attempts FROM jobs WHERE job_id = ?",
        ("job-fenced",),
    )
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2


@pytest.mark.asyncio
async def test_active_stale_recovery_uses_token_cas(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await _seed(runtime, "job-stale-cas")
    stale_token = await runtime.jobs.persistence.mark_running("job-stale-cas")
    assert stale_token
    await runtime.repository.execute(
        """
        UPDATE jobs
        SET execution_token = 'new-owner-token'
        WHERE job_id = ?
        """,
        ("job-stale-cas",),
    )

    assert not await runtime.jobs.stale_jobs.recover(
        "job-stale-cas",
        execution_token=stale_token,
        requeue=False,
    )
    row = await runtime.repository.one(
        "SELECT status, execution_token FROM jobs WHERE job_id = ?",
        ("job-stale-cas",),
    )
    assert row is not None
    assert row["status"] == "running"
    assert row["execution_token"] == "new-owner-token"


@pytest.mark.asyncio
async def test_reconciler_transitions_only_truly_stale_running_job(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings(tmp_path),
        retry_network_max=0,
        retry_responses_stream_max=0,
        retry_upstream_5xx_max=0,
        timeouts=ImageJobTimeouts(upstream_s=1, graceful_shutdown_s=0),
    )
    runtime = create_runtime(settings)
    await runtime.repository.initialize()
    await _seed(runtime, "job-stale-running")
    await _seed(runtime, "job-queued-live")
    execution_token = await runtime.jobs.persistence.mark_running(
        "job-stale-running"
    )
    assert execution_token
    stale_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=runtime.jobs.stale_jobs.active_stale_after_s() + 1)
    ).isoformat()
    await runtime.repository.execute(
        "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
        (stale_at, "job-stale-running"),
    )

    await runtime.jobs.reconcile()

    rows = await runtime.repository.all(
        """
        SELECT job_id, status
        FROM jobs
        WHERE job_id IN ('job-stale-running', 'job-queued-live')
        ORDER BY job_id
        """
    )
    by_id = {row["job_id"]: row["status"] for row in rows}
    assert by_id == {
        "job-queued-live": "queued",
        "job-stale-running": "uncertain",
    }


@pytest.mark.asyncio
async def test_retention_never_cleans_queued_or_running_jobs(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    for job_id in ("job-queued-live", "job-running-live", "job-terminal-old"):
        await _seed(runtime, job_id)
    running_token = await runtime.jobs.persistence.mark_running("job-running-live")
    terminal_token = await runtime.jobs.persistence.mark_running("job-terminal-old")
    assert running_token
    assert terminal_token
    assert await runtime.jobs.persistence.mark_failed(
        "job-terminal-old",
        execution_token=terminal_token,
        error="failed",
    )
    await runtime.repository.execute(
        """
        UPDATE jobs
        SET created_at = '2026-07-01T00:00:00+00:00',
            updated_at = '2026-07-01T00:00:00+00:00',
            finished_at = CASE
                WHEN status = 'failed' THEN '2026-07-01T01:00:00+00:00'
                ELSE finished_at
            END,
            retention_expires_at = '2026-07-02T00:00:00+00:00'
        """
    )
    temp_root = runtime.settings.data_dir / "images" / "temp" / "2026" / "07" / "01"
    for job_id in ("job-queued-live", "job-running-live", "job-terminal-old"):
        job_dir = temp_root / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "image-1.png").write_bytes(b"payload")

    runtime.jobs.retention = replace(
        runtime.jobs.retention,
        utc_now=lambda: datetime(2026, 7, 29, tzinfo=timezone.utc),
        sweep_filesystem_fn=lambda _cutoff: (0, 0),
    )
    await runtime.jobs.retention.run_pass()

    rows = await runtime.repository.all(
        """
        SELECT job_id, status, auth_ciphertext
        FROM jobs
        ORDER BY job_id
        """
    )
    by_id = {row["job_id"]: row for row in rows}
    assert set(by_id) == {"job-queued-live", "job-running-live"}
    assert by_id["job-queued-live"]["status"] == "queued"
    assert by_id["job-running-live"]["status"] == "running"
    assert by_id["job-queued-live"]["auth_ciphertext"] is not None
    assert by_id["job-running-live"]["auth_ciphertext"] is not None
    assert (temp_root / "job-queued-live" / "image-1.png").is_file()
    assert (temp_root / "job-running-live" / "image-1.png").is_file()
    assert not (temp_root / "job-terminal-old").exists()
