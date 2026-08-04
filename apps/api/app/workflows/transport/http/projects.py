"""Thin HTTP routes for projects workflows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header

from lumen_core.schemas import (  # noqa: F401 - workflow facade compatibility exports
    AccessoryPlanIn,  # noqa: F401 - showcase facade dependency
    AccessoryPreviewCreateIn,
    AccessorySelectionIn,
    AgeSegment,
    ApparelModelLibraryAutoTagOut,
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryBatchDeleteOut,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibraryItemCreateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryItemPatchIn,
    ApparelModelLibraryJobItemOut,
    ApparelModelLibraryJobOut,
    ApparelModelLibraryJobsClearOut,
    ApparelModelLibraryJobsOut,
    ApparelModelLibraryListOut,
    ApparelModelLibrarySaveJobItemIn,
    ApparelModelLibrarySelectIn,
    ApparelModelLibrarySyncOut,
    ModelAgeSegment,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    CopyAnalysisApproveIn,
    GenerationOut,
    ImageOut,
    ImageParamsIn,
    ImageRevisionIn,
    ModelCandidateApproveIn,
    ModelCandidateSaveToLibraryIn,
    ModelCandidatesCreateIn,
    ModelCandidateOut,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMasterOut,
    PosterMastersCreateIn,
    PosterRenderOut,
    PosterRendersCreateIn,
    PosterReviseIn,
    ProductAnalysisApproveIn,
    QualityReportOut,
    ShowcaseImagesCreateIn,
    WorkflowRunOut,
    WorkflowRunPatchIn,
    WorkflowStepOut,
)

from ....db import get_db
from ....deps import CurrentUser, durable_session_id_from_db, verify_csrf
from ...application.http_contracts import WorkflowAssetsAddIn
from ...application.paid_idempotency import (
    APPAREL_ACCESSORY_PREVIEWS_OPERATION,
    APPAREL_REVISE_IMAGE_OPERATION,
    APPAREL_SHOWCASE_IMAGES_OPERATION,
)
from ...application.upsert_project import UpsertWorkflowProjectCommand
from ...composition import WorkflowApplication
from ...slices import project_lifecycle, upsert_workflow_project
from .dependencies import get_workflow_application
from .execution import (
    execute_durable_workflow_action,
    execute_paid_workflow_action,
    execute_workflow_action,
)

actions_router = APIRouter()
core_router = APIRouter()


@core_router.get("/{workflow_run_id}", response_model=WorkflowRunOut)
async def get_workflow(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_workflow_action(
        application.require_http().get_workflow,
        workflow_run_id=workflow_run_id,
        user=user,
        db=db,
    )


@core_router.post(
    "/{workflow_run_id}/reconcile",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reconcile_workflow(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().reconcile_workflow,
        workflow_run_id=workflow_run_id,
        user=user,
        db=db,
    )


@core_router.patch(
    "/{workflow_run_id}",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_workflow(
    workflow_run_id: str,
    body: WorkflowRunPatchIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    result = await execute_durable_workflow_action(
        upsert_workflow_project(db).upsert_project,
        identity_user=user,
        identity_db=db,
        command=UpsertWorkflowProjectCommand(
            user_id=user.id,
            run_id=workflow_run_id,
            title=body.title,
        ),
    )
    return WorkflowRunOut.model_validate(result)


@core_router.delete("/{workflow_run_id}", dependencies=[Depends(verify_csrf)])
async def delete_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> dict[str, bool]:
    return await execute_durable_workflow_action(
        project_lifecycle(db).delete,
        identity_user=user,
        identity_db=db,
        user_id=user.id,
        run_id=workflow_run_id,
        account_mode=getattr(user, "account_mode", "wallet"),
    )


@core_router.post(
    "/{workflow_run_id}/assets",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_workflow_assets(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: WorkflowAssetsAddIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().add_workflow_assets,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/save-to-library",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_model_candidate_to_library(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateSaveToLibraryIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    return await execute_durable_workflow_action(
        application.require_http().save_model_candidate_to_library,
        workflow_run_id=workflow_run_id,
        candidate_id=candidate_id,
        body=body,
        user=user,
        db=db,
        background_tasks=background_tasks,
    )


@actions_router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_model_candidate(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateApproveIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().approve_model_candidate,
        workflow_run_id=workflow_run_id,
        candidate_id=candidate_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/model-candidates/reopen",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reopen_model_selection(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().reopen_model_selection,
        workflow_run_id=workflow_run_id,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/model-candidates/accessory-previews",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_accessory_previews(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: AccessoryPreviewCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().create_accessory_previews,
        operation_namespace=APPAREL_ACCESSORY_PREVIEWS_OPERATION,
        request_payload={
            "workflow_run_id": workflow_run_id,
            "body": body.model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
        idempotency_user=user,
        idempotency_db=db,
        idempotency_session_id=durable_session_id_from_db(db),
        replay=lambda run: application.require_http().get_workflow(
            workflow_run_id=run.id,
            user=user,
            db=db,
        ),
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/model-candidates/accessory-selection",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_accessory_selection(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: AccessorySelectionIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().save_accessory_selection,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/showcase-images",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_showcase_images(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().create_showcase_images,
        operation_namespace=APPAREL_SHOWCASE_IMAGES_OPERATION,
        request_payload={
            "workflow_run_id": workflow_run_id,
            "body": body.model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
        idempotency_user=user,
        idempotency_db=db,
        idempotency_session_id=durable_session_id_from_db(db),
        replay=lambda run: application.require_http().get_workflow(
            workflow_run_id=run.id,
            user=user,
            db=db,
        ),
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/images/{image_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_showcase_image(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    image_id: str,
    body: ImageRevisionIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().revise_showcase_image,
        operation_namespace=APPAREL_REVISE_IMAGE_OPERATION,
        request_payload={
            "workflow_run_id": workflow_run_id,
            "image_id": image_id,
            "body": body.model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
        idempotency_user=user,
        idempotency_db=db,
        idempotency_session_id=durable_session_id_from_db(db),
        replay=lambda run: application.require_http().get_workflow(
            workflow_run_id=run.id,
            user=user,
            db=db,
        ),
        workflow_run_id=workflow_run_id,
        image_id=image_id,
        body=body,
        user=user,
        db=db,
    )


@actions_router.post(
    "/{workflow_run_id}/delivery/complete",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def complete_delivery(
    application: Annotated[WorkflowApplication, Depends(get_workflow_application)],
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
) -> WorkflowRunOut:
    return await execute_durable_workflow_action(
        application.require_http().complete_delivery,
        workflow_run_id=workflow_run_id,
        user=user,
        db=db,
    )
