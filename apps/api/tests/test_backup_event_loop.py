"""Slow backup storage must not stop unrelated API requests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from app.routes import admin_backups


@pytest.mark.asyncio
async def test_backup_catalog_runs_outside_request_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    expected = admin_backups.BackupListOut(items=[], total=0)

    def slow_catalog(_root):
        assert threading.get_ident() != loop_thread
        return expected

    monkeypatch.setattr(admin_backups._backup_catalog, "list_backup_items", slow_catalog)
    assert await admin_backups.list_backups(SimpleNamespace()) is expected


@pytest.mark.asyncio
async def test_backup_receipt_lookup_runs_outside_request_event_loop(monkeypatch):
    loop_thread = threading.get_ident()

    def lookup(_root, operation_id, _started_at):
        assert threading.get_ident() != loop_thread
        assert operation_id == "backup-event-loop"
        return None

    monkeypatch.setattr(
        admin_backups._backup_catalog, "find_backup_pair_metadata_for_operation", lookup,
    )
    assert await admin_backups._find_paired_backup_for_operation(
        "backup-event-loop", datetime.now(timezone.utc),
    ) is None


@pytest.mark.asyncio
async def test_blocked_backup_storage_leaves_event_loop_responsive(monkeypatch):
    loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()

    def blocked_catalog(_root):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(timeout=5), "event loop did not release storage read"
        return admin_backups.BackupListOut(items=[], total=0)

    monkeypatch.setattr(admin_backups._backup_catalog, "list_backup_items", blocked_catalog)
    task = asyncio.create_task(admin_backups.list_backups(SimpleNamespace()))
    try:
        for _ in range(200):
            if started.is_set() or task.done():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        assert not task.done()
    finally:
        release.set()
        await task
