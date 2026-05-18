"""Tasks 路由（DESIGN §5.5）：generations / completions 快照 + cancel/retry + 聚合。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_QUEUED,
    EV_GEN_QUEUED,
    GenerationStage,
    GenerationStatus,
    task_channel,
)
from lumen_core.models import Completion, Generation, OutboxEvent
from lumen_core.schemas import (
    ActiveTasksOut,
    CompletionOut,
    GenerationOut,
    TaskItemOut,
)

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..sse_publish import publish_sse_event


router = APIRouter()
logger = logging.getLogger(__name__)

_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX = "generation:image_queue:provider_active:"
_DUAL_RACE_SENTINEL_PREFIX = "__dr:"


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _image_task_provider_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def _image_provider_active_key(provider_name: str) -> str:
    return f"{_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX}{provider_name}"


def _redis_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return None


async def _release_generation_queue_state(redis: Any, task_id: str) -> None:
    task_provider_key = _image_task_provider_key(task_id)
    provider_name = _redis_text(await redis.get(task_provider_key))
    pipe_fn = getattr(redis, "pipeline", None)
    pipe = pipe_fn(transaction=False) if callable(pipe_fn) else None

    async def _zrem(key: str, member: str) -> None:
        if pipe is not None:
            pipe.zrem(key, member)
        else:
            await redis.zrem(key, member)

    async def _delete(key: str) -> None:
        if pipe is not None:
            pipe.delete(key)
        else:
            await redis.delete(key)

    if provider_name:
        if provider_name.startswith(_DUAL_RACE_SENTINEL_PREFIX):
            await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, provider_name)
        else:
            await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
            await _zrem(_image_provider_active_key(provider_name), task_id)
    else:
        await _zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
    await _delete(task_provider_key)
    await _delete(f"task:{task_id}:lease")
    if pipe is not None:
        await pipe.execute()


# ---------- generations ----------

@router.get("/generations/{gen_id}", response_model=GenerationOut)
async def get_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    return GenerationOut.model_validate(gen)


@router.post("/generations/{gen_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value):
        raise _http("not_cancelable", f"status is {gen.status}", 409)

    redis = get_redis()
    was_queued = gen.status == GenerationStatus.QUEUED.value
    if gen.status == GenerationStatus.RUNNING.value:
        # The worker still owns an upstream call and the image queue lease.
        # Keep the task visible as running until the worker observes the cancel
        # flag, stops the upstream awaitable, and writes the final canceled row.
        # Why no explicit commit: this branch makes no field mutation on `gen`
        # — only the SELECT FOR UPDATE row lock is held. The lock is released
        # at session exit by ``get_db``'s context manager (rollback on raise,
        # commit on clean return); calling commit() here would just be wasted
        # round-trip with identical lock-release timing.
        try:
            await redis.set(f"task:{gen.id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel flag write failed gen=%s err=%s", gen.id, exc)
            raise _http("cancel_unavailable", "cancel signal unavailable", 503) from exc
        return {"status": gen.status}

    gen.status = GenerationStatus.CANCELED.value
    gen.finished_at = datetime.now(timezone.utc)
    await db.commit()
    # Queued tasks do not have an upstream process to stop. Clear any stale
    # image_queue side state so a canceled queued row cannot keep capacity.
    try:
        if was_queued:
            await _release_generation_queue_state(redis, gen.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cancel image_queue release failed gen=%s err=%s",
            gen.id,
            exc,
        )
    return {"status": gen.status}


@router.post("/generations/{gen_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_generation(
    gen_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id, Generation.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {gen.status}", 409)

    redis = get_redis()
    # Why: clearing the prior cancel flag is best-effort cleanup. The
    # worker double-checks the cancel key before each terminal write, so
    # even if a stale flag survives a transient redis blip, the worst
    # case is a re-cancel on the next attempt — never a corrupted row.
    # Don't 503 the user for a transient redis issue.
    try:
        await redis.delete(f"task:{gen.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed gen=%s err=%s", gen.id, exc)

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None

    payload = {"task_id": gen.id, "user_id": user.id, "kind": "generation"}
    outbox = OutboxEvent(kind="generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()

    # best-effort publish
    await _publish_queued(payload, gen.message_id)
    return {"status": gen.status}


# ---------- completions ----------

@router.get("/completions/{comp_id}", response_model=CompletionOut)
async def get_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompletionOut:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    return CompletionOut.model_validate(comp)


@router.post("/completions/{comp_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status not in (
        CompletionStatus.QUEUED.value,
        CompletionStatus.STREAMING.value,
    ):
        raise _http("not_cancelable", f"status is {comp.status}", 409)

    redis = get_redis()
    if comp.status == CompletionStatus.STREAMING.value:
        try:
            await redis.set(f"task:{comp.id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel flag write failed comp=%s err=%s", comp.id, exc)
            raise _http("cancel_unavailable", "cancel signal unavailable", 503) from exc
        return {"status": "canceling", "cancel_requested": True}

    comp.status = CompletionStatus.CANCELED.value
    comp.progress_stage = CompletionStage.FINALIZING.value
    comp.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": comp.status}


@router.post("/completions/{comp_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status not in (CompletionStatus.FAILED.value, CompletionStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {comp.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{comp.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed comp=%s err=%s", comp.id, exc)
        raise _http("retry_unavailable", "could not clear prior cancel signal", 503) from exc

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None

    payload = {"task_id": comp.id, "user_id": user.id, "kind": "completion"}
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()

    await _publish_queued(payload, comp.message_id)
    return {"status": comp.status}


# ---------- aggregate ----------

@router.get("/tasks", response_model=list[TaskItemOut])
async def list_tasks(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    mine: Literal[0, 1] = 1,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskItemOut]:
    _ = mine  # V1: always mine==1; flag accepted for API compat.

    gen_stmt = select(Generation).where(Generation.user_id == user.id)
    comp_stmt = select(Completion).where(Completion.user_id == user.id)
    if status == "active":
        gen_stmt = gen_stmt.where(
            Generation.status.in_(
                [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
            )
        )
        comp_stmt = comp_stmt.where(
            Completion.status.in_(
                [CompletionStatus.QUEUED.value, CompletionStatus.STREAMING.value]
            )
        )
    elif status == "terminal":
        gen_stmt = gen_stmt.where(
            Generation.status.in_(
                [
                    GenerationStatus.SUCCEEDED.value,
                    GenerationStatus.FAILED.value,
                    GenerationStatus.CANCELED.value,
                ]
            )
        )
        comp_stmt = comp_stmt.where(
            Completion.status.in_(
                [
                    CompletionStatus.SUCCEEDED.value,
                    CompletionStatus.FAILED.value,
                    CompletionStatus.CANCELED.value,
                ]
            )
        )
    elif status == GenerationStatus.RUNNING.value:
        gen_stmt = gen_stmt.where(Generation.status == GenerationStatus.RUNNING.value)
        comp_stmt = comp_stmt.where(
            Completion.status == CompletionStatus.STREAMING.value
        )
    elif status == CompletionStatus.STREAMING.value:
        gen_stmt = gen_stmt.where(Generation.status == GenerationStatus.RUNNING.value)
        comp_stmt = comp_stmt.where(
            Completion.status == CompletionStatus.STREAMING.value
        )
    elif status:
        gen_stmt = gen_stmt.where(Generation.status == status)
        comp_stmt = comp_stmt.where(Completion.status == status)

    gens = (await db.execute(gen_stmt.order_by(Generation.created_at.desc()).limit(limit))).scalars().all()
    comps = (await db.execute(comp_stmt.order_by(Completion.created_at.desc()).limit(limit))).scalars().all()

    sortable_items: list[tuple[datetime, TaskItemOut]] = []
    for g in gens:
        sortable_items.append((
            g.started_at or g.created_at or datetime.min.replace(tzinfo=timezone.utc),
            TaskItemOut(
                kind="generation",
                id=g.id,
                message_id=g.message_id,
                status=g.status,
                progress_stage=g.progress_stage,
                started_at=g.started_at,
            ),
        ))
    for c in comps:
        sortable_items.append((
            c.started_at or c.created_at or datetime.min.replace(tzinfo=timezone.utc),
            TaskItemOut(
                kind="completion",
                id=c.id,
                message_id=c.message_id,
                status=c.status,
                progress_stage=c.progress_stage,
                started_at=c.started_at,
            ),
        ))
    sortable_items.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _sort_at, item in sortable_items]
    return items[:limit]


@router.get("/tasks/mine/active", response_model=ActiveTasksOut)
async def list_my_active_tasks(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=200, ge=1, le=500),
) -> ActiveTasksOut:
    """用户级中心任务列表：返回当前用户所有未完成 generations / completions 的完整字段。

    前端启动 / SSE 重连时一次性 hydrate 到 store，让 GlobalTaskTray 显示**所有会话**的
    进行中任务（包括其他会话提交后未访问的）。"""
    # Pull a little extra from each table before the cross-table merge so one
    # busy task type does not starve the other in the final `limit` window.
    query_limit = limit * 2
    gens = (
        await db.execute(
            select(Generation).where(
                Generation.user_id == user.id,
                Generation.status.in_(
                    [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                ),
            )
            .order_by(Generation.created_at.desc())
            .limit(query_limit)
        )
    ).scalars().all()
    comps = (
        await db.execute(
            select(Completion).where(
                Completion.user_id == user.id,
                Completion.status.in_(
                    [CompletionStatus.QUEUED.value, CompletionStatus.STREAMING.value]
                ),
            )
            .order_by(Completion.created_at.desc())
            .limit(query_limit)
        )
    ).scalars().all()
    items: list[tuple[datetime, str, Generation | Completion]] = []
    for gen in gens:
        items.append((gen.created_at, "generation", gen))
    for comp in comps:
        items.append((comp.created_at, "completion", comp))
    items.sort(key=lambda item: item[0], reverse=True)
    items = items[:limit]
    return ActiveTasksOut(
        generations=[
            GenerationOut.model_validate(item)
            for _created_at, kind, item in items
            if kind == "generation"
        ],
        completions=[
            CompletionOut.model_validate(item)
            for _created_at, kind, item in items
            if kind == "completion"
        ],
    )


# ---------- helpers ----------

async def _publish_queued(payload: dict, message_id: str) -> None:
    """Best-effort arq enqueue + PubSub on retry. Outbox publisher is the source of truth."""
    try:
        redis = get_redis()
        kind = payload["kind"]
        fn_name = "run_completion" if kind == "completion" else "run_generation"
        ev_name = EV_COMP_QUEUED if kind == "completion" else EV_GEN_QUEUED
        id_field = "completion_id" if kind == "completion" else "generation_id"
        # Enqueue via arq so the Worker's registered functions consume it.
        pool = await get_arq_pool()
        await pool.enqueue_job(
            fn_name,
            payload["task_id"],
            _job_id=arq_job_id(kind, payload["task_id"], payload.get("outbox_id")),
        )
        await publish_sse_event(
            redis,
            user_id=payload["user_id"],
            channel=task_channel(payload["task_id"]),
            event_name=ev_name,
            data={
                id_field: payload["task_id"],
                "message_id": message_id,
                "kind": kind,
            },
        )
    except Exception:
        kind = str(payload.get("kind") or "unknown")
        task_publish_errors_total.labels(kind=kind).inc()
        logger.warning(
            "best-effort queued task publish failed kind=%s task_id=%s",
            kind,
            payload.get("task_id"),
            exc_info=True,
        )
