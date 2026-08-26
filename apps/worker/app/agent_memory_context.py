"""Isolated Agent memory assembly with explicit degradation state."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import AgentRun, Conversation, Message, User

from .db import SessionLocal
from .tasks import memory_extraction


logger = logging.getLogger(__name__)


def _user_text(message: Message) -> str:
    content = message.content if isinstance(message.content, dict) else {}
    value = content.get("text")
    return value.strip()[:20_000] if isinstance(value, str) else ""


async def memory_context(
    db: AsyncSession,
    *,
    run: AgentRun,
    conversation: Conversation,
    current_user: Message,
    redis: Any,
) -> tuple[
    list[str],
    list[dict[str, str]],
    str,
    str,
    Literal["disabled", "empty", "ready", "degraded"],
]:
    if conversation.memory_disabled:
        return [], [], "", "", "disabled"
    if isinstance(db, AsyncSession):
        user = await db.get(User, run.user_id)
        if user is not None and (
            user.memory_disabled or getattr(user, "memory_paused", False)
        ):
            return [], [], "", "", "disabled"
    assembled = None
    for attempt in range(2):
        try:
            if isinstance(db, AsyncSession):
                async with SessionLocal() as memory_db:
                    assembled = await memory_extraction.assemble_user_memory_prompt(
                        memory_db,
                        user_id=run.user_id,
                        conversation_id=conversation.id,
                        user_text=_user_text(current_user),
                        redis=redis,
                        parent_user_message_id=current_user.id,
                    )
            else:
                assembled = await memory_extraction.assemble_user_memory_prompt(
                    db,
                    user_id=run.user_id,
                    conversation_id=conversation.id,
                    user_text=_user_text(current_user),
                    redis=redis,
                    parent_user_message_id=current_user.id,
                )
            break
        except Exception:
            logger.warning(
                "agent memory assembly failed run=%s attempt=%d",
                run.id,
                attempt + 1,
                exc_info=True,
            )
            if attempt == 0:
                await asyncio.sleep(0)
    if assembled is None:
        return [], [], "", "", "degraded"
    system_sections = "\n\n".join(
        section
        for section in (
            assembled.scope_hint_text,
            assembled.profile_text,
            assembled.constraints_text,
            assembled.confirmation_instruction,
        )
        if section
    )
    state: Literal["empty", "ready"] = (
        "ready"
        if assembled.used_memory_ids or system_sections or assembled.context_text
        else "empty"
    )
    return (
        list(assembled.used_memory_ids),
        list(assembled.used_memory_summary),
        system_sections,
        assembled.context_text or "",
        state,
    )


__all__ = ["memory_context"]
