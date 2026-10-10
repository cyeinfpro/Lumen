from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from lumen_core.models import Generation
from lumen_core.canvas_models import CanvasRun, CanvasRunEvent
from app.canvas_progress_events import record_canvas_progress


@pytest.mark.asyncio
async def test_progress_sequence_is_durable_coalesced_and_owner_scoped():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    def create_tables(sync):
        for table in [
            Generation.__table__,
            CanvasRun.__table__,
            CanvasRunEvent.__table__,
        ]:
            ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            sync.exec_driver_sql(
                ddl.replace("DEFAULT (ARRAY[]::varchar[])", "DEFAULT '[]'")
            )

    async with engine.begin() as conn:
        await conn.run_sync(create_tables)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        owner = Generation(
            id="gen",
            user_id="u",
            message_id="m",
            action="generate",
            model="gpt-image-2",
            prompt="private",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            status="running",
            progress_stage="rendering",
            idempotency_key="gen",
        )
        run = CanvasRun(
            id="run",
            user_id="u",
            canvas_id="canvas",
            version_id="v",
            kind="single",
            status="running",
            target_node_ids=["node"],
            idempotency_key="run",
            request_fingerprint="a" * 64,
        )
        db.add_all([owner, run])
        await db.commit()
        execution = SimpleNamespace(
            id="exec", run_id="run", user_id="u", node_id="node"
        )
        task = SimpleNamespace(task_kind="generation", generation_id="gen")
        assert await record_canvas_progress(db, execution=execution, tasks=[task])
        await db.commit()
        assert run.last_event_seq == 1
        assert not await record_canvas_progress(db, execution=execution, tasks=[task])
        owner.progress_stage = "saving"
        assert await record_canvas_progress(db, execution=execution, tasks=[task])
        await db.commit()
        events = list(
            (
                await db.execute(select(CanvasRunEvent).order_by(CanvasRunEvent.seq))
            ).scalars()
        )
        assert [event.seq for event in events] == [1, 2]
        assert events[-1].payload_jsonb["tasks"][0]["progress_stage"] == "saving"
        assert events[-1].payload_jsonb["tasks"][0]["progress_pct"] is None
        assert "private" not in str(events[-1].payload_jsonb)
        execution.user_id = "foreign"
        assert not await record_canvas_progress(db, execution=execution, tasks=[task])
    await engine.dispose()
