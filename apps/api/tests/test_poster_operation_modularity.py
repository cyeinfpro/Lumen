from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.workflows.adapters.operations import poster
from app.workflows.application import poster_design, poster_generation
from app.workflows.ports.poster_generation import (
    PosterMasterTask,
    PosterRenderTask,
    PosterTaskResult,
)


class _PosterPort:
    def __init__(self) -> None:
        self.masters: list[PosterMasterTask] = []
        self.renders: list[PosterRenderTask] = []

    async def submit_master(self, task: PosterMasterTask) -> PosterTaskResult:
        self.masters.append(task)
        return PosterTaskResult(
            bundle=f"master-{task.candidate_index}",
            generation_ids=(f"gen-{task.candidate_index}",),
        )

    async def submit_render(self, task: PosterRenderTask) -> PosterTaskResult:
        self.renders.append(task)
        return PosterTaskResult(
            bundle=f"render-{task.aspect_ratio}",
            generation_ids=(f"gen-{task.aspect_ratio}",),
        )


@pytest.mark.asyncio
async def test_poster_master_batch_is_orchestrated_by_application() -> None:
    port = _PosterPort()

    result = await poster_generation.generate_poster_masters(
        poster_generation.GeneratePosterMasters(
            run_id="run-1234567890",
            existing_count=2,
            candidate_count=2,
            style_summary={"prompt_template": "editorial"},
            copy_analysis={"main_title": "Sale", "info_density": "low"},
            brand_assets={},
            brand_attachment_ids=(),
            quality_mode="premium",
            size_mode="fixed",
            size="2880x2880",
        ),
        port=port,
    )

    assert [task.candidate_index for task in port.masters] == [3, 4]
    assert [task.intent for task in port.masters] == [
        "text_to_image",
        "text_to_image",
    ]
    assert result.generation_ids == ("gen-3", "gen-4")


def test_poster_policy_owner_and_facade_budget() -> None:
    assert poster_design.poster_master_prompt.__module__ == poster_design.__name__
    assert poster_design.poster_parse_copy_analysis_text.__module__ == (
        poster_design.__name__
    )

    source_path = Path(poster.__file__)
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
    assert "SimpleNamespace" not in source_path.read_text("utf-8")


def test_poster_application_and_port_do_not_import_infrastructure() -> None:
    forbidden = ("sqlalchemy", "fastapi", "app.workflows.adapters")
    for module in (poster_design, poster_generation):
        tree = ast.parse(Path(module.__file__).read_text("utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(
            imported.startswith(prefix) for imported in imports for prefix in forbidden
        )
