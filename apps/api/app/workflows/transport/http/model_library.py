"""Thin HTTP routes for model library workflows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from lumen_core.schemas import (
    ApparelModelLibraryAutoTagOut,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryJobOut,
    ApparelModelLibraryJobsClearOut,
    ApparelModelLibraryJobsOut,
    ApparelModelLibrarySaveJobItemIn,
)

from ....db import get_db
from ....deps import CurrentUser, verify_csrf
from ...composition import WorkflowApplication
from .dependencies import get_workflow_application
from .execution import execute_workflow_action

router = APIRouter()


@router.post(
    "/apparel-model-library/generate",
    response_model=ApparelModelLibraryJobOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_apparel_model_library_job(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    body: ApparelModelLibraryGenerateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibraryJobOut:
    return await execute_workflow_action(
        application.require_http().generate_apparel_model_library_job,
        body=body,
        user=user,
        db=db,
    )


@router.get("/apparel-model-library/jobs", response_model=ApparelModelLibraryJobsOut)
async def list_apparel_model_library_jobs(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ApparelModelLibraryJobsOut:
    return await execute_workflow_action(
        application.require_http().list_apparel_model_library_jobs,
        user=user,
        db=db,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/apparel-model-library/jobs/{workflow_run_id}", dependencies=[Depends(verify_csrf)]
)
async def delete_apparel_model_library_job(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> dict[str, bool]:
    return await execute_workflow_action(
        application.require_http().delete_apparel_model_library_job,
        workflow_run_id=workflow_run_id,
        user=user,
        db=db,
    )


@router.delete(
    "/apparel-model-library/jobs",
    response_model=ApparelModelLibraryJobsClearOut,
    dependencies=[Depends(verify_csrf)],
)
async def clear_apparel_model_library_jobs(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibraryJobsClearOut:
    return await execute_workflow_action(
        application.require_http().clear_apparel_model_library_jobs, user=user, db=db
    )


@router.post(
    "/apparel-model-library/jobs/{workflow_run_id}/items/{image_id}/save",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_apparel_model_library_job_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    image_id: str,
    body: ApparelModelLibrarySaveJobItemIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    return await execute_workflow_action(
        application.require_http().save_apparel_model_library_job_item,
        workflow_run_id=workflow_run_id,
        image_id=image_id,
        body=body,
        user=user,
        db=db,
        background_tasks=background_tasks,
    )


@router.post(
    "/apparel-model-library/items/{item_id:path}/auto-tag",
    response_model=ApparelModelLibraryAutoTagOut,
    dependencies=[Depends(verify_csrf)],
)
async def auto_tag_apparel_model_library_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    item_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibraryAutoTagOut:
    return await execute_workflow_action(
        application.require_http().auto_tag_apparel_model_library_item,
        item_id=item_id,
        user=user,
        db=db,
    )
