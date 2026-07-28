"""Thin HTTP routes for apparel workflows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from lumen_core.schemas import (
    AgeSegment,
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryBatchDeleteOut,
    ApparelModelLibraryItemCreateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryItemPatchIn,
    ApparelModelLibraryListOut,
    ApparelModelLibrarySelectIn,
    ApparelModelLibrarySyncOut,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ModelCandidatesCreateIn,
    ProductAnalysisApproveIn,
    WorkflowRunOut,
)

from ....db import get_db
from ....deps import CurrentUser, verify_csrf
from ...composition import WorkflowApplication
from ...ports.run_creation import CreateApparelRunCommand
from ...slices import create_workflow_run
from .delivery import binary_file_response
from .dependencies import get_workflow_application
from .execution import execute_workflow_action

entry_router = APIRouter()
project_router = APIRouter()


@entry_router.post(
    "/apparel-model-showcase",
    response_model=ApparelWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_apparel_model_showcase(
    body: ApparelWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelWorkflowCreateOut:
    result = await execute_workflow_action(
        create_workflow_run(db, user).create_apparel,
        command=CreateApparelRunCommand(
            user_id=user.id,
            product_image_ids=tuple(body.product_image_ids),
            user_prompt=body.user_prompt,
            quality_mode=body.quality_mode,
            title=body.title,
        ),
    )
    return ApparelWorkflowCreateOut(
        workflow_run_id=result.workflow_run_id,
        status=result.status,
        current_step=result.current_step,
    )


@entry_router.get("/apparel-model-library", response_model=ApparelModelLibraryListOut)
async def list_apparel_model_library(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    age_segment: AgeSegment = Query(default="all"),
    source: str = Query(default="all"),
    appearance: str = Query(default="all"),
    q: str = Query(default=""),
) -> ApparelModelLibraryListOut:
    return await execute_workflow_action(
        application.require_http().list_apparel_model_library,
        user=user,
        db=db,
        age_segment=age_segment,
        source=source,
        appearance=appearance,
        q=q,
    )


@entry_router.post(
    "/apparel-model-library/sync-presets",
    response_model=ApparelModelLibrarySyncOut,
    dependencies=[Depends(verify_csrf)],
)
async def sync_apparel_model_library_presets(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibrarySyncOut:
    return await execute_workflow_action(
        application.require_http().sync_apparel_model_library_presets, user=user, db=db
    )


@entry_router.get("/apparel-model-library/items/{item_id:path}/binary")
async def get_apparel_model_library_item_binary(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> Response:
    binary = await execute_workflow_action(
        application.require_http().get_apparel_model_library_item_binary,
        item_id=item_id,
        request=request,
        user=user,
        db=db,
    )
    return binary_file_response(binary, request)


@entry_router.get("/apparel-model-library/items/{item_id:path}/thumb")
async def get_apparel_model_library_item_thumb(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> Response:
    binary = await execute_workflow_action(
        application.require_http().get_apparel_model_library_item_thumb,
        item_id=item_id,
        request=request,
        user=user,
        db=db,
    )
    return binary_file_response(binary, request)


@entry_router.post(
    "/apparel-model-library/items",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_apparel_model_library_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    body: ApparelModelLibraryItemCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    return await execute_workflow_action(
        application.require_http().create_apparel_model_library_item,
        body=body,
        user=user,
        db=db,
        background_tasks=background_tasks,
    )


@entry_router.patch(
    "/apparel-model-library/items/{item_id:path}",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_apparel_model_library_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    item_id: str,
    body: ApparelModelLibraryItemPatchIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibraryItemOut:
    return await execute_workflow_action(
        application.require_http().patch_apparel_model_library_item,
        item_id=item_id,
        body=body,
        user=user,
        db=db,
    )


@entry_router.delete(
    "/apparel-model-library/items/{item_id:path}", dependencies=[Depends(verify_csrf)]
)
async def delete_apparel_model_library_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    item_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> dict[str, bool]:
    return await execute_workflow_action(
        application.require_http().delete_apparel_model_library_item,
        item_id=item_id,
        user=user,
        db=db,
    )


@entry_router.post(
    "/apparel-model-library/items/batch-delete",
    response_model=ApparelModelLibraryBatchDeleteOut,
    dependencies=[Depends(verify_csrf)],
)
async def batch_delete_apparel_model_library_items(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    body: ApparelModelLibraryBatchDeleteIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> ApparelModelLibraryBatchDeleteOut:
    return await execute_workflow_action(
        application.require_http().batch_delete_apparel_model_library_items,
        body=body,
        user=user,
        db=db,
    )


@project_router.post(
    "/{workflow_run_id}/steps/product-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_product_analysis(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: ProductAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().approve_product_analysis,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@project_router.post(
    "/{workflow_run_id}/model-candidates",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_model_candidates(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: ModelCandidatesCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().create_model_candidates,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@project_router.post(
    "/{workflow_run_id}/model-library/select",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def select_apparel_model_library_item(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: ApparelModelLibrarySelectIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().select_apparel_model_library_item,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )
