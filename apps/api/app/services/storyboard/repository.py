"""Storyboard persistence helpers independent of HTTP route modules."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Conversation, User, WorkflowRun, WorkflowStep

from .common import (
    STORYBOARD_WORKFLOW_TYPE,
    http_error,
    merge_run_metadata,
    step_kind,
)


async def get_owned_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise http_error("not_found", "conversation not found", 404)
    return conversation


async def get_or_create_storyboard_conversation(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
) -> Conversation:
    if run.conversation_id:
        conversation = await get_owned_conversation(
            db,
            user_id=user.id,
            conversation_id=run.conversation_id,
        )
    else:
        conversation = Conversation(
            user_id=user.id,
            title=run.title or "分镜项目",
            archived=True,
            default_params={},
        )
        db.add(conversation)
        await db.flush()
        run.conversation_id = conversation.id
    params = dict(conversation.default_params or {})
    params["workflow_type"] = STORYBOARD_WORKFLOW_TYPE
    params["hidden_from_conversations"] = True
    conversation.default_params = params
    conversation.title = run.title or conversation.title
    conversation.archived = True
    merge_run_metadata(run, {"conversation_id": conversation.id})
    return conversation


async def get_run(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    lock: bool = False,
) -> WorkflowRun:
    statement = select(WorkflowRun).where(
        WorkflowRun.id == run_id,
        WorkflowRun.user_id == user_id,
        WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
        WorkflowRun.deleted_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    run = (await db.execute(statement)).scalar_one_or_none()
    if run is None:
        raise http_error("not_found", "storyboard not found", 404)
    return run


async def load_steps(
    db: AsyncSession,
    run_id: str,
    *,
    lock: bool = False,
) -> list[WorkflowStep]:
    statement = (
        select(WorkflowStep)
        .where(WorkflowStep.workflow_run_id == run_id)
        .order_by(WorkflowStep.created_at.asc(), WorkflowStep.id.asc())
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await db.execute(statement)).scalars()
    return list(rows.all())


async def get_step(
    db: AsyncSession,
    run: WorkflowRun,
    step_id: str,
    *,
    kind: Literal["asset", "shot"] | None = None,
    lock: bool = False,
) -> WorkflowStep:
    statement = select(WorkflowStep).where(
        WorkflowStep.id == step_id,
        WorkflowStep.workflow_run_id == run.id,
    )
    if lock:
        statement = statement.with_for_update()
    step = (await db.execute(statement)).scalar_one_or_none()
    if step is None or (kind is not None and step_kind(step) != kind):
        raise http_error("not_found", "storyboard step not found", 404)
    return step


async def assembly_step(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    lock: bool = False,
) -> WorkflowStep:
    statement = select(WorkflowStep).where(
        WorkflowStep.workflow_run_id == run.id,
        WorkflowStep.step_key == "assembly",
    )
    if lock:
        statement = statement.with_for_update()
    step = (await db.execute(statement)).scalar_one_or_none()
    if step is None:
        step = WorkflowStep(
            workflow_run_id=run.id,
            step_key="assembly",
            status="waiting",
            input_json={},
            output_json={"segment_ids": []},
        )
        db.add(step)
        await db.flush()
    return step
