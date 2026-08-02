from __future__ import annotations

from typing import Any

from . import workflow_hooks


def _load_model_library_tagger() -> workflow_hooks.ModelLibraryTagger:
    from ..model_library_tagging import auto_tag_image_record

    return auto_tag_image_record


def _load_poster_style_tagger() -> workflow_hooks.PosterStyleTagger:
    from ..poster_style_tagging import auto_tag_poster_style_image_record

    return auto_tag_poster_style_image_record


def _services() -> workflow_hooks.WorkflowHookServices:
    return workflow_hooks.WorkflowHookServices(
        model_library_tagger=_load_model_library_tagger(),
        poster_style_tagger=_load_poster_style_tagger(),
    )


async def record_model_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
) -> None:
    await workflow_hooks.maybe_record_model_library_generate_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        services=_services(),
    )


async def record_poster_style_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
) -> None:
    await workflow_hooks.maybe_record_poster_style_library_generate_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        services=_services(),
    )


async def auto_tag_generated_workflow_image(
    *,
    session_factory: Any,
    user_id: str,
    generation: Any,
    image_id: str,
) -> None:
    await workflow_hooks.maybe_auto_tag_generated_workflow_image(
        session_factory=session_factory,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        services=_services(),
    )


async def record_model_library_candidate_image(
    *,
    session: Any,
    user_id: str,
    parent_upstream_request: dict[str, Any],
    bonus_image_id: str,
) -> None:
    await workflow_hooks.maybe_record_model_library_candidate_image(
        session=session,
        user_id=user_id,
        parent_upstream_request=parent_upstream_request,
        bonus_image_id=bonus_image_id,
        services=_services(),
    )


async def record_poster_workflow_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
) -> None:
    await workflow_hooks.maybe_record_poster_workflow_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        services=_services(),
    )


model_library_requested_count_from_step = (
    workflow_hooks.model_library_requested_count_from_step
)
