from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.workflows.adapters.http_operations import (
    ApparelWorkflowOperationsAdapter,
    ModelLibraryWorkflowOperationsAdapter,
    PosterWorkflowOperationsAdapter,
    ProjectWorkflowOperationsAdapter,
)
from app.workflows.adapters.run_creation import SQLAlchemyWorkflowRunCreationAdapter
from app.workflows.adapters.operations import apparel, model_library, poster, projects
from app.workflows.application.create_run import CreateWorkflowRun
from app.workflows.application.http_operations import WorkflowHttpUseCases
from app.workflows.application.runtime_state import WorkflowRuntimeState
from app.workflows.ports.run_creation import (
    CreatePosterRunCommand,
    PosterBrandAssets,
    WorkflowRunCreated,
)


def _application() -> tuple[
    WorkflowHttpUseCases,
    ProjectWorkflowOperationsAdapter,
    ApparelWorkflowOperationsAdapter,
    ModelLibraryWorkflowOperationsAdapter,
    PosterWorkflowOperationsAdapter,
]:
    runtime = WorkflowRuntimeState()
    project_adapter = ProjectWorkflowOperationsAdapter(runtime)
    apparel_adapter = ApparelWorkflowOperationsAdapter(runtime)
    model_library_adapter = ModelLibraryWorkflowOperationsAdapter()
    poster_adapter = PosterWorkflowOperationsAdapter()
    return (
        WorkflowHttpUseCases(
            projects=project_adapter,
            apparel=apparel_adapter,
            model_library=model_library_adapter,
            poster=poster_adapter,
        ),
        project_adapter,
        apparel_adapter,
        model_library_adapter,
        poster_adapter,
    )


@pytest.mark.asyncio
async def test_project_http_cutover_preserves_adapter_result_and_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = {"id": "workflow-1", "status": "needs_review"}

    async def fake_get_workflow(**kwargs: Any) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(projects, "get_workflow", fake_get_workflow)
    application, adapter, *_ = _application()
    arguments = {
        "workflow_run_id": "workflow-1",
        "user": object(),
        "db": object(),
    }

    old_result = await adapter.get_workflow(**arguments)
    new_result = await application.get_workflow(**arguments)

    assert new_result == old_result == expected
    assert calls == [arguments, arguments]


@pytest.mark.asyncio
async def test_apparel_http_cutover_preserves_runtime_adapter_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = {"status": "updated", "added": 3}

    async def fake_sync(**kwargs: Any) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(apparel, "sync_apparel_model_library_presets", fake_sync)
    application, _, adapter, *_ = _application()
    arguments = {"user": object(), "db": object()}

    old_result = await adapter.sync_apparel_model_library_presets(**arguments)
    new_result = await application.sync_apparel_model_library_presets(**arguments)

    assert new_result == old_result == expected
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["user"] is arguments["user"]
    assert calls[0]["db"] is arguments["db"]
    assert calls[0]["runtime"] is adapter.runtime


@pytest.mark.asyncio
async def test_model_library_http_cutover_preserves_adapter_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = {"id": "job-1", "status": "running"}

    async def fake_generate(**kwargs: Any) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        model_library,
        "generate_apparel_model_library_job",
        fake_generate,
    )
    application, _, _, adapter, _ = _application()
    arguments = {"body": object(), "user": object(), "db": object()}

    old_result = await adapter.generate_apparel_model_library_job(**arguments)
    new_result = await application.generate_apparel_model_library_job(**arguments)

    assert new_result == old_result == expected
    assert calls == [arguments, arguments]


@pytest.mark.asyncio
async def test_poster_http_cutover_preserves_adapter_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = SimpleNamespace(
        workflow_run_id="poster-workflow-1",
        status="running",
        current_step="copy_analysis",
    )

    async def fake_create(*args: Any, **kwargs: Any) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return expected

    monkeypatch.setattr(poster, "create_poster_design_workflow", fake_create)
    adapter = SQLAlchemyWorkflowRunCreationAdapter(
        object(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
    )
    result = await CreateWorkflowRun(adapter).create_poster(
        CreatePosterRunCommand(
            user_id="user-1",
            conversation_id=None,
            copy_text="launch",
            style_id="style-1",
            target_aspects=("1:1",),
            brand_assets=PosterBrandAssets(None, None, None, None),
            quality_mode="premium",
            title=None,
        )
    )

    assert result == WorkflowRunCreated(
        workflow_run_id="poster-workflow-1",
        status="running",
        current_step="copy_analysis",
    )
    assert len(calls) == 1
