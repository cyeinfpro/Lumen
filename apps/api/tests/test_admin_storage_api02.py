from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
import time
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.routes import admin_storage
from app.services import storage_apply_dispatch
from lumen_core.model_entities.storage_operations import StorageApplyOperation
from lumen_core.models import (
    AuditLog,
    Base,
    SystemSetting,
    User,
)
from lumen_core.schemas import StorageConfigUpdateIn


ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = ROOT / "apps/api/alembic/versions/0058_storage_apply_operations.py"
ADMIN_ID = "00000000-0000-4000-8000-000000000058"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/storage",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 5800),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


def _body(root: Path) -> StorageConfigUpdateIn:
    return StorageConfigUpdateIn(
        backend="local",
        local={"root": str(root)},
    )


def _patch_state_paths(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    state_dir.mkdir()
    monkeypatch.setattr(admin_storage, "STATE_DIR", state_dir)
    for attr, filename in (
        ("STATUS_FILE", "status.json"),
        ("LAST_APPLY_FILE", "last-apply.json"),
        ("TEST_TRIGGER", "test.trigger"),
        ("TEST_CONF", "test.conf"),
        ("LAST_TEST_FILE", "last-test.json"),
    ):
        monkeypatch.setattr(admin_storage, attr, state_dir / filename)
    monkeypatch.setattr(admin_storage, "APPLY_REQUESTS_DIR", state_dir / "requests")
    monkeypatch.setattr(admin_storage, "APPLY_RESULTS_DIR", state_dir / "results")
    monkeypatch.setattr(
        admin_storage,
        "APPLY_CLAIM_FILE",
        state_dir / "apply.claim.json",
    )


@pytest_asyncio.fixture
async def storage_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_dir = tmp_path / "storage-state"
    _patch_state_paths(monkeypatch, state_dir)
    monkeypatch.setenv("LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS", str(tmp_path))

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    SystemSetting.__table__,
                    AuditLog.__table__,
                    StorageApplyOperation.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage_apply_dispatch, "SessionLocal", factory)

    async with factory() as session:
        session.add(
            User(
                id=ADMIN_ID,
                email="storage-admin@example.test",
                role="admin",
            )
        )
        await session.commit()
    try:
        yield factory, state_dir
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_storage_commit_failure_writes_no_files_or_rows(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    wakeups: list[bool] = []
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: wakeups.append(True),
    )

    async with factory() as session:

        async def fail_commit() -> None:
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            await admin_storage.put_storage_endpoint(
                _body(tmp_path / "desired"),
                _request(),
                SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test"),
                session,
            )

    assert not (state_dir / "requests").exists()
    assert wakeups == []
    async with factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count()).select_from(StorageApplyOperation)
            )
            == 0
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.event_type == "admin.storage.update.requested")
            )
            == 0
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(SystemSetting)
                .where(SystemSetting.key.like("storage.%"))
            )
            == 0
        )


@pytest.mark.asyncio
async def test_second_desired_config_rolls_back_while_first_is_active(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    wakeups: list[bool] = []
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: wakeups.append(True),
    )
    admin = SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    async with factory() as session:
        response = await admin_storage.put_storage_endpoint(
            _body(first_root),
            _request(),
            admin,
            session,
        )
    assert response.status == "pending"
    assert response.config.last_apply is not None
    assert response.config.last_apply["call_id"] == response.call_id

    async with factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await admin_storage.put_storage_endpoint(
                _body(second_root),
                _request(),
                admin,
                session,
            )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "storage_operation_pending"
    assert wakeups == [True]
    assert not (state_dir / "requests").exists()

    async with factory() as session:
        operations = (
            (await session.execute(sa.select(StorageApplyOperation))).scalars().all()
        )
        assert len(operations) == 1
        assert operations[0].status == "pending"
        local_root = await session.scalar(
            sa.select(SystemSetting.value).where(
                SystemSetting.key == "storage.local.root"
            )
        )
        assert local_root == str(first_root)
        requested_audits = await session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "admin.storage.update.requested")
        )
        assert requested_audits == 1


@pytest.mark.asyncio
async def test_storage_crash_after_commit_replays_same_operation_once(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: True,
    )
    async with factory() as session:
        response = await admin_storage.put_storage_endpoint(
            _body(tmp_path / "desired"),
            _request(),
            SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test"),
            session,
        )

    def fail_before_request(
        _operation_id: str,
        _fence: int,
        _config_sha256: str,
        _conf_text: str,
    ) -> None:
        raise OSError("injected request staging failure")

    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=fail_before_request,
            read_host_result=lambda _operation_id, _fence: None,
        )
        == 0
    )
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.status == "pending"
        assert operation.dispatch_attempts == 1
        assert "injected request staging failure" in (operation.last_error or "")

    staged_identities: list[tuple[str, int]] = []

    def stage_once(
        operation_id: str,
        fence: int,
        config_sha256: str,
        conf_text: str,
    ) -> None:
        staged_identities.append((operation_id, fence))
        admin_storage._stage_storage_apply(
            operation_id,
            fence,
            config_sha256,
            conf_text,
        )

    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=stage_once,
            read_host_result=lambda _operation_id, _fence: None,
        )
        == 1
    )
    assert staged_identities == [(response.call_id, 2)]
    request_path = state_dir / "requests" / f"{response.call_id}.2.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["operation_id"] == response.call_id
    assert request["fence"] == 2
    assert (
        request["config_sha256"]
        == hashlib.sha256(request["config"].encode("utf-8")).hexdigest()
    )

    result_path = state_dir / "results" / f"{response.call_id}.2.json"
    result_path.parent.mkdir()
    result_path.write_text(
        json.dumps(
            {
                "call_id": response.call_id,
                "operation_id": response.call_id,
                "fence": 2,
                "status": "ok",
                "message": "applied mode=local",
                "started_at": 1_786_000_000,
                "finished_at": 1_786_000_010,
            }
        ),
        encoding="utf-8",
    )
    read_result = lambda operation_id, fence: admin_storage._read_json(  # noqa: E731
        state_dir / "results" / f"{operation_id}.{fence}.json"
    )
    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=stage_once,
            read_host_result=read_result,
        )
        == 1
    )
    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=stage_once,
            read_host_result=read_result,
        )
        == 0
    )
    assert staged_identities == [(response.call_id, 2)]
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.status == "succeeded"
        assert operation.active_slot is None
        assert operation.dispatch_attempts == 2
        assert operation.result_message == "applied mode=local"


@pytest.mark.asyncio
async def test_storage_cancel_before_trigger_replays_after_lease_expiry(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: True,
    )
    async with factory() as session:
        response = await admin_storage.put_storage_endpoint(
            _body(tmp_path / "desired"),
            _request(),
            SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test"),
            session,
        )

    current = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(storage_apply_dispatch, "_now", lambda: current)

    def cancel_before_request(
        _operation_id: str,
        _fence: int,
        _config_sha256: str,
        _conf_text: str,
    ) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await storage_apply_dispatch.dispatch_storage_apply_operation(
            response.call_id,
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=cancel_before_request,
        )
    assert not (state_dir / "requests").exists()
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.status == "pending"
        assert operation.dispatch_attempts == 1
        assert operation.dispatch_lease_until is not None

    current += timedelta(seconds=31)
    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=admin_storage._stage_storage_apply,
            read_host_result=lambda _operation_id, _fence: None,
        )
        == 1
    )
    request_path = state_dir / "requests" / f"{response.call_id}.2.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["operation_id"] == response.call_id
    assert request["fence"] == 2
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.status == "dispatched"
        assert operation.dispatch_attempts == 2


@pytest.mark.asyncio
async def test_storage_lease_failure_waits_for_slow_request_write(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: True,
    )
    async with factory() as session:
        response = await admin_storage.put_storage_endpoint(
            _body(tmp_path / "desired"),
            _request(),
            SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test"),
            session,
        )

    loop = asyncio.get_running_loop()
    stage_started = asyncio.Event()
    heartbeat_failed = asyncio.Event()
    finish_stage = Event()

    def slow_stage(
        operation_id: str,
        fence: int,
        config_sha256: str,
        conf_text: str,
    ) -> None:
        loop.call_soon_threadsafe(stage_started.set)
        assert finish_stage.wait(timeout=5)
        admin_storage._stage_storage_apply(
            operation_id,
            fence,
            config_sha256,
            conf_text,
        )

    async def fail_heartbeat(
        _claim: storage_apply_dispatch.StorageDispatchClaim,
    ) -> None:
        await stage_started.wait()
        heartbeat_failed.set()
        raise RuntimeError("injected storage dispatch lease loss")

    monkeypatch.setattr(
        storage_apply_dispatch,
        "_dispatch_heartbeat",
        fail_heartbeat,
    )
    dispatch_task = asyncio.create_task(
        storage_apply_dispatch.dispatch_storage_apply_operation(
            response.call_id,
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=slow_stage,
        )
    )
    await asyncio.wait_for(heartbeat_failed.wait(), timeout=2)
    await asyncio.sleep(0)
    assert not dispatch_task.done()
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.dispatch_owner is not None
        assert operation.dispatch_lease_until is not None

    finish_stage.set()
    with pytest.raises(
        RuntimeError,
        match="injected storage dispatch lease loss",
    ):
        await asyncio.wait_for(dispatch_task, timeout=2)

    request_path = state_dir / "requests" / f"{response.call_id}.1.json"
    assert request_path.is_file()
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.status == "pending"
        assert operation.dispatch_owner is None
        assert operation.dispatch_lease_until is None


@pytest.mark.asyncio
async def test_storage_database_restore_uses_host_fence_as_dispatch_floor(
    storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, state_dir = storage_factory
    monkeypatch.setattr(
        admin_storage,
        "wake_storage_apply_reconciler",
        lambda _request: True,
    )
    async with factory() as session:
        response = await admin_storage.put_storage_endpoint(
            _body(tmp_path / "desired"),
            _request(),
            SimpleNamespace(id=ADMIN_ID, email="storage-admin@example.test"),
            session,
        )

    host_operation_id = "f" * 32
    (state_dir / "apply.claim.json").write_text(
        json.dumps(
            {
                "operation_id": host_operation_id,
                "fence": 40,
                "claimed_at": 1_786_000_000,
            }
        ),
        encoding="utf-8",
    )
    results_dir = state_dir / "results"
    results_dir.mkdir()
    (results_dir / f"{host_operation_id}.43.json").write_text(
        json.dumps(
            {
                "operation_id": host_operation_id,
                "fence": 43,
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )

    assert (
        await storage_apply_dispatch.run_storage_apply_reconciler_once(
            load_conf_text=admin_storage._load_storage_conf_text,
            stage_operation=admin_storage._stage_storage_apply,
            read_host_result=lambda _operation_id, _fence: None,
            read_host_fence=admin_storage._read_storage_host_fence,
        )
        == 1
    )

    request_path = state_dir / "requests" / f"{response.call_id}.44.json"
    assert request_path.is_file()
    async with factory() as session:
        operation = await session.get(StorageApplyOperation, response.call_id)
        assert operation is not None
        assert operation.dispatch_fence == 44
        assert operation.status == "dispatched"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "storage_apply_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_apply_migration_contract() -> None:
    migration = _load_migration()
    assert migration.revision == "0058_storage_apply_operations"
    assert migration.down_revision == "0057_repair_concurrent_indexes"
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "uq_storage_apply_one_active" in source
    assert "pending or dispatched storage operations" in source


def _postgres_url() -> URL:
    raw = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is required")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return url.set(drivername="postgresql+psycopg2")


def _schema_url(url: URL, schema: str) -> URL:
    options = str(url.query.get("options", "")).strip()
    search_path = f"-csearch_path={schema},public"
    return url.update_query_dict(
        {"options": " ".join(value for value in (options, search_path) if value)}
    )


def _create_control_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id varchar(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            """
            CREATE TABLE system_settings (
                id varchar(36) PRIMARY KEY,
                key varchar(64) NOT NULL UNIQUE,
                value text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE audit_logs (
                id varchar(36) PRIMARY KEY,
                user_id varchar(36),
                event_type varchar(64) NOT NULL,
                details jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        migration = _load_migration()
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original


def test_storage_active_operation_conflict_rolls_back_postgres_transaction() -> None:
    base_url = _postgres_url()
    schema = f"storage_apply_{uuid4().hex}"
    admin_engine = sa.create_engine(base_url, poolclass=NullPool)
    schema_engine: Engine | None = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        schema_engine = sa.create_engine(
            _schema_url(base_url, schema),
            poolclass=NullPool,
        )
        _create_control_tables(schema_engine)

        first = schema_engine.connect()
        first_tx = first.begin()
        first.execute(
            sa.text("INSERT INTO users (id) VALUES (:id)"),
            {"id": ADMIN_ID},
        )
        first.execute(
            sa.text(
                """
                INSERT INTO system_settings (id, key, value)
                VALUES (:id, 'storage.backend', 'local')
                """
            ),
            {"id": uuid4().hex},
        )
        first.execute(
            sa.text(
                """
                INSERT INTO audit_logs (id, user_id, event_type)
                VALUES (:id, :user_id, 'admin.storage.update.requested')
                """
            ),
            {"id": uuid4().hex, "user_id": ADMIN_ID},
        )
        first.execute(
            sa.text(
                """
                INSERT INTO storage_apply_operations (
                    id, requested_by, desired_config_sha256
                ) VALUES (:id, :user_id, :digest)
                """
            ),
            {"id": uuid4().hex, "user_id": ADMIN_ID, "digest": "a" * 64},
        )

        contender_started = Event()

        def contender() -> None:
            with schema_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO system_settings (id, key, value)
                        VALUES (:id, 'storage.local.root', '/tmp/loser')
                        """
                    ),
                    {"id": uuid4().hex},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO audit_logs (id, user_id, event_type)
                        VALUES (:id, :user_id, 'admin.storage.update.loser')
                        """
                    ),
                    {"id": uuid4().hex, "user_id": ADMIN_ID},
                )
                contender_started.set()
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO storage_apply_operations (
                            id, requested_by, desired_config_sha256
                        ) VALUES (:id, :user_id, :digest)
                        """
                    ),
                    {
                        "id": uuid4().hex,
                        "user_id": ADMIN_ID,
                        "digest": "b" * 64,
                    },
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            deadline = time.monotonic() + 3
            while not contender_started.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert contender_started.is_set()
            first_tx.commit()
            first.close()
            with pytest.raises(IntegrityError):
                future.result(timeout=5)

        with schema_engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM storage_apply_operations")
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text(
                        """
                    SELECT count(*) FROM system_settings
                    WHERE key = 'storage.local.root'
                    """
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text(
                        """
                    SELECT count(*) FROM audit_logs
                    WHERE event_type = 'admin.storage.update.loser'
                    """
                    )
                )
                == 0
            )
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.schema.DropSchema(schema, cascade=True, if_exists=True)
            )
        admin_engine.dispose()
