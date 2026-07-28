from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.workflows.application.errors import WorkflowRequestError
from app.workflows.application.project_candidate_rules import (
    apply_accessory_selection_state,
    approve_model_candidate_state,
    reopen_model_selection_state,
    saved_library_item_ids,
)
from app.workflows.application.project_lifecycle import ProjectLifecycle
from app.workflows.application.upsert_project import (
    UpsertWorkflowProject,
    UpsertWorkflowProjectCommand,
)


class _ProjectPort:
    def __init__(self, run: Any) -> None:
        self.run = run
        self.events: list[str] = []
        self.renamed_title: str | None = None

    async def get_owned_run(
        self,
        *,
        user_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> Any:
        self.events.append(f"get:{user_id}:{run_id}:{for_update}")
        return self.run

    async def rename_active_conversation(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
    ) -> None:
        self.events.append(f"rename:{conversation_id}:{user_id}")
        self.renamed_title = title

    async def mark_active_conversation_deleted(
        self,
        *,
        conversation_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None:
        self.events.append(f"delete-conversation:{conversation_id}:{user_id}")

    async def sync_standard_outputs(self, run: Any) -> None:
        self.events.append("sync-standard")

    async def sync_poster_outputs(self, run: Any) -> None:
        self.events.append("sync-poster")

    async def build_run_out(self, run: Any) -> Any:
        self.events.append("build")
        return run

    async def soft_delete_generated_images(self, **_kwargs: Any) -> object:
        self.events.append("soft-delete")
        return {"generation_ids": ["gen-1"]}

    async def post_commit_generated_cleanup(self, **_kwargs: Any) -> None:
        self.events.append("post-commit-cleanup")

    async def attach_assets(self, **kwargs: Any) -> None:
        self.events.append(f"attach:{kwargs['source_step_key']}")

    async def commit(self) -> None:
        self.events.append("commit")


def _lifecycle(port: _ProjectPort) -> ProjectLifecycle:
    return ProjectLifecycle(
        repository=port,
        outputs=port,
        assets=port,
        now=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_project_lifecycle_owns_title_and_cleanup_transaction_order() -> None:
    run = SimpleNamespace(
        id="run-1",
        type="apparel_model_showcase",
        title="Old",
        conversation_id="conv-1",
        current_step="model_settings",
        deleted_at=None,
    )
    port = _ProjectPort(run)
    out = await UpsertWorkflowProject(
        repository=port,
        outputs=port,
    ).upsert_project(
        UpsertWorkflowProjectCommand(
            user_id="user-1",
            run_id="run-1",
            title="  New title  ",
        )
    )

    assert out is run
    assert run.title == "New title"
    assert port.renamed_title == "New title"
    assert port.events[-2:] == ["build", "commit"]

    port.events.clear()
    assert await _lifecycle(port).delete(
        user_id="user-1",
        run_id="run-1",
        account_mode="wallet",
    ) == {"ok": True}
    assert run.deleted_at == datetime(2026, 7, 28, tzinfo=timezone.utc)
    assert port.events[-2:] == ["commit", "post-commit-cleanup"]


@pytest.mark.asyncio
async def test_project_lifecycle_rejects_blank_title_before_commit() -> None:
    run = SimpleNamespace(
        id="run-1",
        type="apparel_model_showcase",
        title="Old",
        conversation_id=None,
        current_step="product_analysis",
        deleted_at=None,
    )
    port = _ProjectPort(run)

    with pytest.raises(WorkflowRequestError) as excinfo:
        await UpsertWorkflowProject(
            repository=port,
            outputs=port,
        ).upsert_project(
            UpsertWorkflowProjectCommand(
                user_id="user-1",
                run_id="run-1",
                title="   ",
            )
        )

    assert excinfo.value.status_code == 422
    assert "commit" not in port.events


def _step(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        approved_at=None,
        approved_by=None,
        input_json={},
        output_json={},
        task_ids=[],
        image_ids=[],
    )


def test_project_candidate_rules_apply_and_reset_state() -> None:
    selected = SimpleNamespace(
        id="candidate-1",
        status="ready",
        contact_sheet_image_id="image-1",
        selected_at=None,
        model_brief_json={},
    )
    rejected = SimpleNamespace(
        id="candidate-2",
        status="ready",
        contact_sheet_image_id="image-2",
        selected_at=None,
        model_brief_json={},
    )
    run = SimpleNamespace(current_step="model_candidates", status="needs_review")
    approval = _step("needs_review")
    showcase = _step("waiting_input")
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)

    approve_model_candidate_state(
        candidates=[selected, rejected],
        selected_candidate=selected,
        approval_step=approval,
        showcase_step=showcase,
        run=run,
        user_id="user-1",
        now=now,
        adjustments="keep identity",
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        selected_accessory_image_id=None,
    )

    assert selected.status == "selected"
    assert rejected.status == "rejected"
    assert approval.status == "approved"
    assert showcase.status == "needs_review"

    candidate_step = _step("completed")
    quality = _step("approved")
    delivery = _step("completed")
    reopen_model_selection_state(
        candidates=[selected, rejected],
        approval_step=approval,
        candidate_step=candidate_step,
        showcase_step=showcase,
        quality_step=quality,
        delivery_step=delivery,
        run=run,
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        style_prompt="clean studio",
    )

    assert [selected.status, rejected.status] == ["ready", "ready"]
    assert candidate_step.status == "needs_review"
    assert (showcase.status, quality.status, delivery.status) == (
        "waiting_input",
        "waiting_input",
        "waiting_input",
    )
    apply_accessory_selection_state(
        approval_step=approval,
        run=run,
        selected_accessory_image_id="accessory-1",
    )
    assert approval.output_json["selected_accessory_image_id"] == "accessory-1"
    assert saved_library_item_ids(["item-1", "item-1"], "item-2") == [
        "item-1",
        "item-2",
    ]
