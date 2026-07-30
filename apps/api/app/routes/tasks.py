"""Tasks 路由（DESIGN §5.5）：generations / completions 快照 + cancel/retry + 聚合。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
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
from lumen_core.runtime_settings import get_spec
from lumen_core.models import Completion, Generation, OutboxEvent, WalletTransaction
from lumen_core.schemas import (
    CompletionOut,
    GenerationOut,
)

from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services.task_guard import reject_canvas_retry
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..services.generation_queue import release_generation_queue_state
from ..sse_publish import publish_sse_event
from ..task_billing import apply_rate_multiplier_micro, user_rate_multiplier_x10000
from . import task_listing_routes as _task_listing


router = APIRouter()
logger = logging.getLogger(__name__)

TaskListQuery = _task_listing.TaskListQuery
_task_request = _task_listing._task_request
_task_request_value = _task_listing._task_request_value
_task_request_str = _task_listing._task_request_str
_task_request_int = _task_listing._task_request_int
_json_dict = _task_listing._json_dict
_generation_request_image_count = _task_listing._generation_request_image_count
_string_value = _task_listing._string_value
_task_sort_at = _task_listing._task_sort_at
_task_sort_expr = _task_listing._task_sort_expr
_encode_task_cursor = _task_listing._encode_task_cursor
_decode_task_cursor = _task_listing._decode_task_cursor
_task_kind_rank = _task_listing._task_kind_rank
_same_timestamp_cursor_mode = _task_listing._same_timestamp_cursor_mode
_apply_task_cursor = _task_listing._apply_task_cursor
_apply_task_date_filter = _task_listing._apply_task_date_filter
_task_error_code = _task_listing._task_error_code
_task_retryable = _task_listing._task_retryable
_task_recommended_actions = _task_listing._task_recommended_actions
_task_project_meta = _task_listing._task_project_meta
_task_source = _task_listing._task_source
_task_substage = _task_listing._task_substage
_build_task_item = _task_listing._build_task_item
_variant_thumb_url = _task_listing._variant_thumb_url
_task_listing_runtime = _task_listing._task_listing_runtime
list_tasks = _task_listing.list_tasks
list_my_active_tasks = _task_listing.list_my_active_tasks


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _generation_billing_ref_id(task_id: str, retry_count: int | None) -> str:
    return billing_core.retry_billing_ref_id(task_id, retry_count)


def _generation_billing_retry_count(task: Generation) -> int:
    return billing_core.generation_billing_retry_count(task)


def _completion_billing_ref_id(task_id: str, retry_count: int | None) -> str:
    return billing_core.retry_billing_ref_id(task_id, retry_count)


def _completion_billing_retry_count(task: Completion) -> int:
    return billing_core.completion_billing_retry_count(task)


def _completion_task_billing_ref_id(task: Completion) -> str:
    return billing_core.completion_billing_ref_id(task)


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"),
        False,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


async def _generation_rate_multiplier_x10000(
    db: AsyncSession,
    gen: Generation,
) -> int:
    # 与 worker settle 侧同源（apps/worker/app/billing.py 的
    # _snapshot_rate_multiplier_x10000 / generation_rate_multiplier_x10000）：
    # 优先用下单时钉在 upstream_request 上的快照，缺失才回落到用户当前费率。
    raw = _json_dict(getattr(gen, "upstream_request", None)).get(
        "billing_rate_multiplier_x10000"
    )
    if raw is not None:
        try:
            snapshot = int(raw)
        except (TypeError, ValueError):
            snapshot = -1
        if snapshot >= 0:
            return snapshot
    return await user_rate_multiplier_x10000(db, gen.user_id)


async def _generation_retry_base_micro(db: AsyncSession, gen: Generation) -> int:
    request = _json_dict(getattr(gen, "upstream_request", None))
    image_count = _generation_request_image_count(gen)
    tier = _string_value(request.get("billing_tier"))
    if tier in {"1k", "2k", "4k"}:
        amount, _tier = await billing_core.estimate_image_cost_for_tier(
            db,
            tier=tier,
            n=image_count,
        )
        return int(amount or 0)
    pixels = (
        int(getattr(gen, "upstream_pixels", 0) or 0)
        or _task_request_int(gen, "pixel_count")
        or _task_request_int(gen, "upstream_pixels")
        or 0
    )
    if pixels <= 0:
        size = getattr(gen, "size_requested", None)
        if isinstance(size, str) and "x" in size:
            width_raw, height_raw = size.lower().split("x", 1)
            if width_raw.isdigit() and height_raw.isdigit():
                pixels = int(width_raw) * int(height_raw)
    amount, _tier = await billing_core.estimate_image_cost(
        db,
        size_px=max(0, pixels),
        n=image_count,
        thresholds=billing_core.parse_thresholds(
            await _setting_raw(db, "billing.image_size_thresholds")
        ),
    )
    return int(amount or 0)


async def _generation_retry_hold_micro(db: AsyncSession, gen: Generation) -> int:
    if not await _billing_enabled(db):
        return 0
    base_micro = await _generation_retry_base_micro(db, gen)
    if base_micro <= 0:
        return 0
    # 审计新-12：这里以前直接返回 base（未乘倍率），而 settle 侧是乘过倍率的实际成本。
    # 倍率 > 1 的用户重试时 hold 少扣，余额预检形同虚设，差额只能由平台垫付 ——
    # 违反「上游成本纯转嫁」。hold 必须与 settle 用同一口径，宁可多冻结（多余部分
    # settle 时自然 release），也绝不少冻结。
    return apply_rate_multiplier_micro(
        base_micro,
        await _generation_rate_multiplier_x10000(db, gen),
    )


async def _hold_generation_retry_wallet(
    db: AsyncSession,
    user_id: str,
    gen: Generation,
) -> bool:
    if not await _billing_enabled(db):
        return False
    amount = await _generation_retry_hold_micro(db, gen)
    if amount <= 0:
        return False
    retry_count = _generation_billing_retry_count(gen)
    ref_id = _generation_billing_ref_id(gen.id, retry_count)
    try:
        tx = await billing_core.hold(
            db,
            user_id,
            amount,
            ref_type="generation",
            ref_id=ref_id,
            idempotency_key=f"hold:{ref_id}",
            allow_negative=await _billing_allow_negative(db),
            meta={
                "generation_id": gen.id,
                "reason": "generation retry",
                "retry_count": retry_count,
            },
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _completion_retry_hold_micro(
    db: AsyncSession,
    completion: Completion,
    previous_retry_count: int,
) -> int:
    if not await _billing_enabled(db):
        return 0
    prev_ref_id = _completion_billing_ref_id(completion.id, previous_retry_count)
    hold_tx = (
        await db.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.user_id == completion.user_id,
                WalletTransaction.kind == "hold",
                WalletTransaction.ref_type == "completion",
                WalletTransaction.ref_id == prev_ref_id,
            )
            .order_by(WalletTransaction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if hold_tx is None:
        return 0
    try:
        return max(0, -int(hold_tx.amount_micro))
    except (TypeError, ValueError):
        return 0


async def _hold_completion_retry_wallet(
    db: AsyncSession,
    user_id: str,
    completion: Completion,
    previous_retry_count: int,
) -> bool:
    if not await _billing_enabled(db):
        return False
    amount = await _completion_retry_hold_micro(db, completion, previous_retry_count)
    if amount <= 0:
        return False
    next_retry_count = previous_retry_count + 1
    ref_id = _completion_billing_ref_id(completion.id, next_retry_count)
    try:
        tx = await billing_core.hold(
            db,
            user_id,
            amount,
            ref_type="completion",
            ref_id=ref_id,
            idempotency_key=f"hold:{ref_id}",
            allow_negative=await _billing_allow_negative(db),
            meta={
                "completion_id": completion.id,
                "reason": "completion retry",
                "billing_retry_count": next_retry_count,
                "previous_billing_retry_count": previous_retry_count,
            },
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


_release_generation_queue_state = release_generation_queue_state


async def _release_queued_task_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
    reason: str,
) -> bool:
    try:
        tx = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"cancel:{ref_type}:{ref_id}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _task_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


async def _task_should_release_wallet_hold(
    db: AsyncSession,
    user: Any,
) -> bool:
    if getattr(user, "account_mode", "wallet") == "wallet":
        return True
    return await _task_wallet_exists(db, user.id)


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
            select(Generation)
            .where(Generation.id == gen_id, Generation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status not in (
        GenerationStatus.QUEUED.value,
        GenerationStatus.RUNNING.value,
    ):
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
    released_hold = False
    if await _task_should_release_wallet_hold(db, user):
        released_hold = await _release_queued_task_hold(
            db,
            user_id=user.id,
            ref_type="generation",
            ref_id=_generation_billing_ref_id(
                gen.id, _generation_billing_retry_count(gen)
            ),
            reason="queued generation cancelled by user",
        )
    await db.commit()
    if released_hold:
        await invalidate_balance_cache(user.id)
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
    try:
        await publish_sse_event(
            redis,
            user_id=user.id,
            channel=task_channel(gen.id),
            event_name="generation.canceled",
            data={
                "generation_id": gen.id,
                "message_id": gen.message_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": "cancelled",
                "cancelled": True,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": True,
                "recommended_actions": [
                    {"id": "retry", "label": "重新开始", "kind": "retry"}
                ],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "queued generation cancel publish failed gen=%s err=%s", gen.id, exc
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
            select(Generation)
            .where(Generation.id == gen_id, Generation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    reject_canvas_retry(gen)
    if gen.status not in (
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELED.value,
    ):
        raise _http("not_retryable", f"status is {gen.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{gen.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed gen=%s err=%s", gen.id, exc)
        raise _http(
            "retry_unavailable", "could not clear prior cancel signal", 503
        ) from exc

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.billing_retry_count = _generation_billing_retry_count(gen) + 1
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_generation_retry_wallet(db, user.id, gen)

    payload = {"task_id": gen.id, "user_id": user.id, "kind": "generation"}
    outbox = OutboxEvent(kind="generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

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
            select(Completion)
            .where(Completion.id == comp_id, Completion.user_id == user.id)
            .with_for_update()
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
    released_hold = False
    if await _task_should_release_wallet_hold(db, user):
        released_hold = await _release_queued_task_hold(
            db,
            user_id=user.id,
            ref_type="completion",
            ref_id=_completion_task_billing_ref_id(comp),
            reason="queued completion cancelled by user",
        )
    await db.commit()
    if released_hold:
        await invalidate_balance_cache(user.id)
    return {"status": comp.status}


@router.post("/completions/{comp_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_completion(
    comp_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    comp = (
        await db.execute(
            select(Completion)
            .where(Completion.id == comp_id, Completion.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    reject_canvas_retry(comp)
    if comp.status not in (
        CompletionStatus.FAILED.value,
        CompletionStatus.CANCELED.value,
    ):
        raise _http("not_retryable", f"status is {comp.status}", 409)

    redis = get_redis()
    try:
        await redis.delete(f"task:{comp.id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry cancel-flag cleanup failed comp=%s err=%s", comp.id, exc)
        raise _http(
            "retry_unavailable", "could not clear prior cancel signal", 503
        ) from exc

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None
    previous_retry_count = _completion_billing_retry_count(comp)
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_completion_retry_wallet(
            db,
            user.id,
            comp,
            previous_retry_count,
        )
    upstream_request = dict(comp.upstream_request or {})
    upstream_request["billing_retry_count"] = previous_retry_count + 1
    comp.upstream_request = upstream_request or None

    payload = {"task_id": comp.id, "user_id": user.id, "kind": "completion"}
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

    await _publish_queued(payload, comp.message_id)
    return {"status": comp.status}


# ---------- aggregate ----------

router.include_router(_task_listing.router)


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
                "stage": "queued",
                "substage": "waiting_queue",
                "retrying": False,
                "waiting_provider": False,
                "cancelled": False,
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
