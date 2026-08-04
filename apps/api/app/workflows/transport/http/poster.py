"""Thin HTTP routes for poster workflows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header

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
from ....deps import CurrentUser, durable_session_id_from_db, verify_csrf
from ...application.paid_idempotency import (
    POSTER_CREATE_OPERATION,
    POSTER_INPAINT_RENDER_OPERATION,
    POSTER_MASTERS_OPERATION,
    POSTER_RENDERS_OPERATION,
    POSTER_REVISE_RENDER_OPERATION,
)
from ...composition import WorkflowApplication
from ...ports.run_creation import (
    CreatePosterRunCommand,
    PosterBrandAssets,
)
from ...slices import create_workflow_run
from .dependencies import get_workflow_application
from .execution import (
    execute_durable_workflow_action,
    execute_paid_workflow_action,
)

router = APIRouter()


async def _replay_created_poster_workflow(run: Any) -> PosterDesignWorkflowCreateOut:
    return PosterDesignWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@router.post(
    "/poster-design",
    response_model=PosterDesignWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_design_workflow(
    body: PosterDesignWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[Any, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> PosterDesignWorkflowCreateOut:
    result = await execute_paid_workflow_action(
        create_workflow_run(db, user).create_poster,
        operation_namespace=POSTER_CREATE_OPERATION,
        request_payload={"body": body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
        idempotency_user=user,
        idempotency_db=db,
        idempotency_session_id=durable_session_id_from_db(db),
        replay=_replay_created_poster_workflow,
        command=CreatePosterRunCommand(
            user_id=user.id,
            conversation_id=body.conversation_id,
            copy_text=body.copy_text,
            style_id=body.style_id,
            target_aspects=tuple(body.target_aspects),
            brand_assets=PosterBrandAssets(
                logo_image_id=body.brand_assets.logo_image_id,
                product_image_id=body.brand_assets.product_image_id,
                primary_color=body.brand_assets.primary_color,
                font_family=body.brand_assets.font_family,
            ),
            quality_mode=body.quality_mode,
            title=body.title,
        ),
    )
    return PosterDesignWorkflowCreateOut(
        workflow_run_id=result.workflow_run_id,
        status=result.status,
        current_step=result.current_step,
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
    return await execute_durable_workflow_action(
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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().create_poster_masters,
        operation_namespace=POSTER_MASTERS_OPERATION,
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
    return await execute_durable_workflow_action(
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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().create_poster_renders,
        operation_namespace=POSTER_RENDERS_OPERATION,
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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().revise_poster_render,
        operation_namespace=POSTER_REVISE_RENDER_OPERATION,
        request_payload={
            "workflow_run_id": workflow_run_id,
            "render_id": render_id,
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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunOut:
    return await execute_paid_workflow_action(
        application.require_http().inpaint_poster_render,
        operation_namespace=POSTER_INPAINT_RENDER_OPERATION,
        request_payload={
            "workflow_run_id": workflow_run_id,
            "render_id": render_id,
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
        render_id=render_id,
        body=body,
        user=user,
        db=db,
    )
