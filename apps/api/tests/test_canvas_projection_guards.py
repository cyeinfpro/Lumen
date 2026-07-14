from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen_core.canvas import canvas_node_definition_hash
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
from lumen_core.models import Base, Image, Video

from app.canvas_services import read_repair, selection_service
from app.canvas_services.api_schemas import CanvasSelectOutputIn
from app.canvas_services.asset_ref_service import ensure_asset_not_canvas_referenced
from app.canvas_services.graph_resolution import resolve_node


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        CanvasDocument.__table__,
        CanvasVersion.__table__,
        CanvasRun.__table__,
        CanvasNodeExecution.__table__,
        CanvasExecutionTask.__table__,
        CanvasNodeSelection.__table__,
        CanvasAssetRef.__table__,
        CanvasRunEvent.__table__,
        Image.__table__,
        Video.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _execution(
    *,
    execution_id: str,
    canvas_id: str = "canvas-1",
    run_id: str = "missing-run",
    user_id: str = "user-1",
    node_id: str = "node-1",
    node_type: str = "image_generate",
    outputs: list[dict] | None = None,
    finished_at: datetime | None = None,
) -> CanvasNodeExecution:
    return CanvasNodeExecution(
        id=execution_id,
        canvas_id=canvas_id,
        run_id=run_id,
        user_id=user_id,
        node_id=node_id,
        node_type=node_type,
        node_schema_version=1,
        sequence=0,
        attempt=0,
        attempt_epoch=0,
        status="succeeded",
        definition_hash="d" * 64,
        input_hash="i" * 64,
        execution_fingerprint="e" * 64,
        submission_idempotency_key=f"submission-{execution_id}",
        request_fingerprint="r" * 64,
        config_snapshot_jsonb={},
        input_snapshot_jsonb={},
        model_snapshot_jsonb={},
        pricing_snapshot_jsonb={},
        processor_version="test",
        outputs_jsonb=outputs or [],
        selection_base_revision=0,
        finished_at=finished_at,
    )


def _execution_task(
    *,
    execution_id: str,
    owner_id: str,
    task_kind: str = "generation",
    output: dict | None = None,
) -> CanvasExecutionTask:
    is_video = task_kind == "video_generation"
    return CanvasExecutionTask(
        execution_id=execution_id,
        ordinal=0,
        task_kind=task_kind,
        generation_id=None if is_video else owner_id,
        video_generation_id=owner_id if is_video else None,
        status="succeeded",
        idempotency_key=f"task-{task_kind}-{owner_id}",
        request_fingerprint="r" * 64,
        billing_ref_type=task_kind,
        billing_ref_id=owner_id,
        output_jsonb=output or {},
    )


def _image(*, image_id: str, owner_generation_id: str | None = None) -> Image:
    return Image(
        id=image_id,
        user_id="user-1",
        owner_generation_id=owner_generation_id,
        source="generated",
        storage_key=f"images/{image_id}.webp",
        mime="image/webp",
        width=1024,
        height=1024,
        size_bytes=1024,
        sha256="a" * 64,
        visibility="private",
        metadata_jsonb={},
    )


def _video(*, video_id: str, owner_generation_id: str | None = None) -> Video:
    return Video(
        id=video_id,
        user_id="user-1",
        owner_generation_id=owner_generation_id,
        storage_key=f"videos/{video_id}.mp4",
        mime="video/mp4",
        width=1280,
        height=720,
        duration_ms=5000,
        size_bytes=2048,
        sha256="b" * 64,
        etag=f"etag-{video_id}",
        visibility="private",
        metadata_jsonb={},
    )


@pytest.mark.asyncio
async def test_materialize_asset_refs_reports_only_new_rows() -> None:
    output = {"type": "image", "image_id": "image-1"}
    async with _session() as db:
        execution = _execution(execution_id="execution-1", outputs=[output])
        db.add(execution)
        await db.commit()

        assert (
            await read_repair._materialize_asset_refs(  # noqa: SLF001
                db,
                execution=execution,
                outputs=[output],
            )
            is True
        )
        await db.flush()
        assert (
            await read_repair._materialize_asset_refs(  # noqa: SLF001
                db,
                execution=execution,
                outputs=[output],
            )
            is False
        )
        refs = (
            (
                await db.execute(
                    select(CanvasAssetRef).where(
                        CanvasAssetRef.execution_id == execution.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(refs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_refs_added", "selection_updated"),
    [(True, False), (False, True)],
)
async def test_reconcile_reports_projection_only_changes(
    monkeypatch: pytest.MonkeyPatch,
    asset_refs_added: bool,
    selection_updated: bool,
) -> None:
    finished_at = datetime(2026, 7, 13, tzinfo=timezone.utc)
    output = {"type": "image", "image_id": "image-1"}
    async with _session() as db:
        execution = _execution(
            execution_id="execution-projection",
            outputs=[output],
            finished_at=finished_at,
        )
        task = _execution_task(
            execution_id=execution.id,
            owner_id="generation-1",
            output=output,
        )
        db.add_all([execution, task])
        await db.commit()

        async def project_generation(
            _db: AsyncSession,
            _task: CanvasExecutionTask,
        ) -> tuple[str, dict, SimpleNamespace]:
            return (
                "succeeded",
                output,
                SimpleNamespace(
                    status="succeeded",
                    finished_at=finished_at,
                    error_code=None,
                    error_message=None,
                ),
            )

        async def materialize(*_args, **_kwargs) -> bool:
            return asset_refs_added

        async def auto_select(*_args, **_kwargs) -> bool:
            return selection_updated

        monkeypatch.setattr(read_repair, "_project_generation", project_generation)
        monkeypatch.setattr(read_repair, "_materialize_asset_refs", materialize)
        monkeypatch.setattr(read_repair, "_auto_select", auto_select)

        changed = await read_repair._reconcile_execution(  # noqa: SLF001
            db,
            user_id="user-1",
            execution=execution,
        )

        assert changed is True
        assert execution.status == "succeeded"
        assert task.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_prompt", "expected"),
    [("Render a still", True), ("Changed prompt", False)],
)
async def test_auto_select_uses_locked_current_inputs_not_document_revision(
    current_prompt: str,
    expected: bool,
) -> None:
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "schema_version": 1,
                "title": "Prompt",
                "position": {"x": 0, "y": 0},
                "config": {"text": current_prompt, "locked": False},
                "ui": {},
            },
            {
                "id": "node-1",
                "type": "image_generate",
                "schema_version": 1,
                "title": "Image",
                "position": {"x": 999, "y": 400},
                "config": {},
                "ui": {},
            },
        ],
        "edges": [
            {
                "id": "prompt-image",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "node-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
                "order": 0,
            }
        ],
        "frames": [],
        "settings": {"snap_to_grid": False, "grid_size": 16},
    }
    input_snapshot = {
        "prompt": "Render a still",
        "bindings": [
            {
                "edge_id": "prompt-image",
                "source_node_id": "prompt-1",
                "target_handle": "prompt",
                "role": None,
                "order": 0,
                "binding_mode": "follow_active",
                "text": "Render a still",
            }
        ],
    }
    async with _session() as db:
        canvas = CanvasDocument(
            id="canvas-auto-select",
            user_id="user-1",
            title="Auto select",
            description="",
            graph_schema_version=1,
            graph_jsonb=graph,
            revision=17,
        )
        execution = _execution(
            execution_id="execution-auto-select",
            canvas_id=canvas.id,
            node_id="node-1",
            outputs=[{"type": "image", "image_id": "image-1"}],
        )
        execution.definition_hash = canvas_node_definition_hash(graph["nodes"][1])
        execution.input_snapshot_jsonb = input_snapshot
        execution.config_snapshot_jsonb = {"_canvas": {"auto_select_on_success": True}}
        execution.selection_base_revision = 3
        selection = CanvasNodeSelection(
            canvas_id=canvas.id,
            node_id=execution.node_id,
            execution_id=None,
            output_index=0,
            revision=3,
            locked=False,
        )
        db.add_all([canvas, execution, selection])
        await db.commit()

        changed = await read_repair._auto_select(  # noqa: SLF001
            db,
            user_id="user-1",
            execution=execution,
            outputs=execution.outputs_jsonb,
        )

        assert changed is expected
        assert selection.execution_id == (execution.id if expected else None)
        assert selection.revision == (4 if expected else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nodes", "current_node_type"),
    [
        ([], None),
        ([{"id": "node-1", "type": "video_generate"}], "video_generate"),
    ],
)
async def test_manual_selection_locks_canvas_and_rejects_stale_node(
    monkeypatch: pytest.MonkeyPatch,
    nodes: list[dict],
    current_node_type: str | None,
) -> None:
    async with _session() as db:
        canvas = CanvasDocument(
            id="canvas-selection",
            user_id="user-1",
            title="Selection guard",
            description="",
            graph_schema_version=1,
            graph_jsonb={"nodes": nodes, "edges": []},
            revision=1,
        )
        execution = _execution(
            execution_id="execution-selection",
            canvas_id=canvas.id,
            node_id="node-1",
            outputs=[{"type": "image", "image_id": "image-1"}],
        )
        db.add_all([canvas, execution])
        await db.commit()

        original_get_owned_canvas = selection_service.get_owned_canvas
        lock_values: list[bool | None] = []

        async def tracked_get_owned_canvas(*args, **kwargs):
            lock_values.append(kwargs.get("lock"))
            return await original_get_owned_canvas(*args, **kwargs)

        monkeypatch.setattr(
            selection_service,
            "get_owned_canvas",
            tracked_get_owned_canvas,
        )

        with pytest.raises(HTTPException) as excinfo:
            await selection_service.select_execution_output(
                db,
                user_id="user-1",
                canvas_id=canvas.id,
                execution_id=execution.id,
                body=CanvasSelectOutputIn(
                    output_index=0,
                    selection_revision=0,
                ),
            )

        error = excinfo.value.detail["error"]
        assert excinfo.value.status_code == 409
        assert error["code"] == "canvas_execution_stale"
        assert error["details"]["current_node_type"] == current_node_type
        assert lock_values == [True]
        selection = (
            await db.execute(
                select(CanvasNodeSelection).where(
                    CanvasNodeSelection.canvas_id == canvas.id
                )
            )
        ).scalar_one_or_none()
        assert selection is None


def _resolution_graph(
    *,
    execution_id: str,
    source_type: str,
    source_handle: str,
    edge_data_type: str,
    target_handle: str,
) -> dict:
    return {
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "config": {"text": "Render a clip"},
            },
            {"id": "source-1", "type": source_type, "config": {}},
            {
                "id": "target-1",
                "type": "video_generate",
                "config": {"mode": "reference"},
            },
        ],
        "edges": [
            {
                "id": "edge-prompt",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "target-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
                "order": 0,
            },
            {
                "id": "edge-output",
                "source_node_id": "source-1",
                "source_handle": source_handle,
                "target_node_id": "target-1",
                "target_handle": target_handle,
                "data_type": edge_data_type,
                "binding_mode": "pinned",
                "pinned_execution_id": execution_id,
                "pinned_output_index": 0,
                "order": 0,
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "source_handle", "edge_data_type", "target_handle"),
    [
        ("video_generate", "video", "video", "reference_videos"),
        ("image_generate", "image", "image", "reference_videos"),
    ],
)
async def test_execution_output_must_match_edge_and_target_handle_contract(
    source_type: str,
    source_handle: str,
    edge_data_type: str,
    target_handle: str,
) -> None:
    async with _session() as db:
        execution = _execution(
            execution_id="execution-output-contract",
            canvas_id="canvas-resolution",
            node_id="source-1",
            node_type=source_type,
            outputs=[{"type": "image", "image_id": "image-resolution"}],
        )
        db.add_all([execution, _image(image_id="image-resolution")])
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await resolve_node(
                db,
                user=SimpleNamespace(id="user-1"),
                canvas_id="canvas-resolution",
                graph=_resolution_graph(
                    execution_id=execution.id,
                    source_type=source_type,
                    source_handle=source_handle,
                    edge_data_type=edge_data_type,
                    target_handle=target_handle,
                ),
                node_id="target-1",
            )

        error = excinfo.value.detail["error"]
        assert excinfo.value.status_code == 422
        assert error["code"] == "canvas_input_type_mismatch"
        assert error["details"]["edge_id"] == "edge-output"


@pytest.mark.asyncio
async def test_video_reference_node_requires_reference_media_at_resolution() -> None:
    graph = {
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "config": {"text": "Render a clip"},
            },
            {
                "id": "target-1",
                "type": "video_reference_generate",
                "config": {"mode": "reference"},
            },
        ],
        "edges": [
            {
                "id": "edge-prompt",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "target-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
            }
        ],
    }
    async with _session() as db:
        with pytest.raises(HTTPException) as excinfo:
            await resolve_node(
                db,
                user=SimpleNamespace(id="user-1"),
                canvas_id="canvas-resolution",
                graph=graph,
                node_id="target-1",
            )

    error = excinfo.value.detail["error"]
    assert excinfo.value.status_code == 422
    assert error["code"] == "canvas_input_unresolved"
    assert error["details"]["target_handle"] == "reference_images|reference_videos"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mask_node_type", "mask_source_handle"),
    [
        ("image_asset", "image"),
        ("mask_asset", "mask"),
    ],
)
async def test_resolution_accepts_image_or_mask_assets_for_mask_inputs(
    mask_node_type: str,
    mask_source_handle: str,
) -> None:
    graph = {
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "config": {"text": "Repair the selected region"},
            },
            {
                "id": "source-1",
                "type": "image_asset",
                "config": {"image_id": "source-image"},
            },
            {
                "id": "mask-1",
                "type": mask_node_type,
                "config": {"image_id": "mask-image"},
            },
            {
                "id": "target-1",
                "type": "image_inpaint",
                "config": {},
            },
        ],
        "edges": [
            {
                "id": "edge-prompt",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "target-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
                "order": 0,
            },
            {
                "id": "edge-source",
                "source_node_id": "source-1",
                "source_handle": "image",
                "target_node_id": "target-1",
                "target_handle": "source",
                "data_type": "image",
                "binding_mode": "follow_active",
                "order": 0,
            },
            {
                "id": "edge-mask",
                "source_node_id": "mask-1",
                "source_handle": mask_source_handle,
                "target_node_id": "target-1",
                "target_handle": "mask",
                "data_type": "mask",
                "binding_mode": "follow_active",
                "order": 0,
            },
        ],
    }
    async with _session() as db:
        db.add_all(
            [
                _image(image_id="source-image"),
                _image(image_id="mask-image"),
            ]
        )
        await db.commit()

        resolved = await resolve_node(
            db,
            user=SimpleNamespace(id="user-1"),
            canvas_id="canvas-resolution",
            graph=graph,
            node_id="target-1",
        )

    assert resolved.images_by_handle["source"][0]["image_id"] == "source-image"
    assert resolved.images_by_handle["mask"][0]["image_id"] == "mask-image"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_handle", "target_handle", "edge_data_type"),
    [
        ("image", "mask", "mask"),
        ("mask", "source", "image"),
    ],
)
async def test_resolution_rejects_mask_asset_unknown_or_image_output_contracts(
    source_handle: str,
    target_handle: str,
    edge_data_type: str,
) -> None:
    graph = {
        "nodes": [
            {
                "id": "mask-1",
                "type": "mask_asset",
                "config": {"image_id": "mask-image"},
            },
            {
                "id": "target-1",
                "type": "image_inpaint" if target_handle == "mask" else "image_edit",
                "config": {},
            },
        ],
        "edges": [
            {
                "id": "edge-mask",
                "source_node_id": "mask-1",
                "source_handle": source_handle,
                "target_node_id": "target-1",
                "target_handle": target_handle,
                "data_type": edge_data_type,
                "binding_mode": "follow_active",
                "order": 0,
            }
        ],
    }
    async with _session() as db:
        with pytest.raises(HTTPException) as excinfo:
            await resolve_node(
                db,
                user=SimpleNamespace(id="user-1"),
                canvas_id="canvas-resolution",
                graph=graph,
                node_id="target-1",
            )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_input_type_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_kind", ["image", "video"])
async def test_asset_delete_guard_covers_unmaterialized_execution_outputs(
    asset_kind: str,
) -> None:
    async with _session() as db:
        owner_id = f"{asset_kind}-generation-1"
        if asset_kind == "image":
            asset = _image(
                image_id="image-delete",
                owner_generation_id=owner_id,
            )
            task = _execution_task(
                execution_id="execution-delete",
                owner_id=owner_id,
            )
            call = {"image_id": asset.id}
        else:
            asset = _video(
                video_id="video-delete",
                owner_generation_id=owner_id,
            )
            task = _execution_task(
                execution_id="execution-delete",
                owner_id=owner_id,
                task_kind="video_generation",
            )
            call = {"video_id": asset.id}
        db.add_all([asset, task])
        await db.commit()

        assert (
            await db.execute(select(CanvasAssetRef.id))
        ).scalar_one_or_none() is None
        with pytest.raises(HTTPException) as excinfo:
            await ensure_asset_not_canvas_referenced(db, **call)

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["error"]["code"] == "canvas_asset_referenced"
