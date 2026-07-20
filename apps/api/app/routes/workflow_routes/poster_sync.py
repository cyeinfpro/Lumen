"""State synchronization entry point for poster workflow outputs."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import WorkflowRun

from .poster_sync_steps import (
    PosterSyncHooks,
    sync_copy_analysis_step,
    sync_master_outputs,
    sync_render_outputs,
)


async def sync_poster_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    workflow_type: str,
    hooks: PosterSyncHooks,
) -> None:
    """Advance copy, master, and multi-size output state from durable tasks."""
    if run.type != workflow_type:
        return
    steps = {step.step_key: step for step in await hooks.load_steps(db, run.id)}
    await sync_copy_analysis_step(db, run, steps, hooks)
    await sync_master_outputs(db, run, steps, hooks)
    await sync_render_outputs(db, run, steps, hooks)
