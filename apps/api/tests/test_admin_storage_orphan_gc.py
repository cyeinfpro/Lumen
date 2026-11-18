"""Tests for OPT-10 grace-period two-stage orphan media GC (worker + admin_storage)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.routes import admin_storage
from lumen_core.model_entities.media_workflows import Video
from lumen_core.models import (
    AuditLog,
    Base,
    Image,
    ImageVariant,
    ModelLibraryItem,
    PosterStyleItem,
    User,
)


ROOT = Path(__file__).resolve().parents[3]
ADMIN_ID = "00000000-0000-4000-8000-000000000099"
USER_ID = "00000000-0000-4000-8000-000000000101"


def _load_worker_orphan_gc() -> ModuleType:
    worker_root = ROOT / "apps" / "worker"
    added = False
    if str(worker_root) not in sys.path:
        sys.path.insert(0, str(worker_root))
        added = True
    try:
        path = worker_root / "app" / "tasks" / "storage_orphan_gc.py"
        spec = importlib.util.spec_from_file_location(
            "worker_storage_orphan_gc_test",
            path,
        )
        assert spec is not None and spec.loader is not None
        # Ensure relative imports inside apps/worker/app/tasks resolve cleanly
        pkg_spec = importlib.util.spec_from_file_location(
            "_worker_gc_pkg",
            worker_root / "app" / "__init__.py",
            submodule_search_locations=[str(worker_root / "app")],
        )
        assert pkg_spec is not None and pkg_spec.loader is not None
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules["_worker_gc_pkg"] = pkg
        pkg_spec.loader.exec_module(pkg)

        tasks_spec = importlib.util.spec_from_file_location(
            "_worker_gc_pkg.tasks",
            worker_root / "app" / "tasks" / "__init__.py",
            submodule_search_locations=[str(worker_root / "app" / "tasks")],
        )
        assert tasks_spec is not None and tasks_spec.loader is not None
        tasks_pkg = importlib.util.module_from_spec(tasks_spec)
        sys.modules["_worker_gc_pkg.tasks"] = tasks_pkg

        mod_spec = importlib.util.spec_from_file_location(
            "_worker_gc_pkg.tasks.storage_orphan_gc",
            path,
        )
        assert mod_spec is not None and mod_spec.loader is not None
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = module
        mod_spec.loader.exec_module(module)
        return module
    finally:
        if added and str(worker_root) in sys.path:
            sys.path.remove(str(worker_root))


def _request(method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/admin/storage/orphan-gc",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 5900),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


def _write_aged_file(storage_root: Path, rel_key: str, data: bytes, *, mtime: float) -> Path:
    full = storage_root / rel_key
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    os.utime(full, (mtime, mtime))
    return full


@pytest_asyncio.fixture
async def gc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setattr(admin_storage.settings, "storage_root", str(storage_root))

    test_meta = sa.MetaData()
    for tbl in Base.metadata.tables.values():
        cloned = tbl.to_metadata(test_meta)
        for col in cloned.columns:
            if col.server_default is not None and "ARRAY[" in str(
                getattr(col.server_default, "arg", "")
            ):
                col.server_default = None

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orphan_gc.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(test_meta.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(id=ADMIN_ID, email="gc-admin@example.test", role="admin"),
                User(id=USER_ID, email="creator@example.test", role="user"),
            ]
        )
        await session.commit()
    try:
        yield factory, storage_root
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_orphan_gc_protects_five_tables_and_enforces_48h_and_7d_stages(
    gc_env,
) -> None:
    factory, storage_root = gc_env
    worker_gc = _load_worker_orphan_gc()
    now_ts = 2_000_000_000.0
    ten_days_ago = now_ts - 10 * 86400.0
    ten_hours_ago = now_ts - 10 * 3600.0

    # 1) Live image + variant
    img_key = f"u/{USER_ID}/uploads/live.png"
    var_key = f"u/{USER_ID}/uploads/live.thumb.webp"
    # 2) Live video
    vid_key = f"u/{USER_ID}/v/gen1/out.mp4"
    # 3) Soft-deleted image STILL referenced by ModelLibraryItem -> must NOT be quarantined!
    model_lib_key = f"u/{USER_ID}/uploads/model-fav.png"
    # 4) Soft-deleted image STILL referenced by PosterStyleItem -> must NOT be quarantined!
    poster_lib_key = f"u/{USER_ID}/uploads/poster-style.png"
    # 5) Young unreferenced file (10h < 48h grace period) -> must be skipped!
    young_key = f"u/{USER_ID}/uploads/young-inflight.png"
    # 6) True mature orphan (10d old, unreferenced anywhere) -> quarantined first, purged only after 7d!
    orphan_key = f"u/{USER_ID}/uploads/true-orphan.png"

    for key in (img_key, var_key, vid_key, model_lib_key, poster_lib_key, orphan_key):
        _write_aged_file(storage_root, key, b"payload-bytes", mtime=ten_days_ago)
    _write_aged_file(storage_root, young_key, b"young-bytes", mtime=ten_hours_ago)

    from datetime import datetime, timezone

    deleted_dt = datetime.fromtimestamp(ten_days_ago, tz=timezone.utc)
    async with factory() as session:
        session.add_all(
            [
                Image(
                    id="img-live",
                    user_id=USER_ID,
                    source="uploaded",
                    storage_key=img_key,
                    mime="image/png",
                    width=64,
                    height=64,
                    size_bytes=13,
                    sha256="a" * 64,
                ),
                ImageVariant(
                    id="var-live",
                    image_id="img-live",
                    kind="thumb256",
                    storage_key=var_key,
                    width=32,
                    height=32,
                ),
                Video(
                    id="vid-live",
                    user_id=USER_ID,
                    storage_key=vid_key,
                    mime="video/mp4",
                    width=64,
                    height=64,
                    duration_ms=1000,
                    size_bytes=13,
                    sha256="b" * 64,
                    etag="etag-1",
                ),
                Image(
                    id="img-model-lib",
                    user_id=USER_ID,
                    source="uploaded",
                    storage_key=model_lib_key,
                    mime="image/png",
                    width=64,
                    height=64,
                    size_bytes=13,
                    sha256="c" * 64,
                    deleted_at=deleted_dt,
                ),
                ModelLibraryItem(
                    id="user:model-1",
                    user_id=USER_ID,
                    source="uploaded",
                    image_id="img-model-lib",
                    title="Favorite Model",
                ),
                Image(
                    id="img-poster-lib",
                    user_id=USER_ID,
                    source="uploaded",
                    storage_key=poster_lib_key,
                    mime="image/png",
                    width=64,
                    height=64,
                    size_bytes=13,
                    sha256="d" * 64,
                    deleted_at=deleted_dt,
                ),
                PosterStyleItem(
                    id="style:1",
                    user_id=USER_ID,
                    source="uploaded",
                    cover_image_id="img-poster-lib",
                    sample_image_ids=[],
                    title="Editorial Poster",
                ),
            ]
        )
        await session.commit()

    # Step A: dry_run=True -> detects exactly 1 orphan, moves/deletes nothing
    dry = await worker_gc.run_storage_orphan_gc(
        dry_run=True,
        storage_root=storage_root,
        session_factory=factory,
        now=now_ts,
    )
    assert dry["dry_run"] is True
    assert dry["young_skipped"] == 1
    assert dry["orphan_keys"] == [orphan_key]
    assert dry["quarantined_count"] == 0
    assert (storage_root / orphan_key).is_file()

    # Step B: dry_run=False at t=now_ts -> moves orphan into .quarantine/, MUST NOT delete immediately
    # even though original mtime was 10 days old!
    stage1 = await worker_gc.run_storage_orphan_gc(
        dry_run=False,
        storage_root=storage_root,
        session_factory=factory,
        now=now_ts,
    )
    assert stage1["quarantined_keys"] == [orphan_key]
    assert stage1["purged_count"] == 0
    assert not (storage_root / orphan_key).exists()
    quarantined_file = storage_root / ".quarantine" / orphan_key
    assert quarantined_file.is_file()
    for kept in (img_key, var_key, vid_key, model_lib_key, poster_lib_key, young_key):
        assert (storage_root / kept).is_file(), kept

    # Step C: 6 days later (< 7d retention) -> still retained in .quarantine/
    day6 = await worker_gc.run_storage_orphan_gc(
        dry_run=False,
        storage_root=storage_root,
        session_factory=factory,
        now=now_ts + 6 * 86400.0,
    )
    assert day6["purged_count"] == 0
    assert quarantined_file.is_file()

    # Step D: 7 days + 1s later -> physically purged!
    day7 = await worker_gc.run_storage_orphan_gc(
        dry_run=False,
        storage_root=storage_root,
        session_factory=factory,
        now=now_ts + 7 * 86400.0 + 1.0,
    )
    assert day7["purged_keys"] == [orphan_key]
    assert not quarantined_file.exists()


@pytest.mark.asyncio
async def test_admin_storage_orphan_gc_endpoints_inspect_quarantine_and_audit(
    gc_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, storage_root = gc_env
    base_now = 2_100_000_000.0
    monkeypatch.setattr(admin_storage.time, "time", lambda: base_now)

    orphan_key = f"u/{USER_ID}/uploads/admin-orphan.jpg"
    _write_aged_file(storage_root, orphan_key, b"orphan-jpg", mtime=base_now - 72 * 3600.0)
    admin = SimpleNamespace(id=ADMIN_ID, email="gc-admin@example.test")

    async with factory() as session:
        inspected = await admin_storage.inspect_storage_orphan_gc_endpoint(
            admin,
            session,
            grace_period_hours=48.0,
            quarantine_days=7.0,
            max_files=100,
            max_entries=1000,
        )
    assert inspected["dry_run"] is True
    assert inspected["orphan_keys"] == [orphan_key]
    assert (storage_root / orphan_key).is_file()

    async with factory() as session:
        moved = await admin_storage.run_storage_orphan_gc_endpoint(
            _request("POST"),
            admin,
            session,
            dry_run=False,
            grace_period_hours=48.0,
            quarantine_days=7.0,
            max_files=100,
            max_entries=1000,
        )
    assert moved["quarantined_keys"] == [orphan_key]
    assert moved["purged_count"] == 0
    assert (storage_root / ".quarantine" / orphan_key).is_file()

    # Advance clock past 7 days and run purge
    monkeypatch.setattr(admin_storage.time, "time", lambda: base_now + 8 * 86400.0)
    async with factory() as session:
        purged = await admin_storage.run_storage_orphan_gc_endpoint(
            _request("POST"),
            admin,
            session,
            dry_run=False,
            grace_period_hours=48.0,
            quarantine_days=7.0,
            max_files=100,
            max_entries=1000,
        )
        audit_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "admin.storage.orphan_gc")
        )
    assert purged["purged_keys"] == [orphan_key]
    assert not (storage_root / ".quarantine" / orphan_key).exists()
    assert audit_count == 2
