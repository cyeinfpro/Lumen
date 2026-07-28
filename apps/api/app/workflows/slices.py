"""Stateless assembly helpers for migrated workflow vertical slices."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import User

from .adapters.operations.projects import (
    build_project_lifecycle,
    build_upsert_workflow_project,
)
from .adapters.run_creation import SQLAlchemyWorkflowRunCreationAdapter
from .adapters.sqlalchemy_reads import SQLAlchemyWorkflowRunReadAdapter
from .application.create_run import CreateWorkflowRun
from .application.project_lifecycle import ProjectLifecycle
from .application.queries import ListWorkflowRuns
from .application.upsert_project import UpsertWorkflowProject


def list_workflow_runs(session: AsyncSession) -> ListWorkflowRuns:
    return ListWorkflowRuns(SQLAlchemyWorkflowRunReadAdapter(session))


def create_workflow_run(
    session: AsyncSession,
    user: User,
) -> CreateWorkflowRun:
    return CreateWorkflowRun(SQLAlchemyWorkflowRunCreationAdapter(session, user))


def upsert_workflow_project(session: AsyncSession) -> UpsertWorkflowProject:
    return build_upsert_workflow_project(session)


def project_lifecycle(session: AsyncSession) -> ProjectLifecycle:
    return build_project_lifecycle(session)


__all__ = [
    "create_workflow_run",
    "list_workflow_runs",
    "project_lifecycle",
    "upsert_workflow_project",
]
