from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from image_job import durable_files
from image_job.adapters.filesystem_artifacts import FilesystemArtifactStore
from image_job.adapters.sqlite_jobs import SQLiteJobRepository
from image_job.candidates import ImageCandidate
from image_job.config import ImageJobSettings, ImageJobTimeouts, SecretText
from image_job.runtime import create_runtime


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
        credential_active_key_id="test-v1",
        credential_master_secret=SecretText("test-master-secret-" + "x" * 32),
        stuck_reconcile_interval_s=60,
        retention_sweep_interval_s=60,
    )


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(30, 40, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


def _track_durability_calls(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    real_fsync = durable_files.os.fsync
    real_replace = durable_files.os.replace

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append(
            "directory_fsync" if stat.S_ISDIR(mode) else "file_fsync"
        )
        real_fsync(fd)

    def tracking_replace(source: Path, target: Path) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(durable_files.os, "fsync", tracking_fsync)
    monkeypatch.setattr(durable_files.os, "replace", tracking_replace)


def test_sqlite_connections_use_full_synchronous_for_terminal_durability(
    tmp_path: Path,
) -> None:
    runtime = create_runtime(_settings(tmp_path))
    asyncio.run(runtime.repository.initialize())

    conn = runtime.repository._open()  # noqa: SLF001
    try:
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
    finally:
        conn.close()

    assert synchronous == 2


@pytest.mark.asyncio
async def test_reference_commit_waits_for_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    await repository.initialize()
    events: list[str] = []
    _track_durability_calls(monkeypatch, events)

    class TrackingRepository:
        def _one_sync(self, sql: str, params: tuple[object, ...]) -> Any:
            return repository._one_sync(sql, params)  # noqa: SLF001

        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            changed = repository._execute_sync(sql, params)  # noqa: SLF001
            if sql.lstrip().startswith("INSERT OR IGNORE INTO refs"):
                events.append("refs_commit")
            return changed

    store = FilesystemArtifactStore(settings, TrackingRepository())
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()

    assert store._write("owner", sha, "token", "png", data) is True  # noqa: SLF001
    assert events == [
        "file_fsync",
        "replace",
        "directory_fsync",
        "refs_commit",
    ]


@pytest.mark.asyncio
async def test_reference_recursive_mkdir_is_durable_before_row_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    await repository.initialize()
    shutil.rmtree(settings.data_dir)
    events: list[str] = []
    real_sync = durable_files.fsync_directory

    def tracking_sync(directory: Path) -> None:
        events.append(f"sync:{directory.relative_to(tmp_path)}")
        real_sync(directory)

    class TrackingRepository:
        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            changed = repository._execute_sync(sql, params)  # noqa: SLF001
            if sql.lstrip().startswith("INSERT OR IGNORE INTO refs"):
                events.append("refs_commit")
            return changed

    monkeypatch.setattr(durable_files, "fsync_directory", tracking_sync)
    store = FilesystemArtifactStore(settings, TrackingRepository())
    data = _png_bytes()

    inserted = store._write(  # noqa: SLF001
        "owner",
        hashlib.sha256(data).hexdigest(),
        "token",
        "png",
        data,
    )

    assert inserted is True
    assert events == [
        "sync:.",
        "sync:data",
        "sync:data/refs",
        "refs_commit",
    ]


@pytest.mark.asyncio
async def test_reference_rollback_unlink_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    await repository.initialize()
    events: list[str] = []
    _track_durability_calls(monkeypatch, events)

    class FailingRepository:
        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            _ = params
            if sql.lstrip().startswith("INSERT OR IGNORE INTO refs"):
                events.append("refs_failure")
                raise OSError("injected refs commit failure")
            return repository._execute_sync(sql, params)  # noqa: SLF001

    store = FilesystemArtifactStore(settings, FailingRepository())
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    target = settings.refs_dir / "token.png"

    with pytest.raises(OSError, match="injected refs commit failure"):
        store._write("owner", sha, "token", "png", data)  # noqa: SLF001

    assert not target.exists()
    assert events == [
        "file_fsync",
        "replace",
        "directory_fsync",
        "refs_failure",
        "directory_fsync",
    ]


@pytest.mark.asyncio
async def test_job_success_commit_waits_for_artifact_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    runtime = create_runtime(settings)
    await runtime.repository.initialize()
    await runtime.jobs.persistence.insert_job(
        "job-durable",
        {
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "cat"},
            "retention_days": 1,
        },
        "Bearer sk-test",
    )
    events: list[str] = []
    _track_durability_calls(monkeypatch, events)
    data = _png_bytes()

    async def saving_call(
        row: Any,
        *,
        authorization: str,
    ) -> tuple[int, list[dict[str, Any]]]:
        assert authorization == "Bearer sk-test"
        images = await runtime.upstream.processing.save_images(
            row["job_id"],
            row["created_at"],
            row["retention_days"],
            [ImageCandidate(data, "image/png")],
        )
        return 200, images

    runtime.jobs.upstream.call = saving_call
    persistence = runtime.jobs.persistence

    class TrackingPersistence:
        async def mark_succeeded(self, *args: Any, **kwargs: Any) -> bool:
            events.append("success_commit_started")
            changed = await persistence.mark_succeeded(*args, **kwargs)
            events.append("success_committed")
            return changed

        def __getattr__(self, name: str) -> Any:
            return getattr(persistence, name)

    runtime.jobs.persistence = TrackingPersistence()

    await runtime.jobs.process("job-durable")

    row = await runtime.repository.one(
        "SELECT status, images_json FROM jobs WHERE job_id = ?",
        ("job-durable",),
    )
    assert row is not None
    assert row["status"] == "succeeded"
    assert events == [
        "directory_fsync",
        "directory_fsync",
        "directory_fsync",
        "directory_fsync",
        "directory_fsync",
        "directory_fsync",
        "file_fsync",
        "replace",
        "directory_fsync",
        "success_commit_started",
        "success_committed",
    ]
