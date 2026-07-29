from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from image_job.application.auth import credential_hash
from image_job.config import ImageJobSettings, ImageJobTimeouts, SecretText
from image_job.credential_vault import (
    CredentialVault,
    CredentialVaultConfigError,
    CredentialVaultError,
)
from image_job.runtime import create_runtime


def _settings(
    tmp_path: Path,
    *,
    master_secret: str = "test-master-secret-" + "x" * 32,
    key_id: str = "test-v1",
    journal_mode: str = "WAL",
) -> ImageJobSettings:
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    return ImageJobSettings(
        data_dir=data_dir,
        refs_dir=data_dir / "refs",
        state_dir=state_dir,
        db_path=state_dir / "jobs.sqlite3",
        queue_max=4,
        concurrency=1,
        sidecar_token=SecretText("s" * 32),
        allow_legacy_bearer=False,
        upstream_base_url="http://127.0.0.1:8081",
        public_base_url="https://images.example.test",
        timeouts=ImageJobTimeouts(graceful_shutdown_s=0),
        credential_active_key_id=key_id,
        credential_master_secret=SecretText(master_secret),
        sqlite_journal_mode=journal_mode,
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


def _assert_files_exclude(paths: list[Path], secrets: list[str]) -> None:
    for path in paths:
        if not path.exists():
            continue
        raw = path.read_bytes()
        for secret in secrets:
            assert secret.encode("utf-8") not in raw, path


def test_credential_vault_rejects_wrong_key_and_aad() -> None:
    vault = CredentialVault(
        active_key_id="v1",
        master_secret="master-secret-a-" + "a" * 32,
    )
    authorization = "Bearer sk-vault-wrong-key-aad"
    encrypted = vault.encrypt(
        authorization,
        job_id="job-a",
        owner_hash="owner-a",
    )

    assert (
        vault.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_id=encrypted.key_id,
            job_id="job-a",
            owner_hash="owner-a",
        )
        == authorization
    )
    assert authorization.encode() not in encrypted.ciphertext

    wrong_secret = CredentialVault(
        active_key_id="v1",
        master_secret="master-secret-b-" + "b" * 32,
    )
    with pytest.raises(CredentialVaultError):
        wrong_secret.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_id=encrypted.key_id,
            job_id="job-a",
            owner_hash="owner-a",
        )
    with pytest.raises(CredentialVaultError):
        vault.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_id=encrypted.key_id,
            job_id="job-b",
            owner_hash="owner-a",
        )
    with pytest.raises(CredentialVaultError):
        vault.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_id=encrypted.key_id,
            job_id="job-a",
            owner_hash="owner-b",
        )
    with pytest.raises(CredentialVaultError):
        vault.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_id="v2",
            job_id="job-a",
            owner_hash="owner-a",
        )


@pytest.mark.parametrize(
    ("key_id", "secret"),
    [
        ("", "x" * 32),
        ("v1", ""),
        ("v1", "short"),
    ],
)
def test_missing_active_credential_key_fails_closed(
    tmp_path: Path,
    key_id: str,
    secret: str,
) -> None:
    settings = _settings(tmp_path, key_id=key_id, master_secret=secret)

    with pytest.raises(CredentialVaultConfigError):
        settings.validate()
    with pytest.raises(CredentialVaultConfigError):
        create_runtime(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_mode", ["WAL", "PERSIST"])
async def test_legacy_rows_migrate_and_clear_plaintext_storage(
    tmp_path: Path,
    journal_mode: str,
) -> None:
    settings = _settings(tmp_path, journal_mode=journal_mode)
    settings.db_path.parent.mkdir(parents=True)
    legacy = {
        "legacy-queued": ("queued", "Bearer sk-legacy-queued-unique"),
        "legacy-running": ("running", "Bearer sk-legacy-running-unique"),
        "legacy-succeeded": ("succeeded", "Bearer sk-legacy-succeeded-unique"),
        "legacy-cancelled": ("cancelled", "Bearer sk-legacy-cancelled-unique"),
    }
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                auth_hash TEXT NOT NULL,
                auth_header TEXT,
                request_type TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                elapsed_ms INTEGER,
                upstream_status INTEGER,
                image_count INTEGER NOT NULL DEFAULT 0,
                images_json TEXT,
                error TEXT,
                upstream_body TEXT
            );
            """
        )
        for job_id, (status, authorization) in legacy.items():
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, auth_hash, auth_header, request_type, endpoint,
                    payload_json, status, relay_url, retention_days,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, 'generations', '/v1/images/generations',
                          '{}', ?, 'http://upstream', 1, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    f"owner-{job_id}",
                    authorization,
                    status,
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-01T00:00:00+00:00",
                    ("2026-07-01T00:01:00+00:00" if status == "running" else None),
                    (
                        "2026-07-01T00:02:00+00:00"
                        if status not in {"queued", "running"}
                        else None
                    ),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    runtime = create_runtime(settings)
    await runtime.repository.initialize()
    rows = {
        row["job_id"]: row for row in await runtime.repository.all("SELECT * FROM jobs")
    }

    for job_id in ("legacy-queued", "legacy-running"):
        row = rows[job_id]
        assert row["auth_header"] is None
        assert row["auth_ciphertext"] is not None
        assert row["auth_nonce"] is not None
        assert row["auth_key_id"] == "test-v1"
        assert runtime.credential_vault.decrypt_job_row(row) == legacy[job_id][1]
        assert row["upstream_auth_hash"] == credential_hash(legacy[job_id][1])

    for job_id in ("legacy-succeeded", "legacy-cancelled"):
        row = rows[job_id]
        assert row["auth_header"] is None
        assert row["auth_ciphertext"] is None
        assert row["auth_nonce"] is None
        assert row["auth_key_id"] is None

    backup_path = tmp_path / "legacy-backup.sqlite3"
    source = sqlite3.connect(settings.db_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    _assert_files_exclude(
        [
            settings.db_path,
            settings.db_path.with_name(settings.db_path.name + "-wal"),
            settings.db_path.with_name(settings.db_path.name + "-shm"),
            settings.db_path.with_name(settings.db_path.name + "-journal"),
            backup_path,
        ],
        [authorization for _status, authorization in legacy.values()],
    )


@pytest.mark.asyncio
async def test_queued_and_running_wal_and_backup_never_store_plaintext(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runtime = create_runtime(settings)
    await runtime.repository.initialize()

    reader = sqlite3.connect(settings.db_path)
    reader.execute("PRAGMA journal_mode = WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
    queued_secret = "Bearer sk-queued-wal-plaintext-sentinel"
    running_secret = "Bearer sk-running-wal-plaintext-sentinel"
    try:
        await runtime.jobs.persistence.insert_job(
            "job-queued-wal",
            _payload(),
            queued_secret,
            owner_auth_header="Bearer owner-queued",
        )
        await runtime.jobs.persistence.insert_job(
            "job-running-wal",
            _payload(),
            running_secret,
            owner_auth_header="Bearer owner-running",
        )
        assert await runtime.jobs.persistence.mark_running("job-running-wal")

        rows = await runtime.repository.all(
            """
            SELECT *
            FROM jobs
            WHERE job_id IN ('job-queued-wal', 'job-running-wal')
            ORDER BY job_id
            """
        )
        assert [row["status"] for row in rows] == ["queued", "running"]
        for row, authorization in zip(
            rows,
            (queued_secret, running_secret),
            strict=True,
        ):
            assert row["auth_header"] is None
            assert runtime.credential_vault.decrypt_job_row(row) == authorization

        wal_path = settings.db_path.with_name(settings.db_path.name + "-wal")
        assert wal_path.is_file()
        backup_path = tmp_path / "active-backup.sqlite3"
        source = sqlite3.connect(settings.db_path)
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()
        _assert_files_exclude(
            [
                settings.db_path,
                wal_path,
                settings.db_path.with_name(settings.db_path.name + "-shm"),
                backup_path,
            ],
            [queued_secret, running_secret],
        )
    finally:
        reader.rollback()
        reader.close()


@pytest.mark.asyncio
async def test_wrong_master_secret_rejects_existing_active_rows(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runtime = create_runtime(settings)
    await runtime.repository.initialize()
    await runtime.jobs.persistence.insert_job(
        "job-wrong-master",
        _payload(),
        "Bearer sk-wrong-master-sentinel",
    )

    wrong = replace(
        settings,
        credential_master_secret=SecretText("wrong-master-secret-" + "y" * 32),
    )
    wrong_runtime = create_runtime(wrong)
    with pytest.raises(CredentialVaultError):
        await wrong_runtime.repository.initialize()


@pytest.mark.asyncio
async def test_terminal_cancel_and_expire_clear_envelopes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = create_runtime(settings)
    await runtime.repository.initialize()

    await runtime.jobs.persistence.insert_job(
        "job-cancelled",
        _payload(),
        "Bearer sk-cancelled",
    )
    assert await runtime.jobs.persistence.mark_cancelled("job-cancelled")

    await runtime.jobs.persistence.insert_job(
        "job-failed",
        _payload(),
        "Bearer sk-failed",
    )
    failed_token = await runtime.jobs.persistence.mark_running("job-failed")
    assert failed_token
    await runtime.jobs.persistence.mark_failed(
        "job-failed",
        execution_token=failed_token,
        error="failed",
    )

    await runtime.jobs.persistence.insert_job(
        "job-succeeded",
        _payload(),
        "Bearer sk-succeeded",
    )
    succeeded_token = await runtime.jobs.persistence.mark_running("job-succeeded")
    assert succeeded_token
    await runtime.jobs.persistence.mark_succeeded(
        "job-succeeded",
        execution_token=succeeded_token,
        upstream_status=200,
        elapsed_ms=1,
        images=[],
    )

    await runtime.jobs.persistence.insert_job(
        "expired/blocked",
        _payload(),
        "Bearer sk-expired",
    )
    await runtime.repository.execute(
        """
        UPDATE jobs
            SET status = 'succeeded',
                finished_at = '2026-07-01T00:00:00+00:00',
                created_at = '2026-07-01T00:00:00+00:00',
                retention_expires_at = NULL
            WHERE job_id = 'expired/blocked'
            """
        )
    runtime.jobs.retention = replace(
        runtime.jobs.retention,
        utc_now=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    await runtime.jobs.retention.run_pass()

    rows = await runtime.repository.all(
        """
        SELECT job_id, auth_header, auth_ciphertext, auth_nonce, auth_key_id
        FROM jobs
        WHERE job_id IN (
            'job-cancelled',
            'job-failed',
            'job-succeeded',
            'expired/blocked'
        )
        """
    )
    assert {row["job_id"] for row in rows} == {
        "job-cancelled",
        "job-failed",
        "job-succeeded",
        "expired/blocked",
    }
    for row in rows:
        assert row["auth_header"] is None
        assert row["auth_ciphertext"] is None
        assert row["auth_nonce"] is None
        assert row["auth_key_id"] is None
