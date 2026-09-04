"""Typed composition root for public Agent session operations."""

from dataclasses import dataclass
from typing import Any, Callable

from .message_submission import submit_agent_message
from .repository import (
    get_agent_session_out,
    get_owned_agent_session,
    list_agent_messages,
    list_agent_sessions,
    load_agent_run_out,
)
from .session_crud import (
    branch_agent_session,
    create_agent_session,
    delete_agent_session,
    patch_agent_session,
)


@dataclass(frozen=True, slots=True)
class AgentSessionServices:
    branch_agent_session: Callable[..., Any]
    create_agent_session: Callable[..., Any]
    delete_agent_session: Callable[..., Any]
    get_agent_session_out: Callable[..., Any]
    get_owned_agent_session: Callable[..., Any]
    list_agent_messages: Callable[..., Any]
    list_agent_sessions: Callable[..., Any]
    load_agent_run_out: Callable[..., Any]
    patch_agent_session: Callable[..., Any]
    submit_agent_message: Callable[..., Any]


agent_session_services = AgentSessionServices(
    branch_agent_session=branch_agent_session,
    create_agent_session=create_agent_session,
    delete_agent_session=delete_agent_session,
    get_agent_session_out=get_agent_session_out,
    get_owned_agent_session=get_owned_agent_session,
    list_agent_messages=list_agent_messages,
    list_agent_sessions=list_agent_sessions,
    load_agent_run_out=load_agent_run_out,
    patch_agent_session=patch_agent_session,
    submit_agent_message=submit_agent_message,
)


__all__ = ["AgentSessionServices", "agent_session_services"]
