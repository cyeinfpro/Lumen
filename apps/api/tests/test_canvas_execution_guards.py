from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, create_autospec

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
from lumen_core.models import Base, Image, OutboxEvent, User, VideoGeneration
from lumen_core.schemas import VideoCreateIn, VideoModelOptionOut, VideoOptionsOut
from lumen_core.video_billing import VideoCostEstimate

from app.canvas_services import execution_service
from app.canvas_services.api_schemas import CanvasCreateIn, CanvasExecuteIn
from app.canvas_services.document_service import create_canvas
from app.canvas_services.execution_service import (
    _await_post_commit_publish,
    _image_task_inputs,
    _image_params,
    _video_body,
    execute_node,
)
from app.canvas_services.graph_resolution import ResolvedNode
from app.services import task_submission
from app.services.task_submission import CanvasImageSubmission
from app.services.task_submission import _canvas_message_attachments
from app.services.video import submission as video_submission


def _graph() -> dict:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "schema_version": 1,
                "title": "Prompt",
                "position": {"x": 0, "y": 0},
                "config": {"text": "A product poster", "locked": False},
                "ui": {},
            },
            {
                "id": "image-1",
                "type": "image_generate",
                "schema_version": 1,
                "title": "Image generation",
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
        User.__table__,
        Image.__table__,
        VideoGeneration.__table__,
        OutboxEvent.__table__,
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
        session.add(
            User(
                id="user-1",
                email="user@example.com",
                account_mode="wallet",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


def _request() -> Request:
    return Request(
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


def test_canvas_submission_advisory_key_is_stable_signed_int64() -> None:
    from app.idempotency.advisory import advisory_lock_key

    first = advisory_lock_key("canvas-submission", "user-1", "key-1")
    second = advisory_lock_key("canvas-submission", "user-1", "key-1")
    different = advisory_lock_key("canvas-submission", "user-1", "key-2")

    assert first == second
    assert first != different
    assert -(2**63) <= first < 2**63


@pytest.mark.asyncio
async def test_execute_node_locks_and_rechecks_before_canvas_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    winner = (SimpleNamespace(id="run-1"), SimpleNamespace(id="execution-1"))
    lookup_results = [None, winner]

    async def fake_idempotent_run(*_args, **_kwargs):
        events.append("lookup")
        return lookup_results.pop(0)

    async def fake_lock(*_args, **_kwargs) -> None:
        events.append("lock")

    async def fail_canvas_load(*_args, **_kwargs):
        raise AssertionError("Canvas load must not run after lock recheck finds winner")

    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "_idempotent_run",
        fake_idempotent_run,
    )
    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "lock_user_key",
        fake_lock,
    )
    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "get_owned_canvas",
        fail_canvas_load,
    )

    result = await execute_node(
        object(),  # type: ignore[arg-type]
        user=SimpleNamespace(id="user-1"),
        canvas_id="canvas-1",
        node_id="image-1",
        body=CanvasExecuteIn(document_revision=1, idempotency_key="same-key"),
        header_idempotency_key="same-key",
        request=_request(),
    )

    assert result == winner
    assert events == ["lookup", "lock", "lookup"]


@pytest.mark.asyncio
async def test_canvas_submission_advisory_lock_is_noop_on_sqlite() -> None:
    from app.idempotency.advisory import lock_user_key

    async with _session() as db:
        await lock_user_key(db, "canvas-submission", "user-1", "same-key")


@pytest.mark.asyncio
async def test_canvas_submission_advisory_lock_uses_postgres_xact_lock() -> None:
    from app.idempotency.advisory import lock_user_key

    statements = []

    class PostgresDb:
        async def connection(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement):
            statements.append(statement)

    await lock_user_key(
        PostgresDb(),  # type: ignore[arg-type]
        "canvas-submission",
        "user-1",
        "same-key",
    )

    assert len(statements) == 1
    rendered = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in rendered


@pytest.mark.asyncio
async def test_canvas_run_integrity_error_recovers_committed_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictDb:
        rolled_back = False

        async def flush(self) -> None:
            raise IntegrityError("insert canvas run", {}, RuntimeError("duplicate"))

        async def rollback(self) -> None:
            self.rolled_back = True

    winner = (SimpleNamespace(id="run-1"), SimpleNamespace(id="execution-1"))

    async def fake_idempotent_run(*_args, **_kwargs):
        return winner

    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "_idempotent_run",
        fake_idempotent_run,
    )
    db = ConflictDb()

    result = await execution_service._flush_run_or_recover(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        idempotency_key="same-key",
        request_fingerprint="fingerprint-1",
    )

    assert result == winner
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_canvas_run_integrity_error_without_winner_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictDb:
        async def flush(self) -> None:
            raise IntegrityError("insert canvas run", {}, RuntimeError("duplicate"))

        async def rollback(self) -> None:
            return None

    async def fake_idempotent_run(*_args, **_kwargs):
        return None

    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "_idempotent_run",
        fake_idempotent_run,
    )

    with pytest.raises(HTTPException) as excinfo:
        await execution_service._flush_run_or_recover(  # noqa: SLF001
            ConflictDb(),  # type: ignore[arg-type]
            user_id="user-1",
            idempotency_key="same-key",
            request_fingerprint="fingerprint-1",
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"]["code"] == "canvas_submission_unavailable"


@pytest.mark.asyncio
async def test_canvas_run_integrity_error_preserves_fingerprint_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictDb:
        async def flush(self) -> None:
            raise IntegrityError("insert canvas run", {}, RuntimeError("duplicate"))

        async def rollback(self) -> None:
            return None

    async def fake_idempotent_run(*_args, **_kwargs):
        raise execution_service.idempotency_conflict()

    monkeypatch.setitem(
        execution_service.execute_node.__globals__,
        "_idempotent_run",
        fake_idempotent_run,
    )

    with pytest.raises(HTTPException) as excinfo:
        await execution_service._flush_run_or_recover(  # noqa: SLF001
            ConflictDb(),  # type: ignore[arg-type]
            user_id="user-1",
            idempotency_key="same-key",
            request_fingerprint="fingerprint-2",
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_post_commit_publish_timeout_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked_publish() -> None:
        await asyncio.Event().wait()

    monkeypatch.setitem(
        _await_post_commit_publish.__globals__,
        "_POST_COMMIT_PUBLISH_TIMEOUT_S",
        0.01,
    )
    await _await_post_commit_publish(
        "image",
        blocked_publish(),
        canvas_id="canvas-1",
        execution_id="execution-1",
    )


def _image(image_id: str) -> Image:
    return Image(
        id=image_id,
        user_id="user-1",
        source="upload",
        storage_key=f"images/{image_id}.webp",
        mime="image/webp",
        width=1024,
        height=1024,
        size_bytes=1024,
        sha256=image_id[0] * 64,
        visibility="private",
        metadata_jsonb={},
    )


def _special_image_graph(node_type: str) -> dict:
    nodes = [
        {
            "id": "prompt-a",
            "type": "prompt",
            "schema_version": 1,
            "title": "Prompt A",
            "position": {"x": 0, "y": 0},
            "config": {"text": "  Preserve subject  ", "locked": False},
            "ui": {},
        },
        {
            "id": "prompt-b",
            "type": "prompt",
            "schema_version": 1,
            "title": "Prompt B",
            "position": {"x": 0, "y": 100},
            "config": {"text": "increase detail", "locked": False},
            "ui": {},
        },
        {
            "id": "prompt-merge",
            "type": "prompt_merge",
            "schema_version": 1,
            "title": "Merge",
            "position": {"x": 160, "y": 0},
            "config": {
                "separator": " | ",
                "prefix": "[",
                "suffix": "]",
                "trim": True,
                "dedupe": True,
            },
            "ui": {"preset_id": "merge-default"},
        },
        {
            "id": "source",
            "type": "image_asset",
            "schema_version": 1,
            "title": "Source",
            "position": {"x": 0, "y": 220},
            "config": {"image_id": "source-image"},
            "ui": {},
        },
        {
            "id": "target",
            "type": node_type,
            "schema_version": 1,
            "title": "Target",
            "position": {"x": 360, "y": 0},
            "config": {},
            "ui": {},
        },
    ]
    edges = [
        {
            "id": "prompt-a-merge",
            "source_node_id": "prompt-a",
            "source_handle": "text",
            "target_node_id": "prompt-merge",
            "target_handle": "texts",
            "data_type": "text",
            "order": 0,
        },
        {
            "id": "prompt-b-merge",
            "source_node_id": "prompt-b",
            "source_handle": "text",
            "target_node_id": "prompt-merge",
            "target_handle": "texts",
            "data_type": "text",
            "order": 1,
        },
        {
            "id": "prompt-target",
            "source_node_id": "prompt-merge",
            "source_handle": "text",
            "target_node_id": "target",
            "target_handle": "prompt",
            "data_type": "text",
        },
        {
            "id": "source-target",
            "source_node_id": "source",
            "source_handle": "image",
            "target_node_id": "target",
            "target_handle": "source",
            "data_type": "image",
        },
    ]
    if node_type == "image_edit":
        nodes.append(
            {
                "id": "reference",
                "type": "image_asset",
                "schema_version": 1,
                "title": "Reference",
                "position": {"x": 0, "y": 320},
                "config": {"image_id": "reference-image"},
                "ui": {},
            }
        )
        edges.append(
            {
                "id": "reference-target",
                "source_node_id": "reference",
                "source_handle": "image",
                "target_node_id": "target",
                "target_handle": "references",
                "data_type": "image",
                "role": "product",
            }
        )
    if node_type == "image_inpaint":
        nodes.append(
            {
                "id": "mask",
                "type": "mask_asset",
                "schema_version": 1,
                "title": "Mask",
                "position": {"x": 0, "y": 320},
                "config": {"image_id": "mask-image"},
                "ui": {},
            }
        )
        edges.append(
            {
                "id": "mask-target",
                "source_node_id": "mask",
                "source_handle": "mask",
                "target_node_id": "target",
                "target_handle": "mask",
                "data_type": "mask",
            }
        )
    return {
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
        "frames": [],
        "settings": {"snap_to_grid": False, "grid_size": 16},
    }


class _NestedTransaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_type", "expected_attachments", "expected_mask"),
    [
        ("image_edit", ["source-image", "reference-image"], None),
        ("image_inpaint", ["source-image"], "mask-image"),
        ("image_upscale", ["source-image"], None),
    ],
)
async def test_special_image_nodes_use_existing_generation_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    node_type: str,
    expected_attachments: list[str],
    expected_mask: str | None,
) -> None:
    captured: dict = {}

    async def fake_create_image_task(*_args, **kwargs) -> CanvasImageSubmission:
        captured.update(kwargs)
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
        db.add(_image("source-image"))
        if node_type == "image_edit":
            db.add(_image("reference-image"))
        if node_type == "image_inpaint":
            db.add(_image("mask-image"))
        await db.commit()
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(
                title=f"Execute {node_type}",
                graph=_special_image_graph(node_type),
            ),
        )
        await execute_node(
            db,
            user=SimpleNamespace(
                id="user-1",
                email="user@example.com",
                account_mode="wallet",
            ),
            canvas_id=canvas.id,
            node_id="target",
            body=CanvasExecuteIn(
                document_revision=1,
                idempotency_key=f"execute-{node_type}",
            ),
            header_idempotency_key=f"execute-{node_type}",
            request=_request(),
        )

    assert captured["prompt"] == "[Preserve subject | increase detail]"
    assert captured["attachment_ids"] == expected_attachments
    assert captured["mask_image_id"] == expected_mask
    expected_roles = [{"image_id": "source-image", "role": "edit_target"}]
    if node_type == "image_edit":
        expected_roles.append({"image_id": "reference-image", "role": "product"})
    assert captured["metadata"]["attachment_roles"] == expected_roles
    if node_type == "image_upscale":
        assert captured["image_params"].quality == "2k"
        assert captured["image_params"].fast is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_type", "mode", "images", "videos"),
    [
        ("video_text_generate", "t2v", {}, {}),
        (
            "video_image_generate",
            "i2v",
            {"first_frame": [{"image_id": "first-frame"}]},
            {},
        ),
        (
            "video_reference_generate",
            "reference",
            {"reference_images": [{"image_id": "reference-image"}]},
            {"reference_videos": [{"video_id": "reference-video"}]},
        ),
    ],
)
async def test_dedicated_video_nodes_build_fixed_mode_requests(
    node_type: str,
    mode: str,
    images: dict[str, list[dict]],
    videos: dict[str, list[dict]],
) -> None:
    body = await _video_body(
        None,  # type: ignore[arg-type]
        user=SimpleNamespace(id="user-1"),
        resolved=ResolvedNode(
            node={
                "type": node_type,
                "config": {
                    "mode": mode,
                    "model": "video-model",
                    "duration_s": -1,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                },
            },
            prompt="Create a clip",
            images_by_handle=images,
            videos_by_handle=videos,
            snapshot={},
        ),
        idempotency_key=f"video-{mode}",
    )

    assert body.action == mode
    assert body.duration_s == -1
    assert body.input_image_id == ("first-frame" if mode == "i2v" else None)
    assert len(body.reference_media) == (2 if mode == "reference" else 0)


@pytest.mark.asyncio
async def test_canvas_video_auto_model_skips_reference_image_only_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_options(*_args, **_kwargs) -> VideoOptionsOut:
        return VideoOptionsOut(
            enabled=True,
            models=[
                VideoModelOptionOut(
                    model="image-only",
                    actions=["reference"],
                    resolutions=["720p"],
                    durations_s=[5],
                    reference_media_limits={"image": 9},
                ),
                VideoModelOptionOut(
                    model="image-and-video",
                    actions=["reference"],
                    resolutions=["720p"],
                    durations_s=[5],
                    reference_media_limits={"image": 9, "video": 3},
                ),
            ],
            durations_s=[5],
            resolutions=["720p"],
            aspect_ratios=["16:9"],
            generate_audio=True,
            pricing=[],
            hold_estimates={},
        )

    monkeypatch.setitem(_video_body.__globals__, "video_options", fake_options)
    body = await _video_body(
        None,  # type: ignore[arg-type]
        user=SimpleNamespace(id="user-1", account_mode="wallet"),
        resolved=ResolvedNode(
            node={
                "type": "video_reference_generate",
                "config": {
                    "mode": "reference",
                    "model": None,
                    "duration_s": 5,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                },
            },
            prompt="Keep the subject consistent",
            images_by_handle={},
            videos_by_handle={"reference_videos": [{"video_id": "reference-video"}]},
            snapshot={},
        ),
        idempotency_key="video-reference-auto-model",
    )

    assert body.model == "image-and-video"


@pytest.mark.asyncio
async def test_canvas_video_auto_model_honors_duration_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_options(*_args, **_kwargs) -> VideoOptionsOut:
        return VideoOptionsOut(
            enabled=True,
            models=[
                VideoModelOptionOut(
                    model="five-second-only",
                    actions=["t2v"],
                    resolutions=["720p"],
                    durations_s=[5],
                    durations_by_action_resolution={"t2v": {"720p": [5]}},
                ),
                VideoModelOptionOut(
                    model="ten-second-compatible",
                    actions=["t2v"],
                    resolutions=["720p"],
                    durations_s=[5, 10],
                    durations_by_action_resolution={"t2v": {"720p": [5, 10]}},
                ),
            ],
            durations_s=[5, 10],
            resolutions=["720p"],
            aspect_ratios=["16:9"],
            generate_audio=True,
            pricing=[],
            hold_estimates={},
        )

    monkeypatch.setitem(_video_body.__globals__, "video_options", fake_options)
    body = await _video_body(
        None,  # type: ignore[arg-type]
        user=SimpleNamespace(id="user-1", account_mode="wallet"),
        resolved=ResolvedNode(
            node={
                "type": "video_text_generate",
                "config": {
                    "mode": "t2v",
                    "model": None,
                    "duration_s": 10,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                },
            },
            prompt="Create a ten second clip",
            images_by_handle={},
            videos_by_handle={},
            snapshot={},
        ),
        idempotency_key="video-duration-auto-model",
    )

    assert body.model == "ten-second-compatible"
    assert body.duration_s == 10


def test_canvas_message_attachments_preserve_structured_roles() -> None:
    assert _canvas_message_attachments(
        ["source-image", "product-image"],
        {
            "attachment_roles": [
                {"image_id": "source-image", "role": "edit_target"},
                {"image_id": "product-image", "role": "product"},
            ]
        },
    ) == [
        {"image_id": "source-image", "role": "edit_target"},
        {"image_id": "product-image", "role": "product"},
    ]


def test_canvas_image_config_errors_are_structured_422() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _image_params({"aspect_ratio": "bogus"})

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_image_config_invalid"


@pytest.mark.parametrize(
    ("node_type", "images"),
    [
        (
            "image_generate",
            {"references": [{"image_id": f"reference-{index}"} for index in range(17)]},
        ),
        (
            "image_edit",
            {
                "source": [{"image_id": "source"}],
                "references": [
                    {"image_id": f"reference-{index}"} for index in range(16)
                ],
            },
        ),
    ],
)
def test_canvas_image_tasks_enforce_total_attachment_limit(
    node_type: str,
    images: dict[str, list[dict]],
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _image_task_inputs(
            node_type=node_type,
            resolved=ResolvedNode(
                node={"type": node_type, "config": {}},
                prompt="Edit",
                images_by_handle=images,
                videos_by_handle={},
                snapshot={},
            ),
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_input_cardinality_invalid"


@pytest.mark.asyncio
async def test_canvas_video_config_errors_are_structured_422() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _video_body(
            None,  # type: ignore[arg-type]
            user=SimpleNamespace(id="user-1"),
            resolved=ResolvedNode(
                node={
                    "type": "video_text_generate",
                    "config": {
                        "mode": "t2v",
                        "model": "video-model",
                        "duration_s": 5,
                        "resolution": "bogus",
                        "aspect_ratio": "16:9",
                    },
                },
                prompt="Create a clip",
                images_by_handle={},
                videos_by_handle={},
                snapshot={},
            ),
            idempotency_key="video-invalid",
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_video_config_invalid"


@pytest.mark.asyncio
async def test_merged_prompt_length_is_rejected_before_task_submission() -> None:
    graph = _graph()
    graph["nodes"][0]["config"]["text"] = "a" * 6_000
    graph["nodes"].insert(
        1,
        {
            "id": "prompt-2",
            "type": "prompt",
            "schema_version": 1,
            "title": "Prompt 2",
            "position": {"x": 0, "y": 180},
            "config": {"text": "b" * 6_000, "locked": False},
            "ui": {},
        },
    )
    graph["nodes"].insert(
        2,
        {
            "id": "prompt-merge",
            "type": "prompt_merge",
            "schema_version": 1,
            "title": "Prompt merge",
            "position": {"x": 180, "y": 90},
            "config": {},
            "ui": {},
        },
    )
    graph["edges"] = [
        {
            "id": "edge-prompt-1-merge",
            "source_node_id": "prompt-1",
            "source_handle": "text",
            "target_node_id": "prompt-merge",
            "target_handle": "texts",
            "data_type": "text",
            "binding_mode": "follow_active",
            "order": 0,
        },
        {
            "id": "edge-prompt-2-merge",
            "source_node_id": "prompt-2",
            "source_handle": "text",
            "target_node_id": "prompt-merge",
            "target_handle": "texts",
            "data_type": "text",
            "binding_mode": "follow_active",
            "order": 1,
        },
        {
            "id": "edge-merge-image",
            "source_node_id": "prompt-merge",
            "source_handle": "text",
            "target_node_id": "image-1",
            "target_handle": "prompt",
            "data_type": "text",
            "binding_mode": "follow_active",
            "order": 0,
        },
    ]
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="Long prompt", graph=graph),
        )
        with pytest.raises(HTTPException) as excinfo:
            await execute_node(
                db,
                user=SimpleNamespace(
                    id="user-1",
                    email="user@example.com",
                    account_mode="wallet",
                ),
                canvas_id=canvas.id,
                node_id="image-1",
                body=CanvasExecuteIn(
                    document_revision=1,
                    idempotency_key="execute-long-prompt",
                ),
                header_idempotency_key="execute-long-prompt",
                request=_request(),
            )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_prompt_too_long"


@pytest.mark.asyncio
async def test_non_executable_node_returns_422_before_input_resolution() -> None:
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "note-1",
                "type": "note",
                "schema_version": 1,
                "title": "Note",
                "position": {"x": 0, "y": 0},
                "config": {"text": "No execution needed", "tags": []},
                "ui": {},
            }
        ],
        "edges": [],
        "frames": [],
        "settings": {"snap_to_grid": False, "grid_size": 16},
    }
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="Note only", graph=graph),
        )
        with pytest.raises(HTTPException) as excinfo:
            await execute_node(
                db,
                user=SimpleNamespace(
                    id="user-1",
                    email="user@example.com",
                    account_mode="wallet",
                ),
                canvas_id=canvas.id,
                node_id="note-1",
                body=CanvasExecuteIn(
                    document_revision=1,
                    idempotency_key="execute-note",
                ),
                header_idempotency_key="execute-note",
                request=_request(),
            )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "canvas_node_not_executable"


@pytest.mark.asyncio
async def test_execution_rejects_malformed_stored_graph_before_resolution() -> None:
    async with _session() as db:
        canvas = await create_canvas(
            db,
            user_id="user-1",
            body=CanvasCreateIn(title="Malformed graph", graph=_graph()),
        )
        malformed = _graph()
        malformed["edges"][0]["order"] = {"invalid": True}
        canvas.graph_jsonb = malformed
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await execute_node(
                db,
                user=SimpleNamespace(
                    id="user-1",
                    email="user@example.com",
                    account_mode="wallet",
                ),
                canvas_id=canvas.id,
                node_id="image-1",
                body=CanvasExecuteIn(
                    document_revision=1,
                    idempotency_key="execute-malformed",
                ),
                header_idempotency_key="execute-malformed",
                request=_request(),
            )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_canvas_graph"


@pytest.mark.asyncio
async def test_canvas_video_submission_preserves_outbox_publish_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Db:
        def begin_nested(self):
            return _NestedTransaction()

    body = VideoCreateIn(
        action="t2v",
        model="seedance-2.0-fast",
        prompt="A product video",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="canvas-video-contract",
    )
    user = SimpleNamespace(id="user-1")
    request = _request()
    metadata = {"workflow_type": "infinite_canvas"}
    create_record = create_autospec(video_submission.create_video_generation_record)

    async def create_record_side_effect(
        db,
        submitted_body,
        submitted_user,
        *,
        context=None,
        services=None,
    ):
        assert isinstance(context, video_submission.VideoSubmissionContext)
        assert isinstance(services, video_submission.VideoSubmissionServices)
        assert context.request is request
        assert context.workflow_metadata == metadata
        assert context.defer_commit is True
        assert context.deferred_publish_payload is not None
        context.deferred_publish_payload.update(
            {
                "task_id": "video-1",
                "user_id": "user-1",
                "kind": "video_generation",
                "outbox_id": "outbox-1",
            }
        )
        return SimpleNamespace(id="video-1")

    create_record.side_effect = create_record_side_effect
    invalidated: list[str] = []
    published: list[dict] = []

    async def invalidate(user_id: str) -> None:
        invalidated.append(user_id)

    async def publish(payload: dict) -> None:
        published.append(dict(payload))

    monkeypatch.setattr(
        task_submission,
        "_create_video_generation_record",
        create_record,
    )
    monkeypatch.setattr(task_submission, "invalidate_balance_cache", invalidate)
    monkeypatch.setattr(task_submission, "publish_video_queued", publish)

    submission = await task_submission.create_canvas_video_task(
        Db(),  # type: ignore[arg-type]
        body=body,
        user=user,  # type: ignore[arg-type]
        request=request,
        metadata=metadata,
        active_user_snapshot=SimpleNamespace(
            user=user,
            account_mode="wallet",
        ),
    )
    await task_submission.publish_canvas_video_task(submission=submission)

    create_record.assert_awaited_once()
    assert submission.generation.id == "video-1"
    assert submission.publish_payload["outbox_id"] == "outbox-1"
    assert invalidated == ["user-1"]
    assert published == [submission.publish_payload]


@pytest.mark.asyncio
async def test_canvas_video_submission_defers_commit_and_publishes_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(
        name="canvas-provider",
        kind="test",
        upstream_model_for=lambda _model, _action: "upstream-video-model",
    )

    async def require_ready(_db, _body):
        return provider, {}

    async def public_base_loader(
        request,
        _db,
        _body,
        _fallback_snapshots,
        *,
        requires_public_media,
        prefers_public_media_url,
    ):
        assert request is not None
        assert requires_public_media is False
        assert prefers_public_media_url is False
        return None

    async def input_snapshot_loader(
        _db,
        *,
        user_id,
        image_id,
        fallback_snapshot,
        reference_public_base_url,
        required_public_media,
    ):
        assert user_id == "user-1"
        assert image_id is None
        assert fallback_snapshot is None
        assert reference_public_base_url is None
        assert required_public_media is False
        return None, None, None

    async def reference_snapshot_loader(
        _db,
        *,
        user_id,
        items,
        fallback_snapshots,
        reference_public_base_url,
        required_public_media,
    ):
        assert user_id == "user-1"
        assert items == []
        assert fallback_snapshots is None
        assert reference_public_base_url is None
        assert required_public_media is False
        return []

    def reference_validator(provider_kind, reference_snapshots):
        assert provider_kind == "test"
        assert reference_snapshots == []

    async def allow_negative_loader(_db):
        return False

    async def wallet_loader(_db, user_id, *, lock):
        assert user_id == "user-1"
        assert lock is False
        return SimpleNamespace(balance_micro=100)

    async def generation_renderer(_db, generation):
        return SimpleNamespace(id=generation.id)

    async def unexpected_publish(_value):
        raise AssertionError("video publish side effects must wait for commit")

    services = video_submission.VideoSubmissionServices(
        require_ready=require_ready,
        public_base_loader=public_base_loader,
        input_snapshot_loader=input_snapshot_loader,
        reference_snapshot_loader=reference_snapshot_loader,
        reference_validator=reference_validator,
        allow_negative_loader=allow_negative_loader,
        wallet_loader=wallet_loader,
        generation_renderer=generation_renderer,
        balance_invalidator=unexpected_publish,
        queued_publisher=unexpected_publish,
    )

    def canvas_video_services():
        return services

    async def estimate_video_cost(
        _db,
        *,
        model,
        action,
        resolution,
        duration_s,
        fps=None,
        generate_audio=False,
        estimates,
        pricing_variant=None,
        reference_media=None,
    ):
        assert model == "seedance-2.0-fast"
        assert action == "t2v"
        assert resolution == "720p"
        assert duration_s == 5
        assert fps is None
        assert generate_audio is True
        assert estimates == {}
        assert pricing_variant == "t2v_720p"
        assert reference_media is None
        return VideoCostEstimate(
            estimated_tokens=100,
            hold_micro=25,
            unit_price_micro=250_000,
            source="test",
        )

    async def hold(
        _db,
        user_id,
        amount_micro,
        *,
        ref_type,
        ref_id,
        idempotency_key,
        allow_negative=False,
        meta=None,
    ):
        assert user_id == "user-1"
        assert amount_micro == 25
        assert ref_type == "video_generation"
        assert ref_id
        assert idempotency_key == f"video_generation:hold:{ref_id}"
        assert allow_negative is False
        assert meta is not None
        return None

    invalidated: list[str] = []
    published: list[dict] = []
    transaction_events: list[str] = []

    async def invalidate(user_id: str) -> None:
        transaction_events.append("invalidate")
        invalidated.append(user_id)

    async def publish(payload: dict) -> None:
        transaction_events.append("publish")
        published.append(dict(payload))

    monkeypatch.setattr(
        task_submission,
        "VideoSubmissionServices",
        canvas_video_services,
    )
    monkeypatch.setattr(
        video_submission,
        "estimate_video_cost",
        estimate_video_cost,
    )
    monkeypatch.setattr(video_submission.billing_core, "hold", hold)
    monkeypatch.setattr(task_submission, "invalidate_balance_cache", invalidate)
    monkeypatch.setattr(task_submission, "publish_video_queued", publish)

    body = VideoCreateIn(
        action="t2v",
        model="seedance-2.0-fast",
        prompt="A committed Canvas video",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="canvas-video-transaction",
    )
    async with _session() as db:
        commit = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit)
        user = await db.get(User, "user-1")
        assert user is not None

        submission = await task_submission.create_canvas_video_task(
            db,
            body=body,
            user=user,
            request=_request(),
            metadata={
                "workflow_type": "infinite_canvas",
                "canvas_node_type": "video_generate",
            },
            active_user_snapshot=SimpleNamespace(
                user=user,
                account_mode="wallet",
            ),
        )

        commit.assert_not_awaited()
        assert invalidated == []
        assert published == []
        generation = (
            await db.execute(
                select(VideoGeneration).where(
                    VideoGeneration.id == submission.generation.id
                )
            )
        ).scalar_one()
        outbox = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.id == submission.publish_payload["outbox_id"]
                )
            )
        ).scalar_one()
        assert generation.upstream_request["workflow_type"] == "infinite_canvas"
        assert generation.upstream_request["canvas_node_type"] == "video_generate"
        assert str(outbox.id) == submission.publish_payload["outbox_id"]
        assert outbox.payload == {
            "task_id": submission.publish_payload["task_id"],
            "user_id": "user-1",
            "kind": "video_generation",
        }

        await db.commit()
        transaction_events.append("commit")
        await task_submission.publish_canvas_video_task(submission=submission)

    commit.assert_awaited_once()
    assert transaction_events == ["commit", "invalidate", "publish"]
    assert invalidated == ["user-1"]
    assert published == [submission.publish_payload]


@pytest.mark.asyncio
async def test_active_execution_rejects_a_different_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission_calls = 0

    async def fake_create_image_task(*_args, **_kwargs) -> CanvasImageSubmission:
        nonlocal submission_calls
        submission_calls += 1
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
            body=CanvasCreateIn(title="Execution guard", graph=_graph()),
        )
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            account_mode="wallet",
        )
        first_run, first_execution = await execute_node(
            db,
            user=user,
            canvas_id=canvas.id,
            node_id="image-1",
            body=CanvasExecuteIn(document_revision=1, idempotency_key="execute-1"),
            header_idempotency_key="execute-1",
            request=_request(),
        )
        with pytest.raises(HTTPException) as excinfo:
            await execute_node(
                db,
                user=user,
                canvas_id=canvas.id,
                node_id="image-1",
                body=CanvasExecuteIn(
                    document_revision=1,
                    idempotency_key="execute-2",
                ),
                header_idempotency_key="execute-2",
                request=_request(),
            )

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["error"]["code"] == "canvas_execution_active"
        assert excinfo.value.detail["error"]["details"]["run_id"] == first_run.id
        assert (
            excinfo.value.detail["error"]["details"]["execution_id"]
            == first_execution.id
        )
        assert submission_calls == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CanvasRun)
                .where(CanvasRun.canvas_id == canvas.id)
            )
        ) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CanvasNodeExecution)
                .where(
                    CanvasNodeExecution.canvas_id == canvas.id,
                    CanvasNodeExecution.node_id == "image-1",
                )
            )
        ) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CanvasExecutionTask)
                .where(CanvasExecutionTask.execution_id == first_execution.id)
            )
        ) == 1
