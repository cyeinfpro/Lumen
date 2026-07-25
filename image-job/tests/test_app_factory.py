from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image


IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
if str(IMAGE_JOB_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGE_JOB_DIR))

from image_job.app_factory import create_app  # noqa: E402
from image_job.application.queue_supervisor import QueueSupervisor  # noqa: E402
from image_job.config import (  # noqa: E402
    ImageJobSettings,
    ImageJobTimeouts,
    SecretText,
)
from image_job.runtime import create_runtime  # noqa: E402


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
        sidecar_token=SecretText("s" * 32),
        allow_legacy_bearer=False,
        upstream_base_url="http://127.0.0.1:8081",
        public_base_url="https://images.example.test",
        timeouts=ImageJobTimeouts(graceful_shutdown_s=0),
        stuck_reconcile_interval_s=60,
        retention_sweep_interval_s=60,
    )


def test_create_app_keeps_runtime_state_per_instance(tmp_path: Path) -> None:
    runtime_a = create_runtime(_settings(tmp_path / "a"))
    runtime_b = create_runtime(_settings(tmp_path / "b"))
    app_a = create_app(runtime=runtime_a)
    app_b = create_app(runtime=runtime_b)

    assert app_a.state.runtime is runtime_a
    assert app_b.state.runtime is runtime_b
    assert runtime_a.queue.queue is not runtime_b.queue.queue
    assert runtime_a.queue.shutdown_event is not runtime_b.queue.shutdown_event
    assert (
        runtime_a.repository.settings.db_path != runtime_b.repository.settings.db_path
    )


def test_settings_hide_secrets_and_reject_missing_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert "s" * 32 not in repr(settings)

    invalid = ImageJobSettings(
        **{
            **settings.__dict__,
            "sidecar_token": SecretText(""),
        }
    )
    with pytest.raises(RuntimeError, match="IMAGE_JOB_SIDECAR_TOKEN is required"):
        invalid.validate()


@pytest.mark.asyncio
async def test_health_contract_is_minimal_and_metrics_require_auth(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            live = await client.get("/livez")
            ready = await client.get("/readyz")
            health = await client.get("/health")
            denied = await client.get("/metrics")
            metrics = await client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {'s' * 32}"},
            )

    assert live.json() == {"status": "ok"}
    assert ready.json() == {"status": "ready"}
    assert health.json() == {"status": "ready"}
    assert set(health.json()) == {"status"}
    assert denied.status_code == 401
    assert metrics.status_code == 200
    assert "image_job_queue_size" in metrics.text


@pytest.mark.asyncio
async def test_reference_service_uses_runtime_repository_and_artifacts(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(30, 40, 50)).save(buffer, format="PNG")
    headers = {
        "Authorization": f"Bearer {'s' * 32}",
        "Content-Type": "image/png",
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            first = await client.post(
                "/v1/refs",
                headers=headers,
                content=buffer.getvalue(),
            )
            second = await client.post(
                "/v1/refs",
                headers=headers,
                content=buffer.getvalue(),
            )

    assert first.status_code == 200
    assert first.json()["deduped"] is False
    assert second.json()["deduped"] is True
    assert first.json()["url"] == second.json()["url"]


@pytest.mark.asyncio
async def test_readiness_fails_when_runtime_is_shutting_down(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.startup()
    try:
        runtime.queue.shutdown_event.set()
        ready, failures = await runtime.readiness()
    finally:
        await runtime.shutdown()

    assert ready is False
    assert "queue_not_accepting" in failures


@pytest.mark.asyncio
async def test_queue_supervisor_replaces_crashed_worker() -> None:
    crashed = asyncio.Event()

    async def processor(_job_id: str) -> None:
        crashed.set()
        raise RuntimeError("boom")

    async def reconcile() -> None:
        return None

    queue = QueueSupervisor(
        queue_max=1,
        concurrency=1,
        graceful_shutdown_s=0,
        reconcile_interval_s=60,
        retention_interval_s=60,
    )
    queue.bind(processor=processor, reconcile=reconcile)
    await queue.startup()
    try:
        original = queue.workers[1]
        assert await queue.enqueue("job-1") == "enqueued"
        await asyncio.wait_for(crashed.wait(), timeout=1)
        for _ in range(100):
            if queue.workers[1] is not original:
                break
            await asyncio.sleep(0.01)
        assert queue.workers[1] is not original
        assert queue.metrics["worker_failures_total"] == 1
        assert queue.metrics["worker_restarts_total"] == 1
    finally:
        await queue.shutdown()


def test_deployment_entrypoint_remains_app_colon_app() -> None:
    root = IMAGE_JOB_DIR.parent
    service = (root / "deploy/image-job/image-job.service").read_text()

    assert "uvicorn app:app --host 127.0.0.1 --port 8091" in service
