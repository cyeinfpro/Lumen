"""ARQ entrypoint and reconciliation schedule for Pi Agent runs."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from ..agent_billing_corrections import correct_agent_unknown_charges
from ..agent_runtime_client import AgentRuntimeClient
from .agent_run_parts import orchestrate_agent_run, reconcile_agent_runs


async def run_agent(ctx: dict[str, Any], task_id: str) -> None:
    if not isinstance(ctx.get("agent_runtime_client"), AgentRuntimeClient):
        raise TypeError("ctx['agent_runtime_client'] must be AgentRuntimeClient")
    await orchestrate_agent_run(ctx, task_id)


cron_jobs = (
    cron(
        reconcile_agent_runs,
        minute=set(range(60)),
        second=41,
        run_at_startup=False,
        timeout=30,
    ),
)


__all__ = [
    "correct_agent_unknown_charges",
    "cron_jobs",
    "reconcile_agent_runs",
    "run_agent",
]
