from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from image_job.adapters import filesystem_artifacts as filesystem_artifacts_module
from image_job.persistence import RetentionFacade
from image_job.persistence_parts import (
    retention_references as reference_retention,
)
from image_job.adapters.filesystem_artifacts import FilesystemArtifactStore
from image_job.adapters.sqlite_jobs import SQLiteJobRepository
from image_job.config import ImageJobSettings, ImageJobTimeouts, SecretText


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


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(30, 40, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _store(
    tmp_path: Path,
) -> tuple[FilesystemArtifactStore, SQLiteJobRepository, ImageJobSettings]:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    await repository.initialize()
    return FilesystemArtifactStore(settings, repository), repository, settings


@pytest.mark.asyncio
async def test_initialize_normalizes_legacy_reference_timestamp(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.state_dir.mkdir(parents=True)
    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    try:
        conn.executescript(
            """
            CREATE TABLE refs (
                auth_hash TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                token TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(auth_hash, sha256)
            );
            INSERT INTO refs(
                auth_hash, sha256, token, ext, size, created_at
            ) VALUES (
                'owner', 'sha', 'legacy-token', 'png', 1,
                '2026-08-01 23:59:59'
            );
            """
        )
    finally:
        conn.close()
    repository = SQLiteJobRepository(settings)

    await repository.initialize()

    row = repository._one_sync(  # noqa: SLF001
        "SELECT created_at FROM refs WHERE token = ?",
        ("legacy-token",),
    )
    assert row is not None
    assert row["created_at"] == "2026-08-01T23:59:59.000Z"


def _insert_ref(
    repository: SQLiteJobRepository,
    *,
    owner_hash: str,
    sha: str,
    token: str,
    ext: str = "png",
    size: int = 1,
) -> None:
    repository._execute_sync(  # noqa: SLF001
        """
        INSERT INTO refs(auth_hash, sha256, token, ext, size, created_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (owner_hash, sha, token, ext, size),
    )


def _row(
    repository: SQLiteJobRepository,
    owner_hash: str,
    sha: str,
) -> Any:
    return repository._one_sync(  # noqa: SLF001
        """
        SELECT token, ext, size, created_at
        FROM refs
        WHERE auth_hash = ? AND sha256 = ?
        """,
        (owner_hash, sha),
    )


def _retention(
    repository: SQLiteJobRepository,
    settings: ImageJobSettings,
) -> RetentionFacade:
    return RetentionFacade(
        data_dir=lambda: settings.data_dir,
        refs_dir=lambda: settings.refs_dir,
        db_exec_sync=lambda sql, params: repository._execute_sync(  # noqa: SLF001
            sql,
            params,
        ),
        db_exec=lambda sql, params=(): repository.execute(sql, params),
        db_all=lambda sql, params=(): repository.all(sql, params),
        utc_now=lambda: datetime.now(timezone.utc),
        max_retention_days=lambda: settings.max_retention_days,
        job_ttl_days=lambda: settings.job_ttl_days,
        log=logging.getLogger(__name__),
        open_db=repository._open,  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_identical_upload_repairs_row_with_missing_backing_file(
    tmp_path: Path,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="stale-token",
    )

    repaired = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )
    repeated = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )

    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] != "stale-token"
    assert repaired["deduped"] is False
    assert repeated["deduped"] is True
    assert repaired["url"] == repeated["url"]
    assert settings.refs_dir.joinpath(f"{row['token']}.{row['ext']}").read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["symlink", "directory"])
async def test_identical_upload_rejects_symlink_and_non_regular_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    stale_target = settings.refs_dir / "stale-token.png"
    if target_kind == "symlink":
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        stale_target.symlink_to(outside)
    else:
        stale_target.mkdir()
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="stale-token",
    )

    repaired = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )

    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] != "stale-token"
    assert repaired["url"] != (
        "https://images.example.test/refs/stale-token.png"
    )
    assert settings.refs_dir.joinpath(f"{row['token']}.{row['ext']}").read_bytes() == data
    if target_kind == "symlink":
        assert stale_target.is_symlink()
        assert outside.read_bytes() == b"outside"
    else:
        assert stale_target.is_dir()


@pytest.mark.asyncio
async def test_identical_upload_rejects_unsafe_reference_path(
    tmp_path: Path,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    escaped_target = settings.refs_dir.parent / "escaped.png"
    escaped_target.write_bytes(b"outside")
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="../escaped",
    )

    repaired = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )

    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] != "../escaped"
    assert repaired["url"] != "https://images.example.test/refs/../escaped.png"
    assert escaped_target.read_bytes() == b"outside"
    assert settings.refs_dir.joinpath(f"{row['token']}.{row['ext']}").read_bytes() == data


@pytest.mark.asyncio
async def test_concurrent_identical_uploads_repair_once_and_keep_deduping(
    tmp_path: Path,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="stale-token",
    )

    results = await asyncio.gather(
        *(
            store.put_reference(
                owner_hash="owner",
                content_type="image/png",
                data=data,
            )
            for _ in range(8)
        )
    )

    assert len({result["url"] for result in results}) == 1
    assert sum(result["deduped"] is False for result in results) == 1
    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] != "stale-token"
    assert settings.refs_dir.joinpath(f"{row['token']}.{row['ext']}").read_bytes() == data


@pytest.mark.asyncio
async def test_concurrent_identical_uploads_remove_insert_loser_files(
    tmp_path: Path,
) -> None:
    _, repository, settings = await _store(tmp_path)
    barrier = threading.Barrier(8)

    class CoordinatedRepository:
        def _one_sync(self, sql: str, params: tuple[object, ...]) -> Any:
            row = repository._one_sync(sql, params)  # noqa: SLF001
            if row is None:
                barrier.wait(timeout=5)
            return row

        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            return repository._execute_sync(sql, params)  # noqa: SLF001

    store = FilesystemArtifactStore(settings, CoordinatedRepository())
    data = _png_bytes()
    results = await asyncio.gather(
        *(
            store.put_reference(
                owner_hash="owner",
                content_type="image/png",
                data=data,
            )
            for _ in range(8)
        )
    )

    files = [path for path in settings.refs_dir.iterdir() if path.is_file()]
    assert len({result["url"] for result in results}) == 1
    assert sum(result["deduped"] is False for result in results) == 1
    assert len(files) == 1
    assert files[0].read_bytes() == data


@pytest.mark.asyncio
async def test_database_failure_after_publish_removes_target_and_syncs_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, settings = await _store(tmp_path)
    fsynced_directory = False
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        nonlocal fsynced_directory
        fsynced_directory = fsynced_directory or stat.S_ISDIR(os.fstat(fd).st_mode)
        real_fsync(fd)

    class FailingRepository:
        def _one_sync(self, sql: str, params: tuple[object, ...]) -> Any:
            return repository._one_sync(sql, params)  # noqa: SLF001

        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            if sql.lstrip().startswith("INSERT OR IGNORE INTO refs"):
                raise sqlite3.OperationalError("injected insert failure")
            return repository._execute_sync(sql, params)  # noqa: SLF001

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    store = FilesystemArtifactStore(settings, FailingRepository())

    with pytest.raises(sqlite3.OperationalError, match="injected insert failure"):
        await store.put_reference(
            owner_hash="owner",
            content_type="image/png",
            data=_png_bytes(),
        )

    assert list(settings.refs_dir.iterdir()) == []
    assert fsynced_directory is True


@pytest.mark.asyncio
async def test_orphan_sweep_writer_claim_blocks_active_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, settings = await _store(tmp_path)
    orphan = settings.refs_dir / "old-orphan.png"
    orphan.write_bytes(b"old")
    os.utime(orphan, (1, 1))
    claimed = threading.Event()
    release = threading.Event()
    real_unlink = reference_retention.unlink_verified_entry

    def paused_unlink(directory: Any, entry: Any) -> None:
        assert entry.name == orphan.name
        claimed.set()
        assert release.wait(timeout=5)
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        paused_unlink,
    )
    sweep = asyncio.create_task(
        asyncio.to_thread(
            _retention(repository, settings).sweep_filesystem,
            2,
        )
    )
    assert await asyncio.to_thread(claimed.wait, 5)
    upload = asyncio.create_task(
        store.put_reference(
            owner_hash="owner",
            content_type="image/png",
            data=_png_bytes(),
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not upload.done()
        assert {path.name for path in settings.refs_dir.iterdir()} == {
            "old-orphan.png"
        }
    finally:
        release.set()

    removed = await sweep
    result = await upload

    assert removed == (1, len(b"old"))
    assert result["deduped"] is False
    filename = str(result["url"]).rsplit("/", 1)[-1]
    assert settings.refs_dir.joinpath(filename).read_bytes() == _png_bytes()
    row = repository._one_sync(  # noqa: SLF001
        "SELECT token FROM refs",
        (),
    )
    assert row is not None
    assert filename == f"{row['token']}.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["truncated", "same-size"])
async def test_identical_upload_repairs_corrupted_regular_file(
    tmp_path: Path,
    corruption: str,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    first = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )
    original = _row(repository, "owner", sha)
    assert original is not None
    original_path = settings.refs_dir / f"{original['token']}.{original['ext']}"
    if corruption == "truncated":
        original_path.write_bytes(data[:-1])
    else:
        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        original_path.write_bytes(corrupted)

    repaired = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )
    repeated = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )

    row = _row(repository, "owner", sha)
    assert row is not None
    repaired_path = settings.refs_dir / f"{row['token']}.{row['ext']}"
    assert row["token"] != original["token"]
    assert int(row["size"]) == len(data)
    assert first["url"] != repaired["url"]
    assert repaired["deduped"] is False
    assert repeated["deduped"] is True
    assert repaired["url"] == repeated["url"]
    assert repaired_path.read_bytes() == data
    assert hashlib.sha256(repaired_path.read_bytes()).hexdigest() == sha


def test_existing_root_swap_after_row_read_keeps_reference_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    asyncio.run(repository.initialize())
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="stale-token",
        size=len(data),
    )
    original_target = settings.refs_dir / "stale-token.png"
    original_target.write_bytes(data)
    displaced_root = settings.refs_dir.with_name("refs-displaced")
    live_root = settings.refs_dir.with_name("refs-live")
    live_root.mkdir()
    live_target = live_root / original_target.name
    live_target.write_bytes(b"live-replacement")
    real_validate = FilesystemArtifactStore._is_valid_reference_at  # noqa: SLF001
    swapped = False

    def swap_after_row_read(
        self: FilesystemArtifactStore,
        refs: Any,
        token: str,
        ext: str,
        expected_size: int,
        expected_sha: str,
    ) -> bool:
        nonlocal swapped
        if not swapped:
            settings.refs_dir.rename(displaced_root)
            settings.refs_dir.symlink_to(
                live_root,
                target_is_directory=True,
            )
            swapped = True
        return real_validate(
            self,
            refs,
            token,
            ext,
            expected_size,
            expected_sha,
        )

    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_is_valid_reference_at",
        swap_after_row_read,
    )
    store = FilesystemArtifactStore(settings, repository)

    assert store._existing("owner", sha) is None  # noqa: SLF001
    assert swapped
    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] == "stale-token"
    assert displaced_root.joinpath(original_target.name).read_bytes() == data
    assert live_target.read_bytes() == b"live-replacement"


def test_existing_root_swap_after_delete_rolls_back_reference_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings)
    asyncio.run(repository.initialize())
    sha = hashlib.sha256(b"missing-reference").hexdigest()
    _insert_ref(
        repository,
        owner_hash="owner",
        sha=sha,
        token="stale-token",
        size=len(b"missing-reference"),
    )
    displaced_root = settings.refs_dir.with_name("refs-displaced")
    live_root = settings.refs_dir.with_name("refs-live")
    live_root.mkdir()
    live_target = live_root / "stale-token.png"
    live_target.write_bytes(b"live-replacement")
    real_match = filesystem_artifacts_module.directory_path_matches
    checks = 0

    def swap_before_commit(guard: Any) -> bool:
        nonlocal checks
        checks += 1
        if checks == 3:
            settings.refs_dir.rename(displaced_root)
            settings.refs_dir.symlink_to(
                live_root,
                target_is_directory=True,
            )
        return real_match(guard)

    monkeypatch.setattr(
        filesystem_artifacts_module,
        "directory_path_matches",
        swap_before_commit,
    )
    store = FilesystemArtifactStore(settings, repository)

    assert store._existing("owner", sha) is None  # noqa: SLF001
    assert checks >= 3
    row = _row(repository, "owner", sha)
    assert row is not None
    assert row["token"] == "stale-token"
    assert live_target.read_bytes() == b"live-replacement"


@pytest.mark.asyncio
async def test_dedupe_renewal_fences_concurrent_retention(
    tmp_path: Path,
) -> None:
    _, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    initial_store = FilesystemArtifactStore(settings, repository)
    initial = await initial_store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )
    repository._execute_sync(  # noqa: SLF001
        """
        UPDATE refs
        SET created_at = '2000-01-01T00:00:00+00:00'
        WHERE auth_hash = ? AND sha256 = ?
        """,
        ("owner", sha),
    )
    renewed = threading.Event()
    release = threading.Event()

    class CoordinatedRepository:
        def _one_sync(self, sql: str, params: tuple[object, ...]) -> Any:
            return repository._one_sync(sql, params)  # noqa: SLF001

        def _execute_sync(self, sql: str, params: tuple[object, ...]) -> int:
            changed = repository._execute_sync(sql, params)  # noqa: SLF001
            if "UPDATE refs" in sql and "SET created_at = ?" in sql:
                renewed.set()
                assert release.wait(timeout=5)
            return changed

    store = FilesystemArtifactStore(settings, CoordinatedRepository())
    upload = asyncio.create_task(
        store.put_reference(
            owner_hash="owner",
            content_type="image/png",
            data=data,
        )
    )
    assert await asyncio.to_thread(renewed.wait, 5)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    try:
        removed = await asyncio.to_thread(
            _retention(repository, settings).sweep_filesystem,
            cutoff,
        )
    finally:
        release.set()
    result = await upload

    assert removed == (0, 0)
    assert result["deduped"] is True
    assert result["url"] == initial["url"]
    filename = str(result["url"]).rsplit("/", 1)[-1]
    assert settings.refs_dir.joinpath(filename).read_bytes() == data


@pytest.mark.asyncio
async def test_retention_claim_forces_concurrent_dedupe_to_republish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, settings = await _store(tmp_path)
    data = _png_bytes()
    sha = hashlib.sha256(data).hexdigest()
    initial = await store.put_reference(
        owner_hash="owner",
        content_type="image/png",
        data=data,
    )
    repository._execute_sync(  # noqa: SLF001
        """
        UPDATE refs
        SET created_at = '2000-01-01T00:00:00+00:00'
        WHERE auth_hash = ? AND sha256 = ?
        """,
        ("owner", sha),
    )
    claimed = threading.Event()
    release = threading.Event()
    real_unlink = reference_retention.unlink_verified_entry

    def paused_unlink(directory: Any, entry: Any) -> None:
        claimed.set()
        assert release.wait(timeout=5)
        real_unlink(directory, entry)

    monkeypatch.setattr(
        reference_retention,
        "unlink_verified_entry",
        paused_unlink,
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    retention = asyncio.create_task(
        asyncio.to_thread(
            _retention(repository, settings).sweep_filesystem,
            cutoff,
        )
    )
    assert await asyncio.to_thread(claimed.wait, 5)
    upload = asyncio.create_task(
        store.put_reference(
            owner_hash="owner",
            content_type="image/png",
            data=data,
        )
    )
    await asyncio.sleep(0.05)
    assert not upload.done()
    release.set()

    removed = await retention
    result = await upload

    assert removed == (1, len(data))
    assert result["deduped"] is False
    assert result["url"] != initial["url"]
    filename = str(result["url"]).rsplit("/", 1)[-1]
    assert settings.refs_dir.joinpath(filename).read_bytes() == data
