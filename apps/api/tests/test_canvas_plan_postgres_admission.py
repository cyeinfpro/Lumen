"""Real PostgreSQL admission races, isolated schema, no task/provider dispatch."""
import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from lumen_core.models import Base
from lumen_core.model_entities import User, Image, Video
from lumen_core.canvas_models import (
    CanvasDocument, CanvasVersion, CanvasRun, CanvasNodeExecution,
    CanvasExecutionTask, CanvasNodeSelection, CanvasAssetRef, CanvasRunEvent,
)
from lumen_core.canvas_capabilities import image_capability
from app.canvas_services.core_adapter import validated_graph
from app.canvas_services.plan_pricing import PlanQuote
from app.canvas_services.plan_preview import preview_run_plan
from app.canvas_services.plan_creation import start_run_plan
from app.canvas_services.plan_intents import get_plan_intent
from app.canvas_services.run_plan_schemas import CanvasPlanIn, CanvasPlanStartIn

pytestmark = pytest.mark.skipif(
    os.getenv("LUMEN_LOCAL_PG_TEST") != "1",
    reason="explicit disposable local PostgreSQL test only",
)


def graph():
    return validated_graph({
        "nodes": [
            {"id": "p", "type": "prompt", "position": {"x": 0, "y": 0}, "config": {"text": "stub"}},
            *[{"id": node, "type": "image_generate", "position": {"x": 0, "y": 0}, "config": {}} for node in ("a", "b")],
        ],
        "edges": [
            {"id": "p" + node, "source_node_id": "p", "source_handle": "text",
             "target_node_id": node, "target_handle": "prompt", "data_type": "text"}
            for node in ("a", "b")
        ],
    })


@asynccontextmanager
async def database():
    schema = "lumen_plan_admission_" + uuid4().hex
    admin = create_async_engine("postgresql+asyncpg://127.0.0.1/postgres")
    engine = None
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            "postgresql+asyncpg://127.0.0.1/postgres",
            connect_args={"server_settings": {"search_path": schema + ",pg_catalog",
                "lock_timeout": "3000ms", "statement_timeout": "6000ms"}},
        )
        tables = [User, Image, Video, CanvasDocument, CanvasVersion, CanvasRun,
                  CanvasNodeExecution, CanvasExecutionTask, CanvasNodeSelection,
                  CanvasAssetRef, CanvasRunEvent]
        async with engine.begin() as db:
            for table in ("system_prompts", "generations", "video_generations", "completions", "conversations"):
                await db.execute(text(f"CREATE TABLE {table} (id varchar(36) PRIMARY KEY)"))
            await db.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[model.__table__ for model in tables]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all([User(id=owner, email=owner + "@example.test", account_mode="wallet") for owner in ("u1", "u2")])
            await db.flush()
            db.add_all([CanvasDocument(id="c" + owner, user_id=owner, title="stub", revision=1,
                graph_schema_version=1, graph_jsonb=graph()) for owner in ("u1", "u2")])
            await db.commit()
        yield factory
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture(autouse=True)
def local_stubs(monkeypatch):
    async def quote(*args, **kwargs):
        return PlanQuote(100, "gpt-image-2", image_capability(None)["version"])
    async def notices(*args, **kwargs):
        return None
    monkeypatch.setattr("app.canvas_services.plan_preview.quote_plan_node", quote)
    monkeypatch.setattr("app.canvas_services.event_commit.publish_canvas_notices", notices)


async def prepared_body(factory, owner, node, key):
    async with factory() as db:
        user = await db.get(User, owner)
        canvas = await db.get(CanvasDocument, "c" + owner)
        body = CanvasPlanIn(document_revision=1, kind="selection", target_node_ids=[node], budget_micro=100)
        plan = await preview_run_plan(db, user=user, canvas=canvas, body=body)
        return CanvasPlanStartIn(**body.model_dump(), plan_hash=plan.plan_hash, idempotency_key=key)


async def submit(factory, owner, body):
    async with factory() as db:
        user = await db.get(User, owner)
        return await start_run_plan(db, user=user, canvas_id="c" + owner,
            body=body, header_idempotency_key=body.idempotency_key)


@pytest.mark.asyncio
async def test_concurrent_different_keys_same_node_admit_once_and_replay_recovers(monkeypatch):
    import app.canvas_services.plan_creation as creation
    entered, competitor, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_preview, original_identity = creation.preview_run_plan, creation.lock_canvas_write_identity
    first = True
    async def preview(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        return await original_preview(*args, **kwargs)
    async def identity(*args, **kwargs):
        if entered.is_set():
            competitor.set()
        return await original_identity(*args, **kwargs)
    monkeypatch.setattr(creation, "preview_run_plan", preview)
    monkeypatch.setattr(creation, "lock_canvas_write_identity", identity)
    async with database() as factory:
        one = await prepared_body(factory, "u1", "a", "one")
        two = await prepared_body(factory, "u1", "a", "two")
        winner = asyncio.create_task(submit(factory, "u1", one))
        await asyncio.wait_for(entered.wait(), 2)
        loser = asyncio.create_task(submit(factory, "u1", two))
        await asyncio.wait_for(competitor.wait(), 2)
        release.set()
        run = await asyncio.wait_for(winner, 4)
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(loser, 4)
        assert rejected.value.detail["error"]["code"] == "canvas_execution_active"
        assert (await submit(factory, "u1", one)).id == run.id
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(CanvasRun)) == 1
            assert await db.scalar(select(func.count()).select_from(CanvasNodeExecution)) == 1
            receipt = await get_plan_intent(db, user_id="u1", canvas_id="cu1", idempotency_key="one")
            assert receipt["admitted"] and receipt["run"]["id"] == run.id


@pytest.mark.asyncio
async def test_different_nodes_and_owners_are_not_falsely_blocked():
    async with database() as factory:
        bodies = [
            ("u1", await prepared_body(factory, "u1", "a", "one")),
            ("u1", await prepared_body(factory, "u1", "b", "two")),
            ("u2", await prepared_body(factory, "u2", "a", "one")),
        ]
        runs = await asyncio.wait_for(asyncio.gather(*(submit(factory, owner, body) for owner, body in bodies)), 5)
        assert len({run.id for run in runs}) == 3
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(CanvasRun)) == 3
            assert await db.scalar(select(func.count()).select_from(CanvasExecutionTask)) == 0
            with pytest.raises(HTTPException):
                await get_plan_intent(db, user_id="u2", canvas_id="cu1", idempotency_key="one")
