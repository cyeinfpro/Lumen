from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routes.workflow_routes import projects
from app.workflows.application.errors import InvalidWorkflowCursorError
from app.workflows.application.queries import ListWorkflowRuns
from app.workflows.ports.run_reads import (
    WorkflowRunListRecord,
    WorkflowRunReadPage,
)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self.rows


class _Db:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.responses.pop(0))


class _ReadPort:
    def __init__(self, pages: list[WorkflowRunReadPage]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def list_runs(self, **kwargs: Any) -> WorkflowRunReadPage:
        self.calls.append(kwargs)
        return self.pages.pop(0)


def _record(
    run_id: str,
    *,
    workflow_type: str = "poster_design",
    status: str = "needs_review",
    current_step: str = "multi_size_generation",
    updated_at: datetime | None = None,
) -> WorkflowRunListRecord:
    timestamp = updated_at or datetime(2026, 7, 11, 8, 30, tzinfo=timezone.utc)
    return WorkflowRunListRecord(
        id=run_id,
        conversation_id=None,
        type=workflow_type,
        status=status,
        title=run_id,
        user_prompt="海报",
        product_image_ids=(),
        current_step=current_step,
        quality_mode="premium",
        metadata_jsonb={},
        created_at=timestamp,
        updated_at=timestamp,
        output_count=2,
    )


def _row(run_id: str, updated_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        conversation_id=None,
        type="poster_design",
        status="needs_review",
        title=run_id,
        user_prompt="海报",
        product_image_ids=[],
        current_step="multi_size_generation",
        quality_mode="premium",
        metadata_jsonb={},
        created_at=updated_at,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_list_workflow_runs_owns_cursor_policy_and_next_action() -> None:
    updated_at = datetime(2026, 7, 11, 8, 30)
    port = _ReadPort(
        [
            WorkflowRunReadPage(
                items=(
                    _record("run-2", updated_at=updated_at),
                    _record(
                        "run-1",
                        status="completed",
                        current_step="delivery",
                        updated_at=updated_at,
                    ),
                ),
                has_more=True,
            ),
            WorkflowRunReadPage(items=(), has_more=False),
            WorkflowRunReadPage(items=(), has_more=False),
        ]
    )
    query = ListWorkflowRuns(port)

    first = await query.execute(
        user_id="user-1",
        workflow_type="poster_design",
        limit=2,
    )

    assert [item.next_action for item in first.items] == [
        "生成/确认多尺寸",
        "查看交付",
    ]
    assert first.next_cursor is not None
    assert port.calls[0]["excluded_types"] == ()

    second = await query.execute(
        user_id="user-1",
        workflow_type="poster_design",
        cursor=first.next_cursor,
        limit=2,
    )
    assert second.items == ()
    assert second.next_cursor is None
    assert port.calls[1]["after"].updated_at == updated_at.replace(tzinfo=timezone.utc)
    assert port.calls[1]["after"].run_id == "run-1"

    with pytest.raises(InvalidWorkflowCursorError):
        await query.execute(
            user_id="user-1",
            workflow_type="apparel_model_showcase",
            cursor=first.next_cursor,
            limit=2,
        )
    with pytest.raises(InvalidWorkflowCursorError):
        await query.execute(
            user_id="user-1",
            workflow_type="poster_design",
            cursor="not-a-valid-cursor",
            limit=2,
        )
    assert len(port.calls) == 2

    await query.execute(user_id="user-1", workflow_type=None, limit=50)
    assert port.calls[2]["excluded_types"] == (
        "apparel_model_library_generate",
        "poster_style_library_generate",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"v": 999, "type": "", "id": "run-1", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "wrong", "id": "run-1", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "", "id": "", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "", "id": "run-1", "updated_at": "2026-07-11T00:00:00"},
    ],
)
async def test_list_workflow_runs_rejects_invalid_cursor_payloads(
    payload: dict[str, object],
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidWorkflowCursorError):
        await ListWorkflowRuns(_ReadPort([])).execute(
            user_id="user-1",
            cursor=cursor,
        )


@pytest.mark.asyncio
async def test_list_workflows_route_uses_stable_sqlalchemy_page_boundary() -> None:
    updated_at = datetime(2026, 7, 11, 8, 30, tzinfo=timezone.utc)
    run_3, run_2, run_1 = (
        _row("run-3", updated_at),
        _row("run-2", updated_at),
        _row("run-1", updated_at),
    )
    db = _Db(
        [
            [run_3, run_2, run_1],
            [("run-3", ["img-3"]), ("run-2", ["img-2"])],
        ]
    )

    first_page = await projects.list_workflows(
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
        type="poster_design",
        cursor=None,
        limit=2,
    )

    assert [item.id for item in first_page.items] == ["run-3", "run-2"]
    assert [item.output_count for item in first_page.items] == [1, 1]
    assert [item.next_action for item in first_page.items] == [
        "生成/确认多尺寸",
        "生成/确认多尺寸",
    ]
    assert first_page.next_cursor is not None
    rendered = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ORDER BY workflow_runs.updated_at DESC, workflow_runs.id DESC" in rendered
    assert "LIMIT 3" in rendered

    next_db = _Db([[]])
    next_page = await projects.list_workflows(
        SimpleNamespace(id="user-1"),
        next_db,  # type: ignore[arg-type]
        type="poster_design",
        cursor=first_page.next_cursor,
        limit=2,
    )
    assert next_page.items == []
    assert next_page.next_cursor is None
    next_rendered = str(
        next_db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "workflow_runs.updated_at <" in next_rendered
    assert "workflow_runs.id < 'run-2'" in next_rendered

    boundary_db = _Db(
        [
            [run_3, run_2],
            [("run-3", []), ("run-2", [])],
        ]
    )
    boundary_page = await projects.list_workflows(
        SimpleNamespace(id="user-1"),
        boundary_db,  # type: ignore[arg-type]
        type="poster_design",
        cursor=None,
        limit=2,
    )
    assert boundary_page.next_cursor is None


@pytest.mark.asyncio
async def test_list_workflows_route_hides_background_projects_by_default() -> None:
    db = _Db([[]])

    out = await projects.list_workflows(
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
        type=None,
        cursor=None,
        limit=50,
    )

    assert out.items == []
    rendered = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "apparel_model_library_generate" in rendered
    assert "poster_style_library_generate" in rendered


@pytest.mark.asyncio
async def test_list_workflows_route_maps_invalid_cursor_to_http_422() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await projects.list_workflows(
            SimpleNamespace(id="user-1"),
            _Db([]),  # type: ignore[arg-type]
            type="poster_design",
            cursor="not-a-valid-cursor",
            limit=50,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"] == {
        "code": "invalid_cursor",
        "message": "cursor is invalid",
    }
