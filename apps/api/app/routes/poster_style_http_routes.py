"""HTTP routes and workflow orchestration for poster styles."""

from __future__ import annotations

import sys
from typing import Annotated, Any, AsyncContextManager, Awaitable, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import WorkflowRun, WorkflowStep
from lumen_core.schemas import (
    ImageParamsIn,
    PosterStyleAutoTagOut,
    PosterStyleBatchDeleteIn,
    PosterStyleBatchDeleteOut,
    PosterStyleCreateIn,
    PosterStyleGenerateIn,
    PosterStyleGenerateOut,
    PosterStyleItemOut,
    PosterStyleJobOut,
    PosterStyleJobsOut,
    PosterStyleListOut,
    PosterStylePatchIn,
    PosterStyleSyncOut,
)

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..services.poster_styles import generation as poster_style_generation
from ..services.poster_styles import resources as poster_style_resources
from ..services.poster_styles import serialization as poster_style_serialization
from ..services.poster_styles import tagging as poster_style_tagging
from ..services.poster_styles.capacity import PosterTaggingCapacityUnavailable
from ..services.poster_styles.tagging_runtime import PosterTaggingRuntime


router = APIRouter(prefix="/poster-styles")


def _runtime() -> Any:
    return sys.modules[f"{__package__}.poster_styles"]


@router.get("", response_model=PosterStyleListOut)
async def list_poster_styles(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    category: str = Query(default="all"),
    source: str = Query(default="all"),
    q: str = Query(default=""),
    tags: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PosterStyleListOut:
    return await poster_style_resources.list_poster_styles(
        _runtime(),
        user=user,
        db=db,
        category=category,
        source=source,
        q=q,
        tags=list(tags or []),
        limit=limit,
        offset=offset,
    )


@router.get("/items/{item_id:path}/binary")
async def get_poster_style_item_binary(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_binary(
        _runtime(),
        item_id=item_id,
        request=request,
        user=user,
        db=db,
    )


@router.get("/items/{item_id:path}/thumb")
async def get_poster_style_item_thumb(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_binary(
        _runtime(),
        item_id=item_id,
        request=request,
        user=user,
        db=db,
        thumbnail=True,
    )


@router.get("/items/{item_id:path}/samples/{sample_index}")
async def get_poster_style_sample(
    item_id: str,
    sample_index: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_sample(
        _runtime(),
        item_id=item_id,
        sample_index=sample_index,
        request=request,
        user=user,
        db=db,
    )


@router.post(
    "/items",
    response_model=PosterStyleItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_style_item(
    body: PosterStyleCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> PosterStyleItemOut:
    runtime = _runtime()
    return await poster_style_resources.create_item(
        runtime,
        body=body,
        user=user,
        db=db,
        background_tasks=background_tasks,
        tagging_runtime=runtime._poster_tagging_runtime(request),
    )


@router.patch(
    "/items/{item_id:path}",
    response_model=PosterStyleItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_poster_style_item(
    item_id: str,
    body: PosterStylePatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleItemOut:
    return await poster_style_resources.patch_item(
        _runtime(),
        item_id=item_id,
        body=body,
        user=user,
        db=db,
    )


async def delete_poster_style_item_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> bool:
    return await poster_style_resources.delete_item_for_user(
        _runtime(),
        db,
        user_id=user_id,
        item_id=item_id,
    )


@router.delete(
    "/items/{item_id:path}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await poster_style_resources.delete_item(
        _runtime(),
        item_id=item_id,
        user=user,
        db=db,
    )


@router.post(
    "/items/batch-delete",
    response_model=PosterStyleBatchDeleteOut,
    dependencies=[Depends(verify_csrf)],
)
async def batch_delete_poster_style_items(
    body: PosterStyleBatchDeleteIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleBatchDeleteOut:
    return await poster_style_resources.batch_delete_items(
        _runtime(),
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/sync-presets",
    response_model=PosterStyleSyncOut,
    dependencies=[Depends(verify_csrf)],
)
async def sync_poster_style_presets(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleSyncOut:
    runtime = _runtime()
    if not await runtime._can_sync_library(db, user):
        raise runtime._http("forbidden", "poster style preset sync is not allowed", 403)
    _, proxy_url = await runtime._resolve_sync_proxy(db)
    return await runtime._sync_library_presets_from_github_folder(
        runtime._github_contents_url(),
        proxy_url=proxy_url,
    )


def poster_style_generate_image_params(aspect_ratio: str) -> ImageParamsIn:
    return poster_style_generation.generate_image_params(aspect_ratio)


def poster_style_generate_prompt(
    *,
    body: PosterStyleGenerateIn,
    candidate_index: int,
) -> str:
    return poster_style_serialization.generate_prompt(
        body,
        candidate_index=candidate_index,
    )


async def get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: Any,
    title: str,
    workflow_type: str,
) -> Any:
    return await poster_style_generation.get_or_create_workflow_conversation(
        _runtime(),
        db,
        user=user,
        title=title,
        workflow_type=workflow_type,
    )


async def create_user_message(
    db: AsyncSession,
    *,
    conv: Any,
    text: str,
    attachment_ids: list[str],
    workflow_run_id: str,
    workflow_step_key: str,
) -> Any:
    return await poster_style_generation.create_user_message(
        _runtime(),
        db,
        conv=conv,
        text=text,
        attachment_ids=attachment_ids,
        workflow_run_id=workflow_run_id,
        workflow_step_key=workflow_step_key,
    )


async def poster_style_create_assistant_task(**kwargs: Any) -> Any:
    return await _runtime()._create_assistant_task(**kwargs)


async def poster_style_publish_assistant_task(**kwargs: Any) -> None:
    await _runtime()._publish_assistant_task(**kwargs)


async def enqueue_poster_style_generate_tasks(
    *,
    db: AsyncSession,
    user: Any,
    conv: Any,
    run: WorkflowRun,
    step: WorkflowStep,
    body: PosterStyleGenerateIn,
) -> tuple[list[str], list[dict[str, Any]]]:
    runtime = _runtime()
    return await poster_style_generation.enqueue_generate_tasks(
        runtime,
        db=db,
        user=user,
        conv=conv,
        run=run,
        step=step,
        body=body,
        create_task_fn=runtime._poster_style_create_assistant_task,
    )


@router.post(
    "/generate",
    response_model=PosterStyleGenerateOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_poster_style_samples(
    body: PosterStyleGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleGenerateOut:
    """用户提交 prompt + 元数据，后端创建隐藏 workflow 并入队 N 个生成任务。"""
    runtime = _runtime()
    return await poster_style_generation.generate_poster_style_samples(
        runtime,
        body=body,
        user=user,
        db=db,
        enqueue_fn=runtime._enqueue_poster_style_generate_tasks,
        publish_fn=runtime._poster_style_publish_assistant_task,
    )


def poster_style_job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
    return poster_style_generation.job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=finished_count,
    )


async def job_from_run(
    db: AsyncSession,
    *,
    run: WorkflowRun,
) -> PosterStyleJobOut:
    return await poster_style_generation.job_from_run(
        _runtime(),
        db,
        run=run,
    )


@router.get("/jobs", response_model=PosterStyleJobsOut)
async def list_poster_style_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PosterStyleJobsOut:
    return await poster_style_generation.list_poster_style_jobs(
        _runtime(),
        user=user,
        db=db,
        limit=limit,
        offset=offset,
    )


async def run_auto_tag_in_background(
    tagging_runtime: PosterTaggingRuntime,
    user_id: str,
    item_id: str,
) -> None:
    await poster_style_tagging.run_auto_tag_in_background(
        _runtime(),
        tagging_runtime,
        user_id,
        item_id,
    )


async def api_call_poster_style_tagging_upstream(
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
    tagging_runtime: PosterTaggingRuntime,
) -> dict[str, Any]:
    return await poster_style_tagging.call_tagging_upstream(
        _runtime(),
        db,
        image_id=image_id,
        user_id=user_id,
        tagging_runtime=tagging_runtime,
    )


def parse_poster_style_tagging_text(text: str) -> dict[str, Any]:
    return poster_style_serialization.parse_tagging_text(text)


async def auto_tag_poster_style_item_impl(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
    capacity: AsyncContextManager[None] | None = None,
    upstream: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> PosterStyleAutoTagOut:
    return await poster_style_tagging.auto_tag_item(
        _runtime(),
        db=db,
        user_id=user_id,
        item_id=item_id,
        capacity=capacity,
        upstream=upstream,
    )


@router.post(
    "/items/{item_id:path}/auto-tag",
    response_model=PosterStyleAutoTagOut,
    dependencies=[Depends(verify_csrf)],
)
async def auto_tag_poster_style_item(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleAutoTagOut:
    runtime = _runtime()
    tagging_runtime = runtime._poster_tagging_runtime(request)

    async def upstream(
        session: AsyncSession,
        *,
        image_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await runtime._api_call_poster_style_tagging_upstream(
            session,
            image_id=image_id,
            user_id=user_id,
            tagging_runtime=tagging_runtime,
        )

    try:
        return await runtime._auto_tag_poster_style_item(
            db=db,
            user_id=user.id,
            item_id=item_id,
            capacity=tagging_runtime.capacity.hold(),
            upstream=upstream,
        )
    except PosterTaggingCapacityUnavailable as exc:
        raise runtime._http(
            "poster_tagging_capacity_unavailable",
            "poster tagging capacity is temporarily unavailable",
            503,
        ) from exc


@router.get("/{item_id:path}", response_model=PosterStyleItemOut)
async def get_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleItemOut:
    runtime = _runtime()
    if item_id in {"sync-presets", "jobs", "items", "generate"}:
        raise runtime._http("not_found", "poster style item not found", 404)
    if item_id.startswith("user:"):
        row = await runtime._find_user_item(db, user_id=user.id, item_id=item_id)
        if row is None:
            raise runtime._http("not_found", "poster style item not found", 404)
        return runtime._item_out_from_row(row)
    raw = await runtime._find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise runtime._http("not_found", "poster style item not found", 404)
    return runtime._item_out_from_preset(raw)
