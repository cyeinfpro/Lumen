from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasDocument,
    CanvasExecutionTask,
    CanvasMutation,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
    CanvasTaskTerminalReceipt,
    CanvasVersion,
)
from lumen_core.models import Base, VideoGeneration

from app import db as app_db
from app import deps
from app.canvas_services.api_schemas import (
    MAX_CANVAS_MUTATION_JSON_BYTES,
    CanvasCreateIn,
    CanvasDuplicateIn,
    CanvasExecuteIn,
    CanvasMutationIn,
    CanvasPatchIn,
    CanvasSelectOutputIn,
    CanvasVersionCreateIn,
)
from app.canvas_services.document_service import (
    create_canvas,
    duplicate_canvas,
    get_owned_canvas,
)
from app.canvas_services.execution_service import execute_node
from app.canvas_services.mutation_service import (
    _revision_conflict_details,
    apply_mutation,
)
from app.canvas_services.read_repair import repair_canvas_executions
from app.canvas_services.run_serialization import (
    canvas_projections,
    list_run_events,
)
from app.canvas_services.selection_service import select_execution_output
from app.canvas_services.version_service import (
    create_named_version,
    restore_version,
)
from app.services.task_submission import CanvasImageSubmission
from app.routes import canvases as canvas_routes


def _graph(prompt: str = "一张产品海报") -> dict:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "schema_version": 1,
                "title": "提示词",
                "position": {"x": 0, "y": 0},
                "config": {"text": prompt, "locked": False},
                "ui": {},
            },
            {
                "id": "image-1",
                "type": "image_generate",
                "schema_version": 1,
                "title": "图片生成",
                "position": {"x": 320, "y": 0},
                "config": {
                    "aspect_ratio": "1:1",
                    "quality": "1k",
                    "render_quality": "medium",
                    "count": 1,
                    "fast": True,
                    "output_format": "webp",
                    "background": "auto",
                    "moderation": "low",
                },
                "ui": {},
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "image-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
                "order": 0,
            }
        ],
        "frames": [],
        "settings": {"snap_to_grid": False, "grid_size": 16},
    }


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (CanvasCreateIn, {"title": "   "}),
        (CanvasPatchIn, {"title": "\t"}),
        (CanvasDuplicateIn, {"title": "\n"}),
        (CanvasVersionCreateIn, {"name": "  "}),
    ],
)
def test_canvas_names_reject_whitespace_only_values(schema, payload: dict) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_canvas_mutations_reject_deep_or_oversized_json() -> None:
    nested: dict = {"value": "leaf"}
    for _ in range(40):
        nested = {"nested": nested}
    common = {
        "base_revision": 1,
        "client_id": "tab-1",
        "mutation_id": "mutation-1",
    }
    with pytest.raises(ValidationError, match="nesting limit"):
        CanvasMutationIn.model_validate(
            {**common, "operations": [{"op": "noop", "payload": nested}]}
        )
    with pytest.raises(ValidationError, match="payload limit"):
        CanvasMutationIn.model_validate(
            {
                **common,
                "operations": [
                    {
                        "op": "noop",
                        "payload": "x" * MAX_CANVAS_MUTATION_JSON_BYTES,
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_revision_conflict_falls_back_to_snapshot_for_large_gaps() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="冲突窗口", graph=_graph()),
        )
        rows = [
            CanvasMutation(
                canvas_id=canvas.id,
                user_id="user-1",
                client_id="tab-remote",
                mutation_id=f"remote-{revision}",
                operation_schema_version=1,
                base_revision=revision - 1,
                result_revision=revision,
                operations_jsonb=[],
                response_jsonb={},
            )
            for revision in range(2, 103)
        ]
        db.add_all(rows)
        canvas.revision = 102
        canvas.updated_at = datetime.now(timezone.utc)
        await db.flush()

        details = await _revision_conflict_details(
            db,
            canvas=canvas,
            client_revision=1,
        )

    assert details["rebase_unavailable"] is True
    assert details["remote_mutations"] == []
    assert details["snapshot"]["revision"] == 102


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        CanvasDocument.__table__,
        CanvasMutation.__table__,
        CanvasVersion.__table__,
        CanvasRun.__table__,
        CanvasNodeExecution.__table__,
        CanvasExecutionTask.__table__,
        CanvasNodeSelection.__table__,
        CanvasAssetRef.__table__,
        CanvasRunEvent.__table__,
        CanvasTaskTerminalReceipt.__table__,
        VideoGeneration.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_canvas_crud_ownership_and_duplicate_excludes_history() -> None:
    async with _session() as db:
        source = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="原画布", graph=_graph()),
        )
        assert (
            await get_owned_canvas(
                db,
                user_id="user-1",
                canvas_id=source.id,
            )
        ).id == source.id

        with pytest.raises(HTTPException) as excinfo:
            await get_owned_canvas(
                db,
                user_id="user-2",
                canvas_id=source.id,
            )
        assert excinfo.value.status_code == 404

        copied = await duplicate_canvas(
            db,
            user_id="user-1",
            canvas_id=source.id,
            body=CanvasDuplicateIn(),
        )
        assert copied.id != source.id
        assert copied.title == "原画布 副本"
        assert copied.graph_jsonb == source.graph_jsonb
        assert copied.conversation_id is None
        assert (
            await db.execute(select(CanvasRun).where(CanvasRun.canvas_id == copied.id))
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_canvas_duplicate_detaches_pinned_execution_bindings() -> None:
    graph = _graph()
    graph["nodes"].append(
        {
            **graph["nodes"][1],
            "id": "image-2",
            "title": "图片生成 2",
            "position": {"x": 640, "y": 0},
        }
    )
    graph["edges"].append(
        {
            "id": "edge-pinned",
            "source_node_id": "image-1",
            "source_handle": "image",
            "target_node_id": "image-2",
            "target_handle": "references",
            "data_type": "image",
            "binding_mode": "pinned",
            "pinned_execution_id": "00000000-0000-4000-8000-000000000111",
            "pinned_output_index": 0,
            "order": 0,
        }
    )
    async with _session() as db:
        source = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="固定版本", graph=graph),
        )
        copied = await duplicate_canvas(
            db,
            user_id="user-1",
            canvas_id=source.id,
            body=CanvasDuplicateIn(),
        )

        source_edge = next(
            edge for edge in source.graph_jsonb["edges"] if edge["id"] == "edge-pinned"
        )
        copied_edge = next(
            edge for edge in copied.graph_jsonb["edges"] if edge["id"] == "edge-pinned"
        )
        assert source_edge["binding_mode"] == "pinned"
        assert copied_edge["binding_mode"] == "follow_active"
        assert copied_edge["pinned_execution_id"] is None
        assert copied_edge["pinned_output_index"] is None


@pytest.mark.asyncio
async def test_canvas_mutation_revision_replay_and_conflict_window() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="测试", graph=_graph()),
        )
        body = CanvasMutationIn(
            base_revision=1,
            client_id="tab-1",
            mutation_id="mutation-1",
            operations=[
                {
                    "op": "update_node_config",
                    "operation_schema_version": 1,
                    "node_id": "prompt-1",
                    "config": {"text": "更新提示词", "locked": False},
                }
            ],
        )
        first = await apply_mutation(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            body=body,
            header_idempotency_key="mutation-1",
        )
        replay = await apply_mutation(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            body=body,
            header_idempotency_key="mutation-1",
        )
        assert first["revision"] == 2
        assert replay["revision"] == 2

        with pytest.raises(HTTPException) as idempotency_exc:
            await apply_mutation(
                db,
                user_id="user-1",
                canvas_id=canvas.id,
                body=body.model_copy(
                    update={
                        "operations": [
                            {
                                "op": "move_nodes",
                                "operation_schema_version": 1,
                                "items": [{"node_id": "prompt-1", "x": 10, "y": 10}],
                            }
                        ]
                    }
                ),
                header_idempotency_key="mutation-1",
            )
        assert idempotency_exc.value.status_code == 409
        assert idempotency_exc.value.detail["error"]["code"] == "idempotency_conflict"

        with pytest.raises(HTTPException) as revision_exc:
            await apply_mutation(
                db,
                user_id="user-1",
                canvas_id=canvas.id,
                body=CanvasMutationIn(
                    base_revision=1,
                    client_id="tab-2",
                    mutation_id="mutation-2",
                    operations=[
                        {
                            "op": "move_nodes",
                            "operation_schema_version": 1,
                            "items": [{"node_id": "prompt-1", "x": 20, "y": 20}],
                        }
                    ],
                ),
                header_idempotency_key="mutation-2",
            )
        details = revision_exc.value.detail["error"]["details"]
        assert revision_exc.value.status_code == 409
        assert details["current_revision"] == 2
        assert details["rebase_unavailable"] is False
        assert [item["result_revision"] for item in details["remote_mutations"]] == [2]

        with pytest.raises(HTTPException) as precondition_exc:
            await apply_mutation(
                db,
                user_id="user-1",
                canvas_id=canvas.id,
                body=CanvasMutationIn(
                    base_revision=2,
                    client_id="tab-3",
                    mutation_id="mutation-3",
                    operations=[
                        {
                            "op": "update_node_config",
                            "operation_schema_version": 1,
                            "node_id": "prompt-1",
                            "precondition_hash": "0" * 64,
                            "config": {"text": "不会写入", "locked": False},
                        }
                    ],
                ),
                header_idempotency_key="mutation-3",
            )
        assert precondition_exc.value.status_code == 409
        assert (
            precondition_exc.value.detail["error"]["code"]
            == "canvas_precondition_failed"
        )


@pytest.mark.asyncio
async def test_named_version_restore_creates_new_head_revision() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="版本", graph=_graph("第一版")),
        )
        db.add(
            CanvasNodeSelection(
                canvas_id=canvas.id,
                node_id="image-1",
                execution_id=None,
                output_index=0,
                revision=2,
                locked=False,
            )
        )
        await db.flush()
        named = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="基线",
        )
        await apply_mutation(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            body=CanvasMutationIn(
                base_revision=1,
                client_id="tab-1",
                mutation_id="mutation-1",
                operations=[
                    {
                        "op": "update_node_config",
                        "operation_schema_version": 1,
                        "node_id": "prompt-1",
                        "config": {"text": "第二版", "locked": False},
                    }
                ],
            ),
            header_idempotency_key="mutation-1",
        )
        selection = (
            await db.execute(
                select(CanvasNodeSelection).where(
                    CanvasNodeSelection.canvas_id == canvas.id,
                    CanvasNodeSelection.node_id == "image-1",
                )
            )
        ).scalar_one()
        selection.revision = 7
        await db.commit()
        restored = await restore_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            version_id=named.id,
        )
        current = await get_owned_canvas(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
        )
        assert restored.kind == "restore"
        assert current.revision == 3
        assert current.graph_jsonb["nodes"][0]["config"]["text"] == "第一版"
        restored_selection = (
            await db.execute(
                select(CanvasNodeSelection).where(
                    CanvasNodeSelection.canvas_id == canvas.id,
                    CanvasNodeSelection.node_id == "image-1",
                )
            )
        ).scalar_one()
        assert restored_selection.revision == 8


@pytest.mark.asyncio
async def test_image_execute_persists_run_tasks_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_image_task(*_args, **_kwargs) -> CanvasImageSubmission:
        return CanvasImageSubmission(
            generation_ids=["generation-1"],
            conversation_id="conversation-1",
            user_message_id="message-user",
            assistant_message_id="message-assistant",
            outbox_payloads=[],
            outbox_rows=[],
        )

    async def fake_publish(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setitem(
        execute_node.__globals__,
        "create_canvas_image_task",
        fake_create_image_task,
    )
    monkeypatch.setitem(
        execute_node.__globals__,
        "publish_canvas_image_task",
        fake_publish,
    )
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="执行", graph=_graph()),
        )
        body = CanvasExecuteIn(
            document_revision=1,
            idempotency_key="execute-1",
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/canvases",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            }
        )
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            account_mode="wallet",
        )
        run, execution = await execute_node(
            db,
            user=user,
            canvas_id=canvas.id,
            node_id="image-1",
            body=body,
            header_idempotency_key="execute-1",
            request=request,
        )
        replay_run, replay_execution = await execute_node(
            db,
            user=user,
            canvas_id=canvas.id,
            node_id="image-1",
            body=body,
            header_idempotency_key="execute-1",
            request=request,
        )
        task = (
            await db.execute(
                select(CanvasExecutionTask).where(
                    CanvasExecutionTask.execution_id == execution.id
                )
            )
        ).scalar_one()
        assert replay_run.id == run.id
        assert replay_execution.id == execution.id
        assert task.generation_id == "generation-1"
        assert execution.input_snapshot_jsonb["prompt"] == "一张产品海报"

        with pytest.raises(HTTPException) as excinfo:
            await execute_node(
                db,
                user=user,
                canvas_id=canvas.id,
                node_id="image-1",
                body=body.model_copy(update={"auto_select_on_success": False}),
                header_idempotency_key="execute-1",
                request=request,
            )
        assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_output_selection_uses_revision_cas() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="选择", graph=_graph()),
        )
        version = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="运行快照",
        )
        run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-1",
            kind="single",
            status="succeeded",
            target_node_ids=["image-1"],
            idempotency_key="run-1",
            request_fingerprint="r" * 64,
        )
        db.add(run)
        await db.flush()
        execution = CanvasNodeExecution(
            canvas_id=canvas.id,
            run_id=run.id,
            user_id="user-1",
            node_id="image-1",
            node_type="image_generate",
            node_schema_version=1,
            sequence=0,
            attempt=0,
            attempt_epoch=0,
            status="succeeded",
            definition_hash="d" * 64,
            input_hash="i" * 64,
            execution_fingerprint="e" * 64,
            submission_idempotency_key="execution-1",
            request_fingerprint="r" * 64,
            config_snapshot_jsonb={},
            input_snapshot_jsonb={},
            processor_version="test",
            outputs_jsonb=[{"type": "image", "image_id": "image-1"}],
            selection_base_revision=0,
        )
        db.add(execution)
        await db.commit()

        selected = await select_execution_output(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            execution_id=execution.id,
            body=CanvasSelectOutputIn(output_index=0, selection_revision=0),
        )
        assert selected["execution_id"] == execution.id
        assert selected["revision"] == 1
        with pytest.raises(HTTPException) as excinfo:
            await select_execution_output(
                db,
                user_id="user-1",
                canvas_id=canvas.id,
                execution_id=execution.id,
                body=CanvasSelectOutputIn(output_index=0, selection_revision=0),
            )
        assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_get_repair_links_video_by_execution_metadata() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="视频补偿", graph=_graph()),
        )
        version = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="运行快照",
        )
        run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-1",
            kind="single",
            status="queued",
            target_node_ids=["video-1"],
            idempotency_key="video-run-1",
            request_fingerprint="r" * 64,
        )
        db.add(run)
        await db.flush()
        execution = CanvasNodeExecution(
            canvas_id=canvas.id,
            run_id=run.id,
            user_id="user-1",
            node_id="video-1",
            node_type="video_generate",
            node_schema_version=1,
            sequence=0,
            attempt=0,
            attempt_epoch=0,
            status="queued",
            definition_hash="d" * 64,
            input_hash="i" * 64,
            execution_fingerprint="e" * 64,
            submission_idempotency_key="video-execution-1",
            request_fingerprint="r" * 64,
            config_snapshot_jsonb={},
            input_snapshot_jsonb={},
            processor_version="test",
            selection_base_revision=0,
        )
        db.add(execution)
        await db.flush()
        video = VideoGeneration(
            user_id="user-1",
            action="t2v",
            model="video-model",
            prompt="视频提示词",
            duration_s=5,
            resolution="720p",
            aspect_ratio="16:9",
            upstream_request={"canvas_execution_id": execution.id},
            status="queued",
            progress_stage="queued",
            progress_pct=0,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            idempotency_key="video-real-1",
            request_fingerprint="r" * 64,
            est_token_upper=0,
            est_cost_micro=0,
        )
        db.add(video)
        await db.commit()

        await repair_canvas_executions(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
        )
        link = (
            await db.execute(
                select(CanvasExecutionTask).where(
                    CanvasExecutionTask.execution_id == execution.id
                )
            )
        ).scalar_one()
        assert link.task_kind == "video_generation"
        assert link.video_generation_id == video.id


@pytest.mark.asyncio
async def test_canvas_projection_includes_video_task_progress_details() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="视频进度", graph=_graph()),
        )
        version = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="运行快照",
        )
        run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-1",
            kind="single",
            status="running",
            target_node_ids=["video-1"],
            idempotency_key="video-progress-run",
            request_fingerprint="r" * 64,
        )
        db.add(run)
        await db.flush()
        execution = CanvasNodeExecution(
            canvas_id=canvas.id,
            run_id=run.id,
            user_id="user-1",
            node_id="video-1",
            node_type="video_generate",
            node_schema_version=1,
            sequence=0,
            attempt=0,
            attempt_epoch=0,
            status="running",
            definition_hash="d" * 64,
            input_hash="i" * 64,
            execution_fingerprint="e" * 64,
            submission_idempotency_key="video-progress-execution",
            request_fingerprint="r" * 64,
            config_snapshot_jsonb={},
            input_snapshot_jsonb={},
            processor_version="test",
            selection_base_revision=0,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
        db.add(execution)
        await db.flush()
        video = VideoGeneration(
            user_id="user-1",
            action="reference",
            model="video-model-pro",
            provider_name="primary-video",
            provider_kind="volcano_newapi",
            prompt="保持主体一致",
            duration_s=5,
            resolution="720p",
            aspect_ratio="16:9",
            generate_audio=True,
            upstream_request={"canvas_execution_id": execution.id},
            status="running",
            progress_stage="fetching",
            progress_pct=92,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            idempotency_key="video-progress-real",
            request_fingerprint="v" * 64,
            est_token_upper=0,
            est_cost_micro=0,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=42),
        )
        db.add(video)
        await db.flush()
        db.add(
            CanvasExecutionTask(
                execution_id=execution.id,
                ordinal=0,
                task_kind="video_generation",
                video_generation_id=video.id,
                status="running",
                idempotency_key="video-progress-task",
                request_fingerprint="t" * 64,
                billing_ref_type="video_generation",
                billing_ref_id=video.id,
            )
        )
        await db.commit()

        projection = await canvas_projections(db, canvas_id=canvas.id)
        projected = next(
            item
            for item in projection["recent_executions"]
            if item["id"] == execution.id
        )
        task = projected["tasks"][0]

        assert task["kind"] == "video_generation"
        assert task["video_generation_id"] == video.id
        assert task["status"] == "running"
        assert task["progress_stage"] == "fetching"
        assert task["progress_pct"] == 92
        assert task["model"] == "video-model-pro"
        assert task["provider_name"] == "primary-video"
        assert task["provider_kind"] == "volcano_newapi"
        assert task["action"] == "reference"
        assert task["resolution"] == "720p"
        assert task["duration_s"] == 5
        assert task["elapsed_ms"] >= 40_000


@pytest.mark.asyncio
async def test_canvas_detail_includes_all_selected_executions_once() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="投影", graph=_graph()),
        )
        version = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="运行快照",
        )
        run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-1",
            kind="all",
            status="succeeded",
            target_node_ids=[],
            idempotency_key="projection-run",
            request_fingerprint="r" * 64,
        )
        db.add(run)
        await db.flush()

        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def execution(
            *,
            node_id: str,
            sequence: int,
            created_at: datetime,
        ) -> CanvasNodeExecution:
            return CanvasNodeExecution(
                canvas_id=canvas.id,
                run_id=run.id,
                user_id="user-1",
                node_id=node_id,
                node_type="image_generate",
                node_schema_version=1,
                sequence=sequence,
                attempt=0,
                attempt_epoch=0,
                status="succeeded",
                definition_hash=f"{sequence:064x}",
                input_hash=f"{sequence + 1:064x}",
                execution_fingerprint=f"{sequence + 2:064x}",
                submission_idempotency_key=f"projection-execution-{sequence}",
                request_fingerprint=f"{sequence + 3:064x}",
                config_snapshot_jsonb={},
                input_snapshot_jsonb={},
                processor_version="test",
                outputs_jsonb=[{"type": "image", "image_id": f"image-{sequence}"}],
                selection_base_revision=0,
                created_at=created_at,
                updated_at=created_at,
            )

        selected_a = execution(
            node_id="selected-a",
            sequence=0,
            created_at=base_time + timedelta(minutes=1),
        )
        selected_b = execution(
            node_id="selected-b",
            sequence=1,
            created_at=base_time,
        )
        pinned = execution(
            node_id="pinned",
            sequence=60,
            created_at=base_time - timedelta(minutes=1),
        )
        recent = [
            execution(
                node_id="selected-recent" if index == 49 else f"recent-{index:02d}",
                sequence=index + 2,
                created_at=base_time + timedelta(minutes=100 + index),
            )
            for index in range(50)
        ]
        db.add_all([selected_a, selected_b, pinned, *recent])
        await db.flush()
        db.add_all(
            [
                CanvasNodeSelection(
                    canvas_id=canvas.id,
                    node_id=selected_a.node_id,
                    execution_id=selected_a.id,
                    output_index=0,
                    revision=1,
                ),
                CanvasNodeSelection(
                    canvas_id=canvas.id,
                    node_id=selected_b.node_id,
                    execution_id=selected_b.id,
                    output_index=0,
                    revision=1,
                ),
                CanvasNodeSelection(
                    canvas_id=canvas.id,
                    node_id=recent[-1].node_id,
                    execution_id=recent[-1].id,
                    output_index=0,
                    revision=1,
                ),
            ]
        )
        await db.commit()

        projection = await canvas_projections(
            db,
            canvas_id=canvas.id,
            execution_limit=50,
            graph={
                "edges": [
                    {
                        "binding_mode": "pinned",
                        "pinned_execution_id": pinned.id,
                    }
                ]
            },
        )
        execution_ids = [item["id"] for item in projection["recent_executions"]]
        assert execution_ids[:50] == [row.id for row in reversed(recent)]
        assert execution_ids[-3:] == [selected_a.id, selected_b.id, pinned.id]
        assert len(execution_ids) == len(set(execution_ids)) == 53


@pytest.mark.asyncio
async def test_canvas_run_events_filter_order_limit_and_ownership() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="事件", graph=_graph()),
        )
        other_canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="其他画布", graph=_graph()),
        )
        version = await create_named_version(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            name="运行快照",
        )
        run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-1",
            kind="single",
            status="running",
            target_node_ids=["image-1"],
            idempotency_key="event-run",
            request_fingerprint="r" * 64,
            last_event_seq=4,
        )
        foreign_owner_run = CanvasRun(
            canvas_id=canvas.id,
            version_id=version.id,
            user_id="user-2",
            kind="single",
            status="running",
            target_node_ids=["image-1"],
            idempotency_key="foreign-owner-run",
            request_fingerprint="f" * 64,
        )
        db.add_all([run, foreign_owner_run])
        await db.flush()
        db.add_all(
            [
                CanvasRunEvent(
                    run_id=run.id,
                    seq=seq,
                    event_type="canvas.execution.status_changed",
                    event_key=f"event-{seq}",
                    payload_jsonb={"status": f"status-{seq}"},
                )
                for seq in (4, 2, 1, 3)
            ]
        )
        await db.commit()

        events = await list_run_events(
            db,
            user_id="user-1",
            canvas_id=canvas.id,
            run_id=run.id,
            after_seq=1,
            limit=2,
        )
        assert [event["seq"] for event in events] == [2, 3]
        assert [event["payload"]["status"] for event in events] == [
            "status-2",
            "status-3",
        ]

        for user_id, canvas_id, run_id in (
            ("user-2", canvas.id, run.id),
            ("user-1", other_canvas.id, run.id),
            ("user-1", canvas.id, foreign_owner_run.id),
        ):
            with pytest.raises(HTTPException) as excinfo:
                await list_run_events(
                    db,
                    user_id=user_id,
                    canvas_id=canvas_id,
                    run_id=run_id,
                    after_seq=0,
                    limit=100,
                )
            assert excinfo.value.status_code == 404


def test_canvas_run_events_route_bounds_and_forwards_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_list_run_events(
        _db: object,
        *,
        user_id: str,
        canvas_id: str,
        run_id: str,
        after_seq: int,
        limit: int,
    ) -> list[dict]:
        captured.update(
            user_id=user_id,
            canvas_id=canvas_id,
            run_id=run_id,
            after_seq=after_seq,
            limit=limit,
        )
        return [{"seq": after_seq + 1}]

    monkeypatch.setattr(canvas_routes, "list_run_events", fake_list_run_events)
    app = FastAPI()
    app.include_router(canvas_routes.router)

    async def override_user() -> SimpleNamespace:
        return SimpleNamespace(id="user-1", email="user@example.com")

    async def override_db() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[deps.get_current_user] = override_user
    app.dependency_overrides[app_db.get_db] = override_db

    with TestClient(app) as client:
        response = client.get(
            "/canvases/canvas-1/runs/run-1/events",
            params={"after_seq": 7, "limit": 12},
        )
        assert response.status_code == 200
        assert response.json() == {"items": [{"seq": 8}]}
        assert captured == {
            "user_id": "user-1",
            "canvas_id": "canvas-1",
            "run_id": "run-1",
            "after_seq": 7,
            "limit": 12,
        }
        assert (
            client.get(
                "/canvases/canvas-1/runs/run-1/events",
                params={"after_seq": -1},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/canvases/canvas-1/runs/run-1/events",
                params={"limit": 0},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/canvases/canvas-1/runs/run-1/events",
                params={"limit": 201},
            ).status_code
            == 422
        )
