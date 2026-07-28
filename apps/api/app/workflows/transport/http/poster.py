"""Thin HTTP routes for poster workflows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from lumen_core.schemas import (
    CopyAnalysisApproveIn,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMastersCreateIn,
    PosterRendersCreateIn,
    PosterReviseIn,
    WorkflowRunOut,
)

from ....db import get_db
from ....deps import CurrentUser, verify_csrf
from ...composition import WorkflowApplication
from .dependencies import get_workflow_application
from .execution import execute_workflow_action

router = APIRouter()


@router.post(
    "/poster-design",
    response_model=PosterDesignWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_design_workflow(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    body: PosterDesignWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> PosterDesignWorkflowCreateOut:
    return await execute_workflow_action(
        application.require_http().create_poster_design_workflow,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/steps/copy-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_copy_analysis(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: CopyAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().approve_copy_analysis,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/masters",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_masters(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: PosterMastersCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().create_poster_masters,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/masters/{master_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_poster_master(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    master_id: str,
    body: PosterMasterApproveIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().approve_poster_master,
        workflow_run_id=workflow_run_id,
        master_id=master_id,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/renders",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_renders(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: PosterRendersCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().create_poster_renders,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/renders/{render_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_poster_render(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    render_id: str,
    body: PosterReviseIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().revise_poster_render,
        workflow_run_id=workflow_run_id,
        render_id=render_id,
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/{workflow_run_id}/renders/{render_id}/inpaint",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def inpaint_poster_render(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    render_id: str,
    body: PosterInpaintIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().inpaint_poster_render,
        workflow_run_id=workflow_run_id,
        render_id=render_id,
        body=body,
        user=user,
        db=db,
    )
