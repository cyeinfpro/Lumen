from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from lumen_core.schemas import (
    ApparelWorkflowCreateIn,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibrarySaveJobItemIn,
    PosterMastersCreateIn,
)

from app.routes import workflows
from app.routes.workflow_routes import apparel, model_library, poster


EXTRACTED_ROUTE_ORDER = [
    ("POST", "/workflows/apparel-model-showcase", "create_apparel_model_showcase"),
    ("GET", "/workflows/apparel-model-library", "list_apparel_model_library"),
    (
        "POST",
        "/workflows/apparel-model-library/sync-presets",
        "sync_apparel_model_library_presets",
    ),
    (
        "GET",
        "/workflows/apparel-model-library/items/{item_id:path}/binary",
        "get_apparel_model_library_item_binary",
    ),
    (
        "GET",
        "/workflows/apparel-model-library/items/{item_id:path}/thumb",
        "get_apparel_model_library_item_thumb",
    ),
    (
        "POST",
        "/workflows/apparel-model-library/items",
        "create_apparel_model_library_item",
    ),
    (
        "PATCH",
        "/workflows/apparel-model-library/items/{item_id:path}",
        "patch_apparel_model_library_item",
    ),
    (
        "DELETE",
        "/workflows/apparel-model-library/items/{item_id:path}",
        "delete_apparel_model_library_item",
    ),
    (
        "POST",
        "/workflows/apparel-model-library/items/batch-delete",
        "batch_delete_apparel_model_library_items",
    ),
    (
        "POST",
        "/workflows/{workflow_run_id}/steps/product-analysis/approve",
        "approve_product_analysis",
    ),
    (
        "POST",
        "/workflows/{workflow_run_id}/model-candidates",
        "create_model_candidates",
    ),
    (
        "POST",
        "/workflows/{workflow_run_id}/model-library/select",
        "select_apparel_model_library_item",
    ),
    ("POST", "/workflows/apparel-model-library/generate", "generate_apparel_model_library_job"),
    ("GET", "/workflows/apparel-model-library/jobs", "list_apparel_model_library_jobs"),
    (
        "DELETE",
        "/workflows/apparel-model-library/jobs/{workflow_run_id}",
        "delete_apparel_model_library_job",
    ),
    ("DELETE", "/workflows/apparel-model-library/jobs", "clear_apparel_model_library_jobs"),
    (
        "POST",
        "/workflows/apparel-model-library/jobs/{workflow_run_id}/items/{image_id}/save",
        "save_apparel_model_library_job_item",
    ),
    (
        "POST",
        "/workflows/apparel-model-library/items/{item_id:path}/auto-tag",
        "auto_tag_apparel_model_library_item",
    ),
    ("POST", "/workflows/poster-design", "create_poster_design_workflow"),
    (
        "POST",
        "/workflows/{workflow_run_id}/steps/copy-analysis/approve",
        "approve_copy_analysis",
    ),
    ("POST", "/workflows/{workflow_run_id}/masters", "create_poster_masters"),
    (
        "POST",
        "/workflows/{workflow_run_id}/masters/{master_id}/approve",
        "approve_poster_master",
    ),
    ("POST", "/workflows/{workflow_run_id}/renders", "create_poster_renders"),
    (
        "POST",
        "/workflows/{workflow_run_id}/renders/{render_id}/revise",
        "revise_poster_render",
    ),
    (
        "POST",
        "/workflows/{workflow_run_id}/renders/{render_id}/inpaint",
        "inpaint_poster_render",
    ),
]


class _Result:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.added: list[Any] = []

    async def execute(self, _statement: Any) -> _Result:
        return _Result()

    def add(self, row: Any) -> None:
        self.added.append(row)
        if getattr(row, "id", None) is None:
            row.id = f"row-{len(self.added)}"

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.events.append("commit")


def test_extracted_routes_preserve_order_signatures_and_openapi_operations() -> None:
    extracted_names = {name for _, _, name in EXTRACTED_ROUTE_ORDER}
    routes = [
        route
        for route in workflows.router.routes
        if isinstance(route, APIRoute) and route.name in extracted_names
    ]

    assert [
        (next(iter(route.methods or set())), route.path, route.name) for route in routes
    ] == EXTRACTED_ROUTE_ORDER
    for route in routes:
        assert inspect.signature(getattr(workflows, route.name)) == inspect.signature(
            route.endpoint
        )
    assert inspect.signature(
        apparel.create_apparel_model_showcase
    ) == inspect.signature(workflows.create_apparel_model_showcase)

    app = FastAPI()
    app.include_router(workflows.router)
    schema = app.openapi()
    expected_operations = {
        ("post", "/workflows/apparel-model-showcase"): (
            "create_apparel_model_showcase_workflows_apparel_model_showcase_post"
        ),
        ("get", "/workflows/apparel-model-library"): (
            "list_apparel_model_library_workflows_apparel_model_library_get"
        ),
        ("post", "/workflows/{workflow_run_id}/model-library/select"): (
            "select_apparel_model_library_item_workflows__workflow_run_id__"
            "model_library_select_post"
        ),
        ("post", "/workflows/apparel-model-library/generate"): (
            "generate_apparel_model_library_job_workflows_"
            "apparel_model_library_generate_post"
        ),
        ("get", "/workflows/apparel-model-library/jobs"): (
            "list_apparel_model_library_jobs_workflows_"
            "apparel_model_library_jobs_get"
        ),
        ("post", "/workflows/poster-design"): (
            "create_poster_design_workflow_workflows_poster_design_post"
        ),
        ("post", "/workflows/{workflow_run_id}/masters"): (
            "create_poster_masters_workflows__workflow_run_id__masters_post"
        ),
        (
            "post",
            "/workflows/{workflow_run_id}/renders/{render_id}/inpaint",
        ): (
            "inpaint_poster_render_workflows__workflow_run_id__"
            "renders__render_id__inpaint_post"
        ),
    }
    for (method, path), operation_id in expected_operations.items():
        assert schema["paths"][path][method]["operationId"] == operation_id


def test_route_facades_honor_workflows_and_module_monkeypatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade_now = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)
    module_now = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(workflows, "_now", lambda: facade_now)
    facade_result = workflows._poster_merge_copy_corrections({}, {})  # noqa: SLF001
    assert facade_result["confirmed_at"] == facade_now.isoformat()

    monkeypatch.setattr(poster, "_now", lambda: module_now)
    module_result = poster._poster_merge_copy_corrections({}, {})  # noqa: SLF001
    assert module_result["confirmed_at"] == module_now.isoformat()

    monkeypatch.setattr(
        model_library,
        "_model_library_gender_label",
        lambda _genders: "测试",
    )
    assert "青年测试" in model_library._model_library_run_title(  # noqa: SLF001
        age_segment="young_adult",
        appearance_direction=None,
    )


@pytest.mark.asyncio
async def test_apparel_create_commits_before_outbox_publish_via_legacy_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    db = _Db(events)
    conv = SimpleNamespace(
        id="conv-1",
        archived=False,
        title="",
        last_activity_at=None,
    )
    product_step = SimpleNamespace(task_ids=[])

    async def fake_validate(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["image-1"]

    async def fake_conversation(*_args: Any, **_kwargs: Any) -> Any:
        return conv

    async def fake_step(*_args: Any, **_kwargs: Any) -> Any:
        return product_step

    async def fake_create_task(**_kwargs: Any) -> tuple[Any, str, list[str]]:
        return SimpleNamespace(), "completion-1", []

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        assert events == ["commit"]
        events.append("publish")

    monkeypatch.setattr(workflows, "_validate_owned_images", fake_validate)
    monkeypatch.setattr(
        workflows,
        "_get_or_create_workflow_conversation",
        fake_conversation,
    )
    monkeypatch.setattr(workflows, "_seed_steps", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(workflows, "_create_workflow_task", fake_create_task)
    monkeypatch.setattr(workflows, "_publish_bundles", fake_publish)

    result = await workflows.create_apparel_model_showcase(
        ApparelWorkflowCreateIn(product_image_ids=["image-1"]),
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert result.workflow_run_id == "row-1"
    assert product_step.task_ids == ["completion-1"]
    assert events == ["commit", "publish"]


@pytest.mark.asyncio
async def test_model_library_generate_commits_before_outbox_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    db = _Db(events)
    conv = SimpleNamespace(
        id="conv-1",
        archived=False,
        title="",
        last_activity_at=None,
    )
    job = SimpleNamespace(job_id="job-1")

    async def fake_conversation(*_args: Any, **_kwargs: Any) -> Any:
        return conv

    async def fake_enqueue(**kwargs: Any) -> tuple[list[Any], list[str]]:
        kwargs["step"].task_ids = ["gen-1"]
        return [SimpleNamespace()], ["gen-1"]

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        assert events == ["commit"]
        events.append("publish")

    async def fake_migrate(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def fake_saved(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    async def fake_get_run(*_args: Any, **kwargs: Any) -> Any:
        return next(
            row
            for row in db.added
            if type(row).__name__ == "WorkflowRun" and row.id == kwargs["run_id"]
        )

    async def fake_job(*_args: Any, **_kwargs: Any) -> Any:
        return job

    monkeypatch.setattr(
        workflows,
        "_get_or_create_workflow_conversation",
        fake_conversation,
    )
    monkeypatch.setattr(
        workflows,
        "_enqueue_model_library_generate_tasks",
        fake_enqueue,
    )
    monkeypatch.setattr(workflows, "_publish_bundles", fake_publish)
    monkeypatch.setattr(
        workflows,
        "_ensure_legacy_user_library_migrated",
        fake_migrate,
    )
    monkeypatch.setattr(workflows, "_saved_image_id_set", fake_saved)
    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_job_from_library_run", fake_job)

    result = await workflows.generate_apparel_model_library_job(
        ApparelModelLibraryGenerateIn(age_segment="young_adult", count=1),
        SimpleNamespace(id="user-1", account_mode="wallet"),
        db,  # type: ignore[arg-type]
    )

    assert result is job
    assert events == ["commit", "publish", "commit"]


@pytest.mark.asyncio
async def test_poster_generation_commits_before_outbox_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    db = _Db(events)
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        conversation_id="conv-1",
        type=workflows.POSTER_WORKFLOW_TYPE,
        status="needs_review",
        current_step="master_generation",
        quality_mode="premium",
        product_image_ids=[],
        metadata_jsonb={"style_summary": {}, "brand_assets": {}},
    )
    copy_step = SimpleNamespace(status="approved", output_json={"main_title": "Sale"})
    master_step = SimpleNamespace(
        status="waiting_input",
        task_ids=[],
        image_ids=[],
        input_json={},
        output_json={},
    )
    conv = SimpleNamespace(id="conv-1", last_activity_at=None)

    async def fake_get_run(*_args: Any, **_kwargs: Any) -> Any:
        return run

    async def fake_sync(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_step(_db: Any, _run_id: str, step_key: str) -> Any:
        return {
            "copy_analysis": copy_step,
            "master_generation": master_step,
        }[step_key]

    async def fake_conversation(*_args: Any, **_kwargs: Any) -> Any:
        return conv

    async def fake_create_task(**_kwargs: Any) -> tuple[Any, None, list[str]]:
        return SimpleNamespace(), None, ["gen-1"]

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        assert events == ["commit"]
        events.append("publish")

    async def fake_build(_db: Any, current_run: Any) -> Any:
        return current_run

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_sync_poster_workflow_outputs", fake_sync)
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(workflows, "_get_owned_conversation", fake_conversation)
    monkeypatch.setattr(workflows, "_create_poster_workflow_task", fake_create_task)
    monkeypatch.setattr(workflows, "_publish_bundles", fake_publish)
    monkeypatch.setattr(workflows, "_build_run_out", fake_build)

    result = await workflows.create_poster_masters(
        "run-1",
        PosterMastersCreateIn(candidate_count=1),
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert result is run
    assert events == ["commit", "publish", "commit"]


@pytest.mark.asyncio
async def test_model_library_background_task_is_registered_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    db = _Db(events)
    background = SimpleNamespace()

    def add_task(function: Any, *args: Any) -> None:
        assert events == ["commit"]
        events.append(f"background:{function.__name__}:{':'.join(args)}")

    background.add_task = add_task

    async def fake_get_run(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            id="run-1",
            type=workflows.WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        )

    async def fake_load_steps(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [SimpleNamespace(image_ids=["image-1"], task_ids=[])]

    async def fake_produced(*_args: Any, **_kwargs: Any) -> set[str]:
        return {"image-1"}

    async def fake_add(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "item-1"}

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    monkeypatch.setattr(workflows, "_workflow_produced_model_image_ids", fake_produced)
    monkeypatch.setattr(workflows, "_add_user_library_item", fake_add)
    monkeypatch.setattr(workflows, "_model_library_item_out", lambda item: item)

    result = await workflows.save_apparel_model_library_job_item(
        "run-1",
        "image-1",
        ApparelModelLibrarySaveJobItemIn(
            title="收藏模特",
            age_segment="young_adult",
            gender="female",
            auto_tag=True,
        ),
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
        background,  # type: ignore[arg-type]
    )

    assert result == {"id": "item-1"}
    assert events == [
        "commit",
        "background:_run_auto_tag_in_background:user-1:item-1",
    ]
