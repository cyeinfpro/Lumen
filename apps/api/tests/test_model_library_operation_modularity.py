from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.workflows.adapters.operations import model_library
from app.workflows.application import (
    model_library_generation,
    model_library_tagging,
    model_library_tasks,
)
from app.workflows.application import model_library_jobs
from app.workflows.ports import (
    model_library_tagging as model_library_tagging_ports,
)
from app.workflows.ports import model_library_tasks as model_library_task_ports
from app.workflows.ports.model_library_tagging import (
    ModelLibraryTagItem,
    ModelLibraryTagUpdate,
)
from app.workflows.ports.model_library_tasks import (
    ModelLibraryGenerationTask,
    ModelLibraryTaskResult,
)


class _TaskPort:
    def __init__(self) -> None:
        self.tasks: list[ModelLibraryGenerationTask] = []

    async def submit(
        self,
        task: ModelLibraryGenerationTask,
    ) -> ModelLibraryTaskResult:
        self.tasks.append(task)
        return ModelLibraryTaskResult(
            bundle=f"bundle-{task.task_index}",
            generation_ids=(f"gen-{task.task_index}",),
        )


class _TaggingPort:
    def __init__(self) -> None:
        self.update: ModelLibraryTagUpdate | None = None

    async def ensure_legacy_migrated(self, *, user_id: str) -> bool:
        return False

    async def load_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> ModelLibraryTagItem | None:
        return ModelLibraryTagItem(
            item_id=item_id,
            image_id="image-1",
            style_tags=("existing",),
            appearance_direction="user-value",
            age_segment="user_favorites",
            gender=None,
        )

    async def fetch_tags(
        self,
        *,
        user_id: str,
        image_id: str,
    ) -> dict[str, object]:
        return {
            "style_tags": ["existing", "editorial"],
            "appearance_direction": "provider-value",
            "age_segment": "young",
            "gender": "female",
        }

    async def save_update(
        self,
        *,
        user_id: str,
        item_id: str,
        update: ModelLibraryTagUpdate,
    ) -> None:
        self.update = update

    async def commit_migration(self) -> None:
        raise AssertionError("migration-only commit should not run")


@pytest.mark.asyncio
async def test_model_library_task_batch_is_orchestrated_by_application() -> None:
    port = _TaskPort()

    result = await model_library_tasks.generate_model_library_tasks(
        model_library_tasks.GenerateModelLibraryTasks(
            run_id="run-1234567890",
            workflow_action="model_library_generate",
            age_segment="young_adult",
            genders=("female", "male"),
            count_per_gender=2,
            appearance_direction="east_asian",
            extra_requirements=None,
            style_tags=("minimal",),
            auto_tag=True,
        ),
        port=port,
    )

    assert [task.task_index for task in port.tasks] == [1, 2, 3, 4]
    assert [task.gender for task in port.tasks] == [
        "female",
        "female",
        "male",
        "male",
    ]
    assert result.generation_ids == ("gen-1", "gen-2", "gen-3", "gen-4")


@pytest.mark.asyncio
async def test_model_library_auto_tag_use_case_owns_merge_policy() -> None:
    port = _TaggingPort()

    result = await model_library_tagging.auto_tag_model_library_item(
        user_id="user-1",
        item_id="item-1",
        age_segments={"young_adult", "user_favorites"},
        port=port,
    )

    assert result.style_tags == ("existing", "editorial")
    assert result.appearance_direction == "provider-value"
    assert port.update == ModelLibraryTagUpdate(
        style_tags=("existing", "editorial"),
        appearance_direction=None,
        age_segment="young_adult",
        gender="female",
        notes=None,
    )


def test_model_library_policy_owner_and_facade_budget() -> None:
    assert model_library._model_library_job_status.__module__ == (
        model_library_generation.__name__
    )

    source_path = Path(model_library.__file__)
    tree = ast.parse(source_path.read_text("utf-8"))
    aliases = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Name)
        and node.value.id.startswith("_")
    }
    assert aliases == set()


def test_model_library_application_and_ports_do_not_import_infrastructure() -> None:
    forbidden = ("sqlalchemy", "fastapi", "app.workflows.adapters")
    modules = (
        model_library_generation,
        model_library_jobs,
        model_library_tagging,
        model_library_tasks,
        model_library_tagging_ports,
        model_library_task_ports,
    )
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text("utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(
            imported.startswith(prefix) for imported in imports for prefix in forbidden
        )
