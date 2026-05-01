"""Tasks 路由（DESIGN §5.5）：generations / completions 快照 + cancel/retry + 聚合。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EVENTS_STREAM_PREFIX,
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


router = APIRouter()
logger = logging.getLogger(__name__)


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


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
            )
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value):
        raise _http("not_cancelable", f"status is {gen.status}", 409)

    redis = get_redis()
    await redis.set(f"task:{gen.id}:cancel", "1", ex=3600)
    if gen.status == GenerationStatus.RUNNING.value:
        # The worker still owns an upstream call and the image queue lease.
        # Keep the task visible as running until the worker observes the cancel
        # flag, stops the upstream awaitable, and writes the final canceled row.
        await db.commit()
        return {"status": gen.status}

    gen.status = GenerationStatus.CANCELED.value
    gen.finished_at = datetime.now(timezone.utc)
    await db.commit()
    # Queued tasks do not have an upstream process to stop. Clear any stale
    # image_queue side state so a canceled queued row cannot keep capacity.
    try:
        task_provider_key = f"generation:image_queue:task_provider:{gen.id}"
        raw = await redis.get(task_provider_key)
        provider_name: str | None = None
        if isinstance(raw, bytes):
            provider_name = raw.decode("utf-8", "replace")
        elif isinstance(raw, str):
            provider_name = raw
        if provider_name:
            await redis.zrem("generation:image_queue:active", provider_name)
            if not provider_name.startswith("__dr:"):
                await redis.delete(
                    f"generation:image_queue:provider:{provider_name}"
                )
            await redis.delete(task_provider_key)
        await redis.delete(f"task:{gen.id}:lease")
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
            )
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {gen.status}", 409)

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None

    payload = {"task_id": gen.id, "user_id": user.id, "kind": "generation"}
    db.add(OutboxEvent(kind="generation", payload=payload, published_at=None))
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
) -> dict[str, str]:
    comp = (
        await db.execute(
            select(Completion).where(
                Completion.id == comp_id, Completion.user_id == user.id
            )
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
    await redis.set(f"task:{comp.id}:cancel", "1", ex=3600)
    comp.status = CompletionStatus.CANCELED.value
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
            )
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status not in (CompletionStatus.FAILED.value, CompletionStatus.CANCELED.value):
        raise _http("not_retryable", f"status is {comp.status}", 409)

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None

    payload = {"task_id": comp.id, "user_id": user.id, "kind": "completion"}
    db.add(OutboxEvent(kind="completion", payload=payload, published_at=None))
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
    if status:
        gen_stmt = gen_stmt.where(Generation.status == status)
        comp_stmt = comp_stmt.where(Completion.status == status)

    gens = (await db.execute(gen_stmt.order_by(Generation.created_at.desc()).limit(limit))).scalars().all()
    comps = (await db.execute(comp_stmt.order_by(Completion.created_at.desc()).limit(limit))).scalars().all()

    items: list[TaskItemOut] = []
    for g in gens:
        items.append(
            TaskItemOut(
                kind="generation",
                id=g.id,
                message_id=g.message_id,
                status=g.status,
                progress_stage=g.progress_stage,
                started_at=g.started_at,
            )
        )
    for c in comps:
        items.append(
            TaskItemOut(
                kind="completion",
                id=c.id,
                message_id=c.message_id,
                status=c.status,
                progress_stage=c.progress_stage,
                started_at=c.started_at,
            )
        )
    # rough recency sort: started_at desc, None last
    items.sort(
        key=lambda t: (t.started_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
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
    gens = (
        await db.execute(
            select(Generation).where(
                Generation.user_id == user.id,
                Generation.status.in_(
                    [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                ),
            )
            .order_by(Generation.created_at.desc())
            .limit(limit)
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
            .limit(limit)
        )
    ).scalars().all()
    return ActiveTasksOut(
        generations=[GenerationOut.model_validate(g) for g in gens],
        completions=[CompletionOut.model_validate(c) for c in comps],
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
        evt_data = json.dumps(
            {
                "event": ev_name,
                "data": {
                    id_field: payload["task_id"],
                    "message_id": message_id,
                    "kind": kind,
                },
            },
            separators=(",", ":"),
        )
        # Enqueue via arq so the Worker's registered functions consume it.
        pool = await get_arq_pool()
        await pool.enqueue_job(fn_name, payload["task_id"])

        pipe = redis.pipeline(transaction=False)
        pipe.publish(task_channel(payload["task_id"]), evt_data)
        pipe.xadd(
            f"{EVENTS_STREAM_PREFIX}{payload['user_id']}",
            {"event": ev_name, "data": evt_data},
            maxlen=10000,
            approximate=True,
        )
        await pipe.execute()
    except Exception:
        kind = str(payload.get("kind") or "unknown")
        task_publish_errors_total.labels(kind=kind).inc()
        logger.warning(
            "best-effort queued task publish failed kind=%s task_id=%s",
            kind,
            payload.get("task_id"),
            exc_info=True,
        )
