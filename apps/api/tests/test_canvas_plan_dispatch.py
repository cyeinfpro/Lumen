from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from lumen_core.models import Base
from lumen_core.model_entities import Image, User, Video
from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasDocument,
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
    CanvasVersion,
)
from lumen_core.canvas_capabilities import image_capability
from app.canvas_services.core_adapter import validated_graph
from app.canvas_services.plan_creation import start_run_plan
from app.canvas_services.plan_dispatch import dispatch_plan_once, latest_plan_executions
from app.canvas_services.plan_preview import preview_run_plan
from app.canvas_services.plan_pricing import PlanQuote
from app.canvas_services.run_plan_schemas import CanvasPlanIn, CanvasPlanStartIn


def graph():
    def node(identifier, kind):
        return {
            "id": identifier,
            "type": kind,
            "position": {"x": 0, "y": 0},
            "config": {"text": "draw"} if kind == "prompt" else {},
        }

    def edge(identifier, source, target, handle, kind):
        return {
            "id": identifier,
            "source_node_id": source,
            "target_node_id": target,
            "source_handle": "text" if kind == "text" else "image",
            "target_handle": handle,
            "data_type": kind,
        }

    return validated_graph(
        {
            "nodes": [
                node("prompt", "prompt"),
                node("a", "image_generate"),
                node("b", "image_generate"),
                node("c", "image_edit"),
            ],
            "edges": [
                edge("pa", "prompt", "a", "prompt", "text"),
                edge("pb", "prompt", "b", "prompt", "text"),
                edge("pc", "prompt", "c", "prompt", "text"),
                edge("ac", "a", "c", "source", "image"),
            ],
        }
    )


@asynccontextmanager
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User,
        Image,
        Video,
        CanvasDocument,
        CanvasVersion,
        CanvasRun,
        CanvasNodeExecution,
        CanvasExecutionTask,
        CanvasNodeSelection,
        CanvasAssetRef,
        CanvasRunEvent,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[model.__table__ for model in tables]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(id="user", email="user@example.com", account_mode="wallet")
        canvas = CanvasDocument(
            id="canvas",
            user_id="user",
            title="plan",
            revision=1,
            graph_schema_version=1,
            graph_jsonb=graph(),
        )
        db.add_all([user, canvas])
        await db.commit()
        yield db, factory, user, canvas
    await engine.dispose()


@pytest.fixture(autouse=True)
def isolated_notices(monkeypatch):
    async def noop(_notices):
        return None

    monkeypatch.setattr("app.canvas_services.event_commit.publish_canvas_notices", noop)


@pytest.fixture
def quote(monkeypatch):
    async def fake_quote(*args, **kwargs):
        return PlanQuote(100, "gpt-image-2", image_capability(None)["version"])

    monkeypatch.setattr("app.canvas_services.plan_preview.quote_plan_node", fake_quote)
    monkeypatch.setattr("app.canvas_services.plan_dispatch.quote_plan_node", fake_quote)


async def create_plan(db, user, canvas, **overrides):
    body = CanvasPlanIn(document_revision=1, kind="all", budget_micro=300, **overrides)
    plan = await preview_run_plan(db, user=user, canvas=canvas, body=body)
    start = CanvasPlanStartIn(
        **body.model_dump(), plan_hash=plan.plan_hash, idempotency_key="request"
    )
    run = await start_run_plan(
        db, user=user, canvas_id=canvas.id, body=start, header_idempotency_key="request"
    )
    return run, start


async def fake_admit(db, *, prepared, **kwargs):
    db.add(
        CanvasExecutionTask(
            execution_id=prepared.execution.id,
            ordinal=0,
            task_kind="generation",
            generation_id="generation-" + prepared.execution.node_id,
            status="queued",
            idempotency_key=prepared.submission_key,
            request_fingerprint=prepared.request_fingerprint,
            billing_ref_type="generation",
            billing_ref_id="generation-" + prepared.execution.node_id,
        )
    )
    await db.commit()
    return prepared.run, prepared.execution


@pytest.mark.asyncio
async def test_persisted_plan_replay_restart_and_no_duplicate_admission(
    quote, monkeypatch
):
    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", fake_admit
    )
    async with database() as (db, factory, user, canvas):
        run, body = await create_plan(db, user, canvas)
        replay = await start_run_plan(
            db,
            user=user,
            canvas_id=canvas.id,
            body=body,
            header_idempotency_key="request",
        )
        assert replay.id == run.id
        rows = await latest_plan_executions(db, run.id)
        assert {row.status for row in rows.values()} == {"pending"}
        run_id = run.id
        await db.rollback()
        async with factory() as restarted:
            assert await dispatch_plan_once(restarted, run_id=run_id)
            assert await dispatch_plan_once(restarted, run_id=run_id)
            assert not await dispatch_plan_once(restarted, run_id=run_id)
            tasks = list(
                (await restarted.execute(select(CanvasExecutionTask))).scalars()
            )
            assert len(tasks) == 2
            states = await latest_plan_executions(restarted, run_id)
            assert states["a"].status == states["b"].status == "queued"
            assert states["c"].status == "pending"
            owner = await restarted.get(CanvasRun, run_id)
            assert owner.summary_jsonb["plan_admitted_micro"] == 200


@pytest.mark.asyncio
async def test_failure_blocks_descendants_but_independent_branch_admits(
    quote, monkeypatch
):
    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", fake_admit
    )
    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        rows = await latest_plan_executions(db, run.id)
        rows["a"].status = "failed"
        await db.commit()
        assert await dispatch_plan_once(db, run_id=run.id)
        rows = await latest_plan_executions(db, run.id)
        assert rows["c"].status == "blocked"
        assert rows["b"].status == "queued"


@pytest.mark.asyncio
async def test_crash_before_admission_rolls_back_budget_and_marks_only_failed_node(
    quote, monkeypatch
):
    async def fail(*args, **kwargs):
        raise RuntimeError("before commit")

    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", fail
    )
    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        run_id = run.id
        with pytest.raises(RuntimeError):
            await dispatch_plan_once(db, run_id=run_id)
        rows = await latest_plan_executions(db, run_id)
        assert rows["a"].status == "failed"
        assert rows["b"].status == "pending"
        owner = await db.get(CanvasRun, run_id)
        assert owner.summary_jsonb["plan_admitted_micro"] == 0
        assert not list((await db.execute(select(CanvasExecutionTask))).scalars())


@pytest.mark.asyncio
async def test_lost_commit_ack_does_not_mark_queued_task_failed_or_retry(
    quote, monkeypatch
):
    async def commit_then_fail(db, **kwargs):
        await fake_admit(db, **kwargs)
        raise RuntimeError("ack lost")

    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution",
        commit_then_fail,
    )
    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        run_id = run.id
        with pytest.raises(RuntimeError):
            await dispatch_plan_once(db, run_id=run_id)
        rows = await latest_plan_executions(db, run_id)
        assert rows["a"].status == "queued"
        assert len(list((await db.execute(select(CanvasExecutionTask))).scalars())) == 1


@pytest.mark.asyncio
async def test_changed_plan_or_budget_rejected_before_tasks(quote):
    async with database() as (db, factory, user, canvas):
        with pytest.raises(HTTPException) as denied:
            await preview_run_plan(
                db,
                user=user,
                canvas=canvas,
                body=CanvasPlanIn(document_revision=1, kind="all", budget_micro=299),
            )
        assert denied.value.detail["error"]["code"] == "canvas_plan_budget_insufficient"
        body = CanvasPlanStartIn(
            document_revision=1,
            kind="all",
            budget_micro=300,
            plan_hash="a" * 64,
            idempotency_key="request",
        )
        with pytest.raises(HTTPException) as denied:
            await start_run_plan(
                db,
                user=user,
                canvas_id=canvas.id,
                body=body,
                header_idempotency_key="request",
            )
        assert denied.value.status_code == 409
        assert not list((await db.execute(select(CanvasExecutionTask))).scalars())


def test_plan_admission_guard_rejects_unknown_and_higher_real_snapshot():
    from app.canvas_services.plan_admission_guard import verify_plan_admission

    prepared = SimpleNamespace(
        run=SimpleNamespace(kind="all"),
        execution=SimpleNamespace(pricing_snapshot_jsonb={"estimated_cost_micro": 100}),
        node_type="image_generate",
    )
    for owner in (SimpleNamespace(), SimpleNamespace(est_cost_micro=101)):
        with pytest.raises(HTTPException):
            verify_plan_admission(prepared, [owner])
    verify_plan_admission(prepared, [SimpleNamespace(est_cost_micro=100)])


@pytest.mark.asyncio
async def test_saved_output_drives_downstream_from_original_snapshot(
    quote, monkeypatch
):
    from copy import deepcopy

    admitted = []

    async def capture(db, **kwargs):
        prepared = kwargs["prepared"]
        admitted.append(
            (
                prepared.execution.node_id,
                prepared.resolved.prompt,
                deepcopy(prepared.resolved.snapshot),
            )
        )
        return await fake_admit(db, **kwargs)

    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", capture
    )
    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        await dispatch_plan_once(db, run_id=run.id)
        rows = await latest_plan_executions(db, run.id)
        image = Image(
            id="saved",
            user_id=user.id,
            source="generated",
            sha256="a" * 64,
            mime="image/png",
            width=32,
            height=32,
            size_bytes=10,
            storage_key="fixture/saved.png",
        )
        db.add(image)
        rows["a"].status = "succeeded"
        rows["a"].outputs_jsonb = [
            {"type": "image", "image_id": "saved", "sha256": "a" * 64}
        ]
        changed = deepcopy(canvas.graph_jsonb)
        changed["nodes"][0]["config"]["text"] = "new draft, not the plan"
        canvas.graph_jsonb = changed
        canvas.revision += 1
        await db.commit()
        await dispatch_plan_once(db, run_id=run.id)  # Independent b.
        await dispatch_plan_once(db, run_id=run.id)  # c consumes a's saved output.
        downstream = next(item for item in admitted if item[0] == "c")
        assert downstream[1] == "draw"
        binding = next(
            item for item in downstream[2]["bindings"] if item["edge_id"] == "ac"
        )
        assert binding["source_execution_id"] == rows["a"].id
        assert binding["asset"]["image_id"] == "saved"
        assert binding["binding_mode"] == "follow_active"


@pytest.mark.asyncio
async def test_fail_fast_retry_admits_selected_root_without_restarting_other_branch(
    quote, monkeypatch
):
    from app.canvas_services.plan_retry import retry_failed_plan_steps
    from app.canvas_services.run_plan_schemas import CanvasPlanRetryIn

    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", fake_admit
    )
    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas, failure_policy="fail_fast")
        original = await latest_plan_executions(db, run.id)
        original["a"].status = "failed"
        original["a"].error_code = "canvas_plan_admission_failed"
        await db.commit()
        # The dispatcher fail-fast blocks both a's descendant and independent b.
        await dispatch_plan_once(db, run_id=run.id)
        assert original["b"].status == original["c"].status == "blocked"
        body = CanvasPlanRetryIn(
            idempotency_key="fail-fast-repair",
            execution_ids=[original["a"].id],
            additional_budget_micro=0,
        )
        await retry_failed_plan_steps(
            db,
            user=user,
            canvas_id=canvas.id,
            run_id=run.id,
            body=body,
            header_idempotency_key=body.idempotency_key,
        )
        repaired = await latest_plan_executions(db, run.id)
        assert repaired["a"].attempt == repaired["c"].attempt == 1
        assert repaired["b"].id == original["b"].id
        assert await dispatch_plan_once(db, run_id=run.id)
        assert repaired["a"].status == "queued"
        assert repaired["b"].status == "blocked"
        assert repaired["c"].status == "pending"
        tasks = list((await db.execute(select(CanvasExecutionTask))).scalars())
        assert len(tasks) == 1 and tasks[0].execution_id == repaired["a"].id
        assert run.summary_jsonb["plan_admitted_micro"] == 100


@pytest.mark.asyncio
async def test_retry_repairs_only_failed_steps_and_replays_idempotently(quote):
    from app.canvas_services.plan_retry import retry_failed_plan_steps
    from app.canvas_services.run_plan_schemas import CanvasPlanRetryIn

    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        rows = await latest_plan_executions(db, run.id)
        rows["a"].status = "failed"
        rows["a"].error_code = "canvas_plan_admission_failed"
        rows["b"].status = "succeeded"
        rows["c"].status = "blocked"
        original_success = rows["b"].id
        summary = dict(run.summary_jsonb)
        summary["plan_admitted_micro"] = 200
        run.summary_jsonb = summary
        await db.commit()
        body = CanvasPlanRetryIn(
            idempotency_key="repair",
            execution_ids=[rows["a"].id],
            additional_budget_micro=100,
        )
        await retry_failed_plan_steps(
            db,
            user=user,
            canvas_id=canvas.id,
            run_id=run.id,
            body=body,
            header_idempotency_key="repair",
        )
        repaired = await latest_plan_executions(db, run.id)
        assert repaired["a"].attempt == 1 and repaired["c"].attempt == 1
        assert repaired["b"].id == original_success
        assert repaired["a"].retry_of_execution_id == rows["a"].id
        assert run.budget_micro == 400
        await retry_failed_plan_steps(
            db,
            user=user,
            canvas_id=canvas.id,
            run_id=run.id,
            body=body,
            header_idempotency_key="repair",
        )
        assert run.budget_micro == 400
        assert len(list((await db.execute(select(CanvasNodeExecution))).scalars())) == 5


@pytest.mark.asyncio
async def test_retry_rejects_success_unknown_active_and_partial_outputs(quote):
    from app.canvas_services.plan_retry import retry_failed_plan_steps
    from app.canvas_services.run_plan_schemas import CanvasPlanRetryIn

    async with database() as (db, factory, user, canvas):
        run, _body = await create_plan(db, user, canvas)
        rows = await latest_plan_executions(db, run.id)
        for status in ("succeeded", "running", "reconciling", "partial_failed"):
            rows["a"].status = status
            await db.commit()
            body = CanvasPlanRetryIn(
                idempotency_key="repair-" + status,
                execution_ids=[rows["a"].id],
                additional_budget_micro=100,
            )
            with pytest.raises(HTTPException) as denied:
                await retry_failed_plan_steps(
                    db,
                    user=user,
                    canvas_id=canvas.id,
                    run_id=run.id,
                    body=body,
                    header_idempotency_key=body.idempotency_key,
                )
            assert denied.value.detail["error"]["code"] == "canvas_plan_retry_unsafe"


@pytest.mark.asyncio
async def test_explicit_reuse_keeps_exact_output_without_new_source_task(
    quote, monkeypatch
):
    monkeypatch.setattr(
        "app.canvas_services.plan_dispatch.dispatch_prepared_execution", fake_admit
    )
    async with database() as (db, factory, user, canvas):
        original, _body = await create_plan(db, user, canvas)
        old = await latest_plan_executions(db, original.id)
        # Finish the old target before explicitly starting a new paid branch.
        # An unrelated pending b must not prevent exact source reuse for c.
        old["c"].status = "canceled"
        old["a"].status = "succeeded"
        old["a"].outputs_jsonb = [
            {"type": "image", "image_id": "reused", "sha256": "b" * 64}
        ]
        db.add(
            Image(
                id="reused",
                user_id=user.id,
                source="generated",
                sha256="b" * 64,
                mime="image/png",
                width=32,
                height=32,
                size_bytes=10,
                storage_key="fixture/reused.png",
            )
        )
        await db.commit()
        body = CanvasPlanIn(
            document_revision=1,
            kind="upstream",
            target_node_ids=["c"],
            budget_micro=100,
            reuse_outputs={"a": {"execution_id": old["a"].id, "output_index": 0}},
        )
        plan = await preview_run_plan(db, user=user, canvas=canvas, body=body)
        start = CanvasPlanStartIn(
            **body.model_dump(),
            plan_hash=plan.plan_hash,
            idempotency_key="explicit-reuse",
        )
        run = await start_run_plan(
            db,
            user=user,
            canvas_id=canvas.id,
            body=start,
            header_idempotency_key="explicit-reuse",
        )
        states = await latest_plan_executions(db, run.id)
        assert states["a"].status == "reused"
        assert states["a"].reused_from_execution_id == old["a"].id
        assert await dispatch_plan_once(db, run_id=run.id)
        tasks = list((await db.execute(select(CanvasExecutionTask))).scalars())
        assert len(tasks) == 1
        assert tasks[0].execution_id == states["c"].id
        assert run.summary_jsonb["plan_admitted_micro"] == 100
