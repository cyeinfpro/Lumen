from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from sqlalchemy import func, select
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
from lumen_core.models import Base, VideoGeneration

from app.canvas_services.api_schemas import CanvasCreateIn, CanvasExecuteIn
from app.canvas_services.document_service import create_canvas
from app.canvas_services.execution_service import execute_node
from app.services import task_submission
from app.services.task_submission import CanvasImageSubmission


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


class _NestedTransaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_canvas_video_submission_preserves_outbox_publish_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Db:
        def begin_nested(self):
            return _NestedTransaction()

    async def create_record(*_args, **kwargs):
        kwargs["deferred_publish_payload"].update(
            {
                "task_id": "video-1",
                "user_id": "user-1",
                "kind": "video_generation",
                "outbox_id": "outbox-1",
            }
        )
        return SimpleNamespace(id="video-1")

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
        body=SimpleNamespace(),
        user=SimpleNamespace(),
        request=_request(),
        metadata={},
    )
    await task_submission.publish_canvas_video_task(submission=submission)

    assert submission.generation.id == "video-1"
    assert submission.publish_payload["outbox_id"] == "outbox-1"
    assert invalidated == ["user-1"]
    assert published == [submission.publish_payload]


@pytest.mark.asyncio
async def test_active_execution_reuses_run_for_different_idempotency_keys(
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
        second_run, second_execution = await execute_node(
            db,
            user=user,
            canvas_id=canvas.id,
            node_id="image-1",
            body=CanvasExecuteIn(document_revision=1, idempotency_key="execute-2"),
            header_idempotency_key="execute-2",
            request=_request(),
        )

        assert second_run.id == first_run.id
        assert second_execution.id == first_execution.id
        assert submission_calls == 1
        assert (
            await db.scalar(
                select(func.count()).select_from(CanvasRun).where(
                    CanvasRun.canvas_id == canvas.id
                )
            )
        ) == 1
        assert (
            await db.scalar(
                select(func.count()).select_from(CanvasNodeExecution).where(
                    CanvasNodeExecution.canvas_id == canvas.id,
                    CanvasNodeExecution.node_id == "image-1",
                )
            )
        ) == 1
        assert (
            await db.scalar(
                select(func.count()).select_from(CanvasExecutionTask).where(
                    CanvasExecutionTask.execution_id == first_execution.id
                )
            )
        ) == 1
