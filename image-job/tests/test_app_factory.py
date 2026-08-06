from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import zlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image


IMAGE_JOB_DIR = Path(__file__).resolve().parents[1]
if str(IMAGE_JOB_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGE_JOB_DIR))

from image_job.app_factory import create_app  # noqa: E402
from image_job.application.auth import (  # noqa: E402
    AuthFailure,
    authenticate,
    upstream_credential,
)
from image_job.application.queue_supervisor import QueueSupervisor  # noqa: E402
from image_job.config import (  # noqa: E402
    ImageJobSettings,
    ImageJobTimeouts,
    SecretText,
)
from image_job.contracts import JobProcessOutcome, UpstreamDispatchReceipt  # noqa: E402
from image_job.domain.identity import CallerIdentity, UpstreamCredential  # noqa: E402
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
        upstream_base_url="http://127.0.0.1:8081",
        public_base_url="https://images.example.test",
        timeouts=ImageJobTimeouts(graceful_shutdown_s=0),
        credential_active_key_id="test-v1",
        credential_master_secret=SecretText("test-master-secret-" + "x" * 32),
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
    assert runtime_a.upstream.processing.heartbeat.repository is runtime_a.repository
    assert not hasattr(runtime_a.upstream.processing, "touch_running")


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


@pytest.mark.parametrize("value", ["0", "1", "true"])
def test_removed_legacy_auth_config_is_rejected(value: str) -> None:
    with pytest.raises(RuntimeError, match="no longer supported"):
        ImageJobSettings.from_env(
            {"IMAGE_JOB_ALLOW_LEGACY_BEARER_AUTH": value}
        )


def test_authentication_requires_trusted_sidecar_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(AuthFailure) as arbitrary:
        authenticate(
            {"authorization": "Bearer sk-arbitrary-provider-key"},
            settings,
        )
    assert arbitrary.value.status_code == 401

    unconfigured = replace(settings, sidecar_token=SecretText(""))
    with pytest.raises(AuthFailure) as missing:
        authenticate(
            {"authorization": "Bearer sk-arbitrary-provider-key"},
            unconfigured,
        )
    assert missing.value.status_code == 503

    trusted = authenticate(
        {"authorization": f"Bearer {'s' * 32}"},
        settings,
    )
    assert trusted.service_id == "lumen-worker"
    assert trusted.authorization == f"Bearer {'s' * 32}"

    with pytest.raises(AuthFailure) as upstream_missing:
        upstream_credential({})
    assert upstream_missing.value.status_code == 400
    assert (
        upstream_credential(
            {"x-lumen-upstream-authorization": "Bearer sk-upstream"}
        ).authorization
        == "Bearer sk-upstream"
    )


@pytest.mark.asyncio
async def test_trusted_identity_can_read_pre_upgrade_job_only_with_upstream_key(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    await runtime.jobs.persistence.insert_job(
        "job-pre-upgrade",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
        "Bearer sk-old-provider",
    )
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        arbitrary = await client.get(
            "/v1/image-jobs/job-pre-upgrade",
            headers={
                "Authorization": "Bearer sk-old-provider",
                "X-Lumen-Upstream-Authorization": "Bearer sk-old-provider",
            },
        )
        missing_upstream = await client.get(
            "/v1/image-jobs/job-pre-upgrade",
            headers={"Authorization": f"Bearer {'s' * 32}"},
        )
        compatible = await client.get(
            "/v1/image-jobs/job-pre-upgrade",
            headers={
                "Authorization": f"Bearer {'s' * 32}",
                "X-Lumen-Upstream-Authorization": "Bearer sk-old-provider",
            },
        )

    assert arbitrary.status_code == 401
    assert missing_upstream.status_code == 403
    assert compatible.status_code == 200
    assert compatible.json()["job_id"] == "job-pre-upgrade"


def _job_headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {'s' * 32}",
        "X-Lumen-Upstream-Authorization": "Bearer sk-upstream",
        "Content-Type": "application/json",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _job_payload(prompt: str = "cat") -> dict[str, object]:
    return {
        "endpoint": "/v1/images/generations",
        "body": {"prompt": prompt},
    }


@pytest.mark.asyncio
async def test_paid_job_requires_valid_idempotency_key(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        missing = await client.post(
            "/v1/image-jobs",
            headers=_job_headers(),
            json=_job_payload(),
        )
        invalid = await client.post(
            "/v1/image-jobs",
            headers=_job_headers(idempotency_key="bad key"),
            json=_job_payload(),
        )

    count = await runtime.repository.one("SELECT COUNT(*) AS count FROM jobs")
    assert missing.status_code == 428
    assert invalid.status_code == 400
    assert count is not None
    assert count["count"] == 0


@pytest.mark.asyncio
async def test_paid_job_idempotency_replays_and_conflicts(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = _job_headers(idempotency_key="stable-job-1")
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/v1/image-jobs",
            headers=headers,
            json=_job_payload("cat"),
        )
        second = await client.post(
            "/v1/image-jobs",
            headers=headers,
            json=_job_payload("cat"),
        )
        conflict = await client.post(
            "/v1/image-jobs",
            headers=headers,
            json=_job_payload("dog"),
        )

    count = await runtime.repository.one("SELECT COUNT(*) AS count FROM jobs")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert conflict.status_code == 409
    assert count is not None
    assert count["count"] == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_creates_one_paid_job(tmp_path: Path) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        first, second = await asyncio.gather(
            *(
                client.post(
                    "/v1/image-jobs",
                    headers=_job_headers(idempotency_key="concurrent-stable-1"),
                    json=_job_payload(),
                )
                for _ in range(2)
            )
        )

    rows = await runtime.repository.all("SELECT job_id FROM jobs")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(rows) == 1


def test_responses_stream_limit_cannot_be_lower_than_single_image_limit() -> None:
    settings = ImageJobSettings.from_env(
        {
            "IMAGE_JOB_MAX_IMAGE_BYTES": str(80 * 1024 * 1024),
            "IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES": "1000",
        }
    )

    assert settings.max_image_bytes == 80 * 1024 * 1024
    assert settings.responses_stream_max_bytes == settings.max_image_bytes


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


def test_sqlite_readiness_probe_uses_read_only_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = create_runtime(_settings(tmp_path)).repository
    statements: list[str] = []

    class Result:
        @staticmethod
        def fetchone() -> tuple[int]:
            return (1,)

    class Connection:
        def execute(self, sql: str) -> Result:
            statements.append(" ".join(sql.split()))
            return Result()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(repository, "_open_readonly", Connection)

    assert repository._readiness_probe_sync() is True  # noqa: SLF001
    assert statements == ["SELECT 1"]


def test_sqlite_readiness_connection_opens_database_in_ro_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = create_runtime(_settings(tmp_path)).repository
    repository.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(repository.settings.db_path).close()
    calls: list[tuple[str, dict[str, Any]]] = []

    class Connection:
        row_factory: Any = None

        @staticmethod
        def execute(_sql: str) -> None:
            return None

        @staticmethod
        def close() -> None:
            return None

    def connect(database: str, **kwargs: Any) -> Connection:
        calls.append((database, kwargs))
        return Connection()

    monkeypatch.setattr(sqlite3, "connect", connect)

    repository._open_readonly().close()  # noqa: SLF001

    assert calls[0][0].endswith("?mode=ro")
    assert calls[0][1]["uri"] is True
    assert calls[0][1]["isolation_level"] is None


@pytest.mark.asyncio
async def test_queue_supervisor_replaces_crashed_worker() -> None:
    crashed = asyncio.Event()

    async def processor(_job_id: str) -> JobProcessOutcome:
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
        assert queue.metrics["attempts_finished_total"] == 1
        assert queue.metrics["processor_success_total"] == 0
        assert queue.metrics["processor_crash_total"] == 1
        assert queue.metrics["jobs_completed_total"] == 0
    finally:
        await queue.shutdown()


def test_deployment_entrypoint_remains_app_colon_app() -> None:
    root = IMAGE_JOB_DIR.parent
    service = (root / "deploy/image-job/image-job.service").read_text()

    assert "uvicorn app:app --host 127.0.0.1 --port 8091" in service


async def _seed_queued_job(runtime, job_id: str) -> None:
    await runtime.repository.initialize()
    await runtime.jobs.persistence.insert_job(
        job_id,
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
        "Bearer sk-test",
    )


async def _job_row(runtime, job_id: str):
    row = await runtime.repository.one(
        "SELECT * FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_worker_crash_after_dispatch_marks_job_uncertain(tmp_path: Path) -> None:
    # H-4：upstream.call 已经派发之后的非 JobFailure 崩溃，上游是否计费不可知，
    # 必须落 uncertain（不可退款），而不是 failed（确定未扣费）。
    runtime = create_runtime(_settings(tmp_path))
    await _seed_queued_job(runtime, "job-crash-after")

    async def dispatched_crashing_call(
        _row,
        *,
        dispatch: UpstreamDispatchReceipt,
        **_kwargs,
    ):
        dispatch.mark_started("test.send_request_headers.started")
        raise RuntimeError("boom after dispatch")

    runtime.jobs.upstream.call = dispatched_crashing_call

    await runtime.jobs.process("job-crash-after")

    row = await _job_row(runtime, "job-crash-after")
    assert row["status"] == "uncertain"
    assert bool(row["outcome_uncertain"]) is True
    assert bool(row["retry_suppressed"]) is True


@pytest.mark.asyncio
async def test_persistence_crash_after_success_marks_job_uncertain(
    tmp_path: Path,
) -> None:
    # 上游已经成功交付、只是本地落库崩了：上游一定扣过费，同样禁止 failed。
    runtime = create_runtime(_settings(tmp_path))
    await _seed_queued_job(runtime, "job-persist-crash")

    async def succeeding_call(
        _row,
        *,
        dispatch: UpstreamDispatchReceipt,
        **_kwargs,
    ):
        dispatch.mark_started("test.send_request_headers.started")
        return 200, [{"url": "https://images.example.test/a.png"}]

    runtime.jobs.upstream.call = succeeding_call

    class _CrashOnSuccessPersistence:
        """只让 mark_succeeded 崩溃，其余落库调用照常转发给真实门面。

        JobPersistenceFacade 是 frozen dataclass，不能直接改字段，所以整体替换。
        """

        def __init__(self, inner) -> None:
            self._inner = inner

        async def mark_succeeded(self, *_args, **_kwargs) -> None:
            raise RuntimeError("sqlite write failed")

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    runtime.jobs.persistence = _CrashOnSuccessPersistence(runtime.jobs.persistence)

    await runtime.jobs.process("job-persist-crash")

    row = await _job_row(runtime, "job-persist-crash")
    assert row["status"] == "uncertain"
    assert bool(row["outcome_uncertain"]) is True


@pytest.mark.asyncio
async def test_submit_reconciles_row_persisted_during_shutdown(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    await runtime.repository.initialize()
    persistence = runtime.jobs.persistence

    class _ShutdownAfterInsert:
        def __getattr__(self, name: str):
            return getattr(persistence, name)

        async def insert_job(self, *args, **kwargs) -> None:
            await persistence.insert_job(*args, **kwargs)
            runtime.queue.shutdown_event.set()

    runtime.jobs.persistence = _ShutdownAfterInsert()
    result = await runtime.jobs.submit(
        caller=CallerIdentity(
            service_id="test",
            owner_hash="owner-hash",
            authorization="Bearer owner",
        ),
        upstream=UpstreamCredential("Bearer upstream"),
        payload={
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
        idempotency_key="test-submit-reconcile",
    )

    assert result["status"] == "queued"
    assert runtime.queue.queue.empty()
    row = await runtime.repository.one(
        "SELECT job_id, status FROM jobs WHERE job_id = ?",
        (result["job_id"],),
    )
    assert row is not None
    assert row["status"] == "queued"

    runtime.queue.shutdown_event.clear()
    await runtime.jobs.reconcile()
    assert await runtime.queue.queue.get() == result["job_id"]


@pytest.mark.asyncio
async def test_shutdown_closes_http_client_when_startup_never_finished(
    tmp_path: Path,
) -> None:
    # H-16：upstream.startup() 成功但 queue.startup() 失败（或 lifespan 被取消）
    # 时 started 仍是 False，早退就把 httpx 连接池永久泄漏在进程里。
    runtime = create_runtime(_settings(tmp_path))
    await runtime.upstream.startup()
    client = runtime.upstream.client
    assert client is not None
    assert runtime.started is False

    await runtime.shutdown()

    assert runtime.upstream.client is None
    assert client.is_closed


@pytest.mark.asyncio
async def test_lifespan_closes_upstream_when_queue_startup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    clients: list[httpx.AsyncClient] = []

    async def fail_startup() -> None:
        client = runtime.upstream.client
        assert client is not None
        clients.append(client)
        raise RuntimeError("queue startup failed")

    monkeypatch.setattr(runtime.queue, "startup", fail_startup)
    app = create_app(runtime=runtime)

    with pytest.raises(RuntimeError, match="queue startup failed"):
        async with app.router.lifespan_context(app):
            pass

    assert runtime.upstream.client is None
    assert len(clients) == 1
    assert clients[0].is_closed
    assert runtime.started is False


@pytest.mark.asyncio
async def test_retention_sweeper_is_bound_and_started(tmp_path: Path) -> None:
    # H-17：不 bind retention 的话保留期清扫协程根本不会起，磁盘只增不减。
    runtime = create_runtime(_settings(tmp_path))
    assert runtime.queue.retention_callback is not None

    await runtime.startup()
    try:
        assert "retention" in runtime.queue.background
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_retention_pass_removes_orphan_job_dirs_only(tmp_path: Path) -> None:
    # H-17：jobs 行已经不存在的产物目录没有任何人负责，必须兜底清掉；
    # 但仍有行的（哪怕还停在 queued 从未 finished）绝不能误删。
    runtime = create_runtime(_settings(tmp_path))
    await _seed_queued_job(runtime, "job-alive")

    temp_root = runtime.settings.data_dir / "images" / "temp"
    alive_dir = temp_root / "2026" / "07" / "01" / "job-alive"
    orphan_dir = temp_root / "2026" / "07" / "01" / "job-orphan"
    for target in (alive_dir, orphan_dir):
        target.mkdir(parents=True, exist_ok=True)
        (target / "image-1.png").write_bytes(b"\x89PNG\r\n\x1a\npayload")
        os.utime(target, (1, 1))

    await runtime.jobs.retention.run_pass()

    assert (alive_dir / "image-1.png").is_file()
    assert not orphan_dir.exists()


@pytest.mark.asyncio
async def test_request_id_is_echoed_persisted_and_generated(tmp_path: Path) -> None:
    # H-19：跨服务追踪。调用方带来的 request_id 必须原样回显并落库，
    # 没带的请求也要有一个本地生成的 id。
    runtime = create_runtime(_settings(tmp_path))
    app = create_app(runtime=runtime)
    headers = {
        "Authorization": f"Bearer {'s' * 32}",
        "X-Lumen-Upstream-Authorization": "Bearer sk-upstream",
        "Content-Type": "application/json",
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            generated = await client.get("/livez")
            created = await client.post(
                "/v1/image-jobs",
                headers={
                    **headers,
                    "X-Request-Id": "req-from-worker",
                    "Idempotency-Key": "request-id-persistence",
                },
                json={
                    "endpoint": "/v1/images/generations",
                    "body": {"prompt": "cat"},
                },
            )
        row = await _job_row(runtime, created.json()["job_id"])

    assert created.status_code == 200
    assert created.headers["x-request-id"] == "req-from-worker"
    assert row["request_id"] == "req-from-worker"
    assert generated.headers["x-request-id"].startswith("ij_")


@pytest.mark.asyncio
async def test_metrics_expose_business_outcomes(tmp_path: Path) -> None:
    # H-19：uncertain 计数就是「上游可能已扣费但没交付」的待对账工单量。
    runtime = create_runtime(_settings(tmp_path))
    await _seed_queued_job(runtime, "job-metrics")

    async def crashing_call(
        _row,
        *,
        dispatch: UpstreamDispatchReceipt,
        **_kwargs,
    ):
        dispatch.mark_started("test.send_request_headers.started")
        raise RuntimeError("boom after dispatch")

    runtime.jobs.upstream.call = crashing_call
    await runtime.jobs.process("job-metrics")

    text = await runtime.metrics_text()

    assert "image_job_jobs_uncertain_total 1" in text
    assert "image_job_jobs_failed_total 0" in text
    assert "image_job_jobs_succeeded_total 0" in text
    assert "image_job_images_delivered_total 0" in text


def _png_decompression_bomb(width: int, height: int) -> bytes:
    """只有 57 字节、却自称 width x height 的 PNG。

    Pillow 在 open() 阶段只读 IHDR，就会因为像素数超限抛
    DecompressionBombError —— 不需要真的构造一张巨图。
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + zlib.crc32(tag + data).to_bytes(4, "big")
        )

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([1, 0, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b"")
    )


@pytest.mark.asyncio
async def test_reference_decompression_bomb_returns_413(tmp_path: Path) -> None:
    # H-15：Pillow 的 DecompressionBombError 直接继承 Exception，不是
    # OSError/ValueError，原来的 except 元组接不住，会冒到 ASGI 层变成 500。
    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    settings = replace(_settings(tmp_path), max_image_pixels=1_000_000)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/refs",
                headers={
                    "Authorization": f"Bearer {'s' * 32}",
                    "Content-Type": "image/png",
                },
                content=_png_decompression_bomb(20000, 20000),
            )

    assert response.status_code == 413
    assert "pixel limit" in response.json()["detail"]
    # 进程级全局阈值必须还原，不能被这次请求永久改掉。
    assert Image.MAX_IMAGE_PIXELS == previous_max_pixels


@pytest.mark.asyncio
async def test_worker_crash_before_dispatch_stays_failed(tmp_path: Path) -> None:
    # 对称约束：请求还没交给上游就崩，必须留在 failed 让调用方安心退款。
    runtime = create_runtime(_settings(tmp_path))
    await _seed_queued_job(runtime, "job-crash-before")

    async def unreachable_call(_row):
        raise AssertionError("upstream must not be reached")

    runtime.jobs.upstream.call = unreachable_call

    original_one = runtime.repository.one
    calls = {"n": 0}

    async def flaky_one(sql: str, params=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db read failed before dispatch")
        return await original_one(sql, params)

    runtime.jobs.repository = SimpleNamespace(one=flaky_one)

    await runtime.jobs.process("job-crash-before")

    row = await _job_row(runtime, "job-crash-before")
    assert row["status"] == "failed"
    assert bool(row["outcome_uncertain"]) is False
    assert bool(row["retry_suppressed"]) is False
