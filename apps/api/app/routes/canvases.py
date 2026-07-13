"""Thin HTTP routes for infinite Canvas documents and executions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..canvas_services.api_schemas import (
    CanvasCreateIn,
    CanvasDuplicateIn,
    CanvasExecuteIn,
    CanvasMutationIn,
    CanvasPatchIn,
    CanvasSelectOutputIn,
    CanvasVersionCreateIn,
)
from ..canvas_services.document_service import (
    create_canvas,
    delete_canvas,
    duplicate_canvas,
    get_owned_canvas,
    list_canvases,
    patch_canvas,
)
from ..canvas_services.execution_service import execute_node
from ..canvas_services.mutation_service import apply_mutation
from ..canvas_services.read_repair import repair_canvas_executions
from ..canvas_services.run_serialization import (
    get_run_detail,
    list_run_events,
    list_runs,
    serialize_canvas_document,
    serialize_submission,
)
from ..canvas_services.selection_service import select_execution_output
from ..canvas_services.version_service import (
    create_named_version,
    list_versions,
    restore_version,
    version_dict,
)
from ..db import get_db
from ..deps import CurrentUser, verify_csrf


router = APIRouter(prefix="/canvases", tags=["canvases"])


@router.post("", dependencies=[Depends(verify_csrf)])
async def create_canvas_route(
    body: CanvasCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await create_canvas(db, user_id=user.id, body=body)
    return await serialize_canvas_document(db, canvas=row)


@router.get("")
async def list_canvases_route(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    q: str | None = Query(default=None, max_length=255),
) -> dict:
    return await list_canvases(
        db,
        user_id=user.id,
        cursor=cursor,
        limit=limit,
        q=q,
    )


@router.get("/{canvas_id}")
async def get_canvas_route(
    canvas_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await get_owned_canvas(db, user_id=user.id, canvas_id=canvas_id)
    await repair_canvas_executions(
        db,
        user_id=user.id,
        canvas_id=row.id,
    )
    return await serialize_canvas_document(db, canvas=row)


@router.patch("/{canvas_id}", dependencies=[Depends(verify_csrf)])
async def patch_canvas_route(
    canvas_id: str,
    body: CanvasPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await patch_canvas(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        body=body,
    )
    return await serialize_canvas_document(db, canvas=row)


@router.delete(
    "/{canvas_id}",
    status_code=204,
    dependencies=[Depends(verify_csrf)],
)
async def delete_canvas_route(
    canvas_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    await delete_canvas(db, user_id=user.id, canvas_id=canvas_id)
    return Response(status_code=204)


@router.post(
    "/{canvas_id}/duplicate",
    dependencies=[Depends(verify_csrf)],
)
async def duplicate_canvas_route(
    canvas_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: CanvasDuplicateIn | None = Body(default=None),
) -> dict:
    row = await duplicate_canvas(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        body=body or CanvasDuplicateIn(),
    )
    return await serialize_canvas_document(db, canvas=row)


@router.post(
    "/{canvas_id}/mutations",
    dependencies=[Depends(verify_csrf)],
)
async def apply_canvas_mutation_route(
    canvas_id: str,
    body: CanvasMutationIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> dict:
    return await apply_mutation(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        body=body,
        header_idempotency_key=idempotency_key,
    )


@router.get("/{canvas_id}/versions")
async def list_canvas_versions_route(
    canvas_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return {
        "items": await list_versions(
            db,
            user_id=user.id,
            canvas_id=canvas_id,
            limit=limit,
        )
    }


@router.post(
    "/{canvas_id}/versions",
    dependencies=[Depends(verify_csrf)],
)
async def create_canvas_version_route(
    canvas_id: str,
    body: CanvasVersionCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await create_named_version(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        name=body.name,
    )
    return version_dict(row)


@router.post(
    "/{canvas_id}/versions/{version_id}/restore",
    dependencies=[Depends(verify_csrf)],
)
async def restore_canvas_version_route(
    canvas_id: str,
    version_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await restore_version(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        version_id=version_id,
    )
    return version_dict(row)


@router.post(
    "/{canvas_id}/nodes/{node_id}/execute",
    dependencies=[Depends(verify_csrf)],
)
async def execute_canvas_node_route(
    canvas_id: str,
    node_id: str,
    body: CanvasExecuteIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> dict:
    run, execution = await execute_node(
        db,
        user=user,
        canvas_id=canvas_id,
        node_id=node_id,
        body=body,
        header_idempotency_key=idempotency_key,
        request=request,
    )
    return await serialize_submission(db, run=run, execution=execution)


@router.post(
    "/{canvas_id}/executions/{execution_id}/select",
    dependencies=[Depends(verify_csrf)],
)
async def select_canvas_output_route(
    canvas_id: str,
    execution_id: str,
    body: CanvasSelectOutputIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await select_execution_output(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        execution_id=execution_id,
        body=body,
    )


@router.get("/{canvas_id}/runs")
async def list_canvas_runs_route(
    canvas_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return {
        "items": await list_runs(
            db,
            user_id=user.id,
            canvas_id=canvas_id,
            limit=limit,
        )
    }


@router.get("/{canvas_id}/runs/{run_id}")
async def get_canvas_run_route(
    canvas_id: str,
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await get_run_detail(
        db,
        user_id=user.id,
        canvas_id=canvas_id,
        run_id=run_id,
    )


@router.get("/{canvas_id}/runs/{run_id}/events")
async def list_canvas_run_events_route(
    canvas_id: str,
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    return {
        "items": await list_run_events(
            db,
            user_id=user.id,
            canvas_id=canvas_id,
            run_id=run_id,
            after_seq=after_seq,
            limit=limit,
        )
    }
