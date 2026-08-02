"""Post-commit work that must not affect generation persistence."""

from __future__ import annotations

import asyncio
import logging

from .run_state import GenerationRunState
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


async def run_workflow_tagging(
    state: GenerationRunState,
    image_id: str,
    g: RunGenerationDeps,
) -> None:
    auto_tag = getattr(g.workflows, "auto_tag_generated_workflow_image", None)
    if auto_tag is None:
        return
    try:
        await auto_tag(
            session_factory=g.store.session,
            user_id=state.user_id,
            generation=state.generation,
            image_id=image_id,
        )
    except (Exception, asyncio.CancelledError) as exc:
        logger.info(
            "post-commit workflow tagging skipped task=%s image=%s err=%s",
            state.task_id,
            image_id,
            exc,
        )


async def enqueue_auto_title(state: GenerationRunState) -> None:
    if not state.conversation_id_for_title:
        return
    from ..auto_title import maybe_enqueue_auto_title

    await maybe_enqueue_auto_title(
        state.redis,
        state.conversation_id_for_title,
    )
