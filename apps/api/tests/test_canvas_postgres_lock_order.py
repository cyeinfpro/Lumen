"""Opt-in real PostgreSQL lock-order regression using a disposable schema."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from lumen_core.canvas_models import CanvasNodeExecution
from app.canvas_services import read_repair

pytestmark = pytest.mark.skipif(
    os.getenv("LUMEN_LOCAL_PG_TEST") != "1",
    reason="explicit local PostgreSQL test only",
)


@asynccontextmanager
async def isolated_postgres():
    schema = "lumen_canvas_test_" + uuid4().hex
    admin = create_async_engine("postgresql+asyncpg://127.0.0.1/postgres")
    engine = None
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            "postgresql+asyncpg://127.0.0.1/postgres",
            connect_args={
                "server_settings": {
                    "search_path": schema + ",pg_catalog",
                    "lock_timeout": "1500ms",
                    "statement_timeout": "5000ms",
                }
            },
        )
        async with engine.begin() as db:
            for table in ("users", "canvas_documents", "canvas_runs"):
                await db.execute(
                    text(f"CREATE TABLE {table} (id varchar(36) PRIMARY KEY)")
                )
            await db.run_sync(CanvasNodeExecution.__table__.create)
            for table, identifier in (
                ("users", "u"),
                ("canvas_documents", "c"),
                ("canvas_runs", "r"),
            ):
                await db.execute(
                    text(f"INSERT INTO {table} VALUES (:identifier)"),
                    {"identifier": identifier},
                )
        yield async_sessionmaker(engine, expire_on_commit=False), schema
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def execution(number):
    return CanvasNodeExecution(
        id=f"e{number:03}",
        canvas_id="c",
        run_id="r",
        user_id="u",
        node_id=f"n{number}",
        node_type="image_generate",
        node_schema_version=1,
        sequence=number,
        attempt=0,
        attempt_epoch=0,
        status="running",
        definition_hash="a" * 64,
        input_hash="b" * 64,
        execution_fingerprint="c" * 64,
        submission_idempotency_key=f"key{number}",
        request_fingerprint="d" * 64,
        config_snapshot_jsonb={},
        input_snapshot_jsonb={},
        processor_version="test",
        outputs_jsonb=[],
        selection_base_revision=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["canvas", "next_page_execution"])
async def test_read_repair_never_carries_run_lock_into_next_execution(
    monkeypatch, barrier
):
    run_held, worker_held = asyncio.Event(), asyncio.Event()
    count = 3 if barrier == "canvas" else 101

    async def noop(*_args, **_kwargs):
        return False

    async def reconcile(db, *, execution, **_kwargs):
        if execution.id == "e001":
            await db.execute(text("SELECT id FROM canvas_runs WHERE id='r' FOR UPDATE"))
            run_held.set()
            await asyncio.wait_for(worker_held.wait(), timeout=2)
        elif execution.id == "e002" and barrier == "canvas":
            await db.execute(
                text("SELECT id FROM canvas_documents WHERE id='c' FOR UPDATE")
            )
            await db.execute(text("SELECT id FROM canvas_runs WHERE id='r' FOR UPDATE"))
        return False

    monkeypatch.setattr(read_repair, "lock_canvas_write_identity", noop)
    monkeypatch.setattr(read_repair, "_repair_missing_links", noop)
    monkeypatch.setattr(read_repair, "_reconcile_execution", reconcile)
    async with isolated_postgres() as (factory, schema):
        async with factory() as db:
            db.add_all(execution(index) for index in range(1, count + 1))
            await db.commit()

        async def worker():
            await asyncio.wait_for(run_held.wait(), timeout=2)
            async with factory() as db, db.begin():
                identifier = "e003" if barrier == "canvas" else "e101"
                await db.execute(
                    select(CanvasNodeExecution.id)
                    .where(CanvasNodeExecution.id == identifier)
                    .with_for_update()
                )
                if barrier == "canvas":
                    await db.execute(
                        text("SELECT id FROM canvas_documents WHERE id='c' FOR UPDATE")
                    )
                worker_held.set()
                await db.execute(
                    text("SELECT id FROM canvas_runs WHERE id='r' FOR UPDATE")
                )

        async def reader():
            async with factory() as db:
                await read_repair.repair_canvas_executions(
                    db, user_id="u", canvas_id="c", limit=count
                )

        await asyncio.wait_for(asyncio.gather(reader(), worker()), timeout=8)
        assert worker_held.is_set()
        print("PG_BARRIER_PASSED", barrier, schema, count)
