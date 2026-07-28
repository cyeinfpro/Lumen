"""Standalone model-library generation task orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..domain.showcase_model_policy import model_diversity_anchor
from ..ports.model_library_tasks import (
    ModelLibraryGenerationTask,
    ModelLibraryTaskPort,
)
from .model_library_generation import model_library_generate_prompt


@dataclass(frozen=True, slots=True)
class GenerateModelLibraryTasks:
    run_id: str
    workflow_action: str
    age_segment: str
    genders: Sequence[str]
    count_per_gender: int
    appearance_direction: str | None
    extra_requirements: str | None
    style_tags: Sequence[str]
    auto_tag: bool
    reference_image_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelLibraryTaskBatchResult:
    bundles: tuple[object, ...]
    generation_ids: tuple[str, ...]


def clean_model_library_style_tags(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:32])
        if len(out) >= 12:
            break
    return out


async def generate_model_library_tasks(
    command: GenerateModelLibraryTasks,
    *,
    port: ModelLibraryTaskPort,
) -> ModelLibraryTaskBatchResult:
    bundles: list[object] = []
    generation_ids: list[str] = []
    task_index = 0
    style_tags = clean_model_library_style_tags(command.style_tags)
    for gender in command.genders:
        for candidate_index in range(1, command.count_per_gender + 1):
            task_index += 1
            prompt_index = 1 if command.reference_image_id else candidate_index
            prompt = model_library_generate_prompt(
                age_segment=command.age_segment,
                gender=gender,
                appearance_direction=command.appearance_direction,
                extra_requirements=command.extra_requirements,
                style_tags=style_tags,
                candidate_index=prompt_index,
                reference_mode=command.reference_image_id is not None,
                clean_style_tags=clean_model_library_style_tags,
                model_diversity_anchor=model_diversity_anchor,
            )
            task = ModelLibraryGenerationTask(
                task_index=task_index,
                gender=gender,
                candidate_index=candidate_index,
                intent=(
                    "image_to_image" if command.reference_image_id else "text_to_image"
                ),
                prompt=prompt,
                attachment_ids=(
                    (command.reference_image_id,) if command.reference_image_id else ()
                ),
                idempotency_key=(
                    f"mlib:{command.run_id[:24]}:{gender}:{candidate_index}"
                ),
                workflow_meta={
                    "workflow_action": command.workflow_action,
                    "workflow_candidate_index": task_index,
                    "workflow_model_library_mode": (
                        "reference_image" if command.reference_image_id else "text"
                    ),
                    "workflow_model_library_reference_image_id": (
                        command.reference_image_id or ""
                    ),
                    "workflow_model_library_age_segment": command.age_segment,
                    "workflow_model_library_gender": gender,
                    "workflow_model_library_appearance_direction": (
                        command.appearance_direction or ""
                    ),
                    "workflow_model_library_style_tags": style_tags,
                    "workflow_model_library_auto_tag": command.auto_tag,
                },
            )
            result = await port.submit(task)
            bundles.append(result.bundle)
            generation_ids.extend(result.generation_ids)
    return ModelLibraryTaskBatchResult(
        bundles=tuple(bundles),
        generation_ids=tuple(generation_ids),
    )
