"""Canonical predicates separating Studio and Agent conversations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select

from lumen_core.model_entities import AgentSession, Conversation


def studio_conversation_filter(conversation_id: Any = None) -> Any:
    column = Conversation.id if conversation_id is None else conversation_id
    return ~exists(
        select(AgentSession.id).where(AgentSession.conversation_id == column)
    )


def agent_conversation_filter(conversation_id: Any = None) -> Any:
    column = Conversation.id if conversation_id is None else conversation_id
    return exists(
        select(AgentSession.id).where(AgentSession.conversation_id == column)
    )


def exclude_agent_conversations(statement: Any) -> Any:
    return statement.where(studio_conversation_filter())


__all__ = [
    "agent_conversation_filter",
    "exclude_agent_conversations",
    "studio_conversation_filter",
]
