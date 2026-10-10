from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen_core.models import Base, User
from lumen_core.canvas_models import (
    CanvasDocument,
    CanvasRun,
    CanvasRunEvent,
    CanvasNodeExecution,
    CanvasExecutionTask,
)
from app.canvas_services.history_service import execution_event_batch, execution_history


@asynccontextmanager
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    CanvasDocument.__table__,
                    CanvasRun.__table__,
                    CanvasRunEvent.__table__,
                    CanvasNodeExecution.__table__,
                    CanvasExecutionTask.__table__,
                ],
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(CanvasDocument(id="canvas", user_id="u", title="Canvas", graph_jsonb={}))
        db.add(
            CanvasRun(
                id="run",
                canvas_id="canvas",
                version_id="version",
                user_id="u",
                kind="single",
                status="running",
                target_node_ids=["node"],
                idempotency_key="run",
                request_fingerprint="a" * 64,
                last_event_seq=3,
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


def execution(identifier, *, node="node", user="u"):
    return CanvasNodeExecution(
        id=identifier,
        canvas_id="canvas",
        run_id="run",
        user_id=user,
        node_id=node,
        node_type="image_generate",
        status="succeeded",
        node_schema_version=1,
        sequence=ord(identifier),
        attempt=ord(identifier),
        attempt_epoch=0,
        selection_base_revision=0,
        definition_hash="a" * 64,
        input_hash="b" * 64,
        execution_fingerprint="c" * 64,
        submission_idempotency_key=identifier,
        request_fingerprint="d" * 64,
        config_snapshot_jsonb={"model": "gpt-image-2", "_canvas": {"internal": True}},
        input_snapshot_jsonb={"prompt": "exact submitted prompt"},
        model_snapshot_jsonb={},
        pricing_snapshot_jsonb={},
        processor_version="test",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_history_keyset_tie_breaker_and_node_owner_scope():
    async with session() as db:
        db.add_all(
            [
                execution("a"),
                execution("b"),
                execution("c"),
                execution("d", node="other"),
                execution("e", user="foreign"),
            ]
        )
        await db.commit()
        first = await execution_history(
            db, user_id="u", canvas_id="canvas", node_id="node", limit=2
        )
        assert [row["id"] for row in first["items"]] == ["c", "b"]
        assert "_canvas" not in first["items"][0]["config_snapshot"]
        assert first["items"][0]["input_snapshot"]["prompt"] == "exact submitted prompt"
        second = await execution_history(
            db,
            user_id="u",
            canvas_id="canvas",
            node_id="node",
            limit=2,
            cursor=first["next_cursor"],
        )
        assert [row["id"] for row in second["items"]] == ["a"]
        assert second["next_cursor"] is None
        with pytest.raises(HTTPException) as denied:
            await execution_history(
                db, user_id="foreign", canvas_id="canvas", node_id="node"
            )
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_event_batch_bounded_cursor_and_ownership():
    async with session() as db:
        for seq in range(1, 4):
            db.add(
                CanvasRunEvent(
                    run_id="run",
                    seq=seq,
                    event_type="status",
                    event_key=f"e{seq}",
                    payload_jsonb={"status": "running"},
                )
            )
        await db.commit()
        first = await execution_event_batch(
            db, user_id="u", canvas_id="canvas", run_id="run", limit=2
        )
        assert [row["seq"] for row in first["items"]] == [1, 2]
        assert first["has_more"]
        assert not first["snapshot_required"]
        second = await execution_event_batch(
            db,
            user_id="u",
            canvas_id="canvas",
            run_id="run",
            after_seq=first["next_after_seq"],
        )
        assert [row["seq"] for row in second["items"]] == [3]
        assert not second["has_more"]
        with pytest.raises(HTTPException) as denied:
            await execution_event_batch(
                db, user_id="foreign", canvas_id="canvas", run_id="run"
            )
        assert denied.value.status_code == 404
