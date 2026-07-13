from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.canvas_services.task_guard import is_canvas_task
from app.routes import tasks, videos


class _ScalarResult:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any:
        return self.row


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.execute_calls = 0

    async def execute(self, _statement: Any) -> _ScalarResult:
        self.execute_calls += 1
        return _ScalarResult(self.row)


def _canvas_request() -> dict[str, str]:
    return {
        "source": "canvas",
        "canvas_execution_id": "execution-1",
    }


@pytest.mark.asyncio
async def test_canvas_image_retry_is_rejected_before_mutation() -> None:
    generation = SimpleNamespace(
        id="generation-1",
        user_id="user-1",
        status="failed",
        upstream_request=_canvas_request(),
    )
    db = _Db(generation)

    with pytest.raises(HTTPException) as exc_info:
        await tasks.retry_generation(
            generation.id,
            SimpleNamespace(id="user-1"),
            db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "canvas_retry_requires_canvas"
    assert "回画布重新运行节点" in exc_info.value.detail["error"]["message"]
    assert generation.status == "failed"
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_canvas_video_retry_is_rejected_before_new_task_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_generation = SimpleNamespace(
        id="video-generation-1",
        user_id="user-1",
        status="failed",
        upstream_request=_canvas_request(),
    )
    db = _Db(video_generation)

    async def fail_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Canvas retry must not create a generic video task")

    monkeypatch.setattr(videos, "_create_video_generation_record", fail_create)

    with pytest.raises(HTTPException) as exc_info:
        await videos.retry_video_generation(
            video_generation.id,
            SimpleNamespace(),
            SimpleNamespace(id="user-1", account_mode="wallet"),
            db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "canvas_retry_requires_canvas"
    assert "回画布重新运行节点" in exc_info.value.detail["error"]["message"]
    assert video_generation.status == "failed"
    assert db.execute_calls == 1


def test_non_canvas_task_metadata_is_not_classified_as_canvas() -> None:
    image_task = SimpleNamespace(
        upstream_request={"source": "chat", "canvas_execution_id": "execution-1"}
    )
    video_task = SimpleNamespace(
        upstream_request={"source": "canvas", "canvas_execution_id": ""}
    )

    assert is_canvas_task(image_task) is False
    assert is_canvas_task(video_task) is False
