"""Tasks 路由（DESIGN §5.5）：generations / completions 快照 + cancel/retry + 聚合。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
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
from lumen_core.upstream_billing import clear_upstream_execution_state

from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..canvas_services.task_guard import reject_canvas_retry
from ..db import get_db
from ..deps import (
    CurrentUser,
    durable_session_id,
    durable_session_id_from_db,
    verify_csrf,
)
from ..observability import task_publish_errors_total
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..services.active_user import lock_authenticated_user_snapshot
from ..services.generation_queue import (
    capture_generation_queue_state,
    completion_cancel_requires_durable_settlement,
    current_execution_epoch,
    generation_cancel_requires_durable_settlement,
    release_generation_queue_state,
)
from ..services.task_retry_publisher import (
    publish_queued_retry as _publish_queued_retry,
)
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


def _next_execution_epoch(task: Generation | Completion) -> int:
    try:
        current = max(0, int(getattr(task, "execution_epoch", 0) or 0))
    except (TypeError, ValueError):
        current = 0
    return current + 1


_COMPLETION_EXECUTION_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "reasoning_tokens",
    "image_output_tokens",
)
_COMPLETION_EXECUTION_REQUEST_KEYS = frozenset(
    {
        "tool_image_reserved_micro",
        "completion_usage_execution_epoch",
        "completion_usage_attempt_epoch",
        "context",
        "memory",
    }
)


def _reset_completion_execution_fields(completion: Completion) -> None:
    completion.text = ""
    for field in _COMPLETION_EXECUTION_USAGE_FIELDS:
        setattr(completion, field, 0)


def _clear_completion_execution_request(completion: Completion) -> dict[str, object]:
    request = clear_upstream_execution_state(completion)
    for key in _COMPLETION_EXECUTION_REQUEST_KEYS:
        request.pop(key, None)
    return request


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
                "execution_epoch": int(getattr(gen, "execution_epoch", 0) or 0),
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
                "execution_epoch": int(getattr(completion, "execution_epoch", 0) or 0),
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


async def _notify_task_cancel(redis: Any, task_id: str, *, kind: str) -> None:
    try:
        await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cancel notification write failed kind=%s task=%s err=%s",
            kind,
            task_id,
            exc,
        )


async def _clear_task_cancel_notification(
    redis: Any,
    task_id: str,
    *,
    kind: str,
) -> None:
    try:
        await redis.delete(f"task:{task_id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cancel notification cleanup failed kind=%s task=%s err=%s",
            kind,
            task_id,
            exc,
        )


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
) -> dict[str, object]:
    snapshot = await lock_authenticated_user_snapshot(
        db,
        user,
        session_id=durable_session_id_from_db(db),
    )
    user = snapshot.user
    gen = (
        await db.execute(
            select(Generation)
            .where(Generation.id == gen_id, Generation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not gen:
        raise _http("not_found", "generation not found", 404)
    if gen.status == GenerationStatus.CANCELED.value:
        return {"status": gen.status, "cancel_requested": True}
    if gen.status not in (
        GenerationStatus.QUEUED.value,
        GenerationStatus.RUNNING.value,
    ):
        raise _http("not_cancelable", f"status is {gen.status}", 409)

    redis = get_redis()
    now = datetime.now(timezone.utc)
    was_queued = gen.status == GenerationStatus.QUEUED.value
    gen.cancel_requested_at = getattr(gen, "cancel_requested_at", None) or now
    if gen.status == GenerationStatus.RUNNING.value or (
        was_queued and generation_cancel_requires_durable_settlement(gen)
    ):
        # The worker still owns upstream/billing cleanup. Persist the intent
        # first; Redis only wakes the live worker and is never authoritative.
        await db.commit()
        await _notify_task_cancel(redis, gen.id, kind="generation")
        return {"status": "canceling", "cancel_requested": True}

    gen.status = GenerationStatus.CANCELED.value
    gen.finished_at = now
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
            queue_ownership = await capture_generation_queue_state(
                redis,
                gen.id,
                expected_execution_epoch=current_execution_epoch(gen),
            )
            await _release_generation_queue_state(
                redis,
                gen.id,
                expected_execution_epoch=current_execution_epoch(gen),
                ownership_token=queue_ownership,
            )
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
    request: Request = None,
) -> dict[str, str]:
    snapshot = await lock_authenticated_user_snapshot(
        db,
        user,
        session_id=durable_session_id(request),
    )
    user = snapshot.user
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

    gen.status = GenerationStatus.QUEUED.value
    gen.progress_stage = GenerationStage.QUEUED.value
    gen.attempt = 0
    gen.execution_epoch = _next_execution_epoch(gen)
    gen.billing_retry_count = _generation_billing_retry_count(gen) + 1
    gen.error_code = None
    gen.error_message = None
    gen.started_at = None
    gen.finished_at = None
    gen.cancel_requested_at = None
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_generation_retry_wallet(db, user.id, gen)
    gen.upstream_request = clear_upstream_execution_state(gen)

    payload = {
        "task_id": gen.id,
        "user_id": user.id,
        "kind": "generation",
        "execution_epoch": gen.execution_epoch,
    }
    outbox = OutboxEvent(kind="generation", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

    await _clear_task_cancel_notification(redis, gen.id, kind="generation")
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
    snapshot = await lock_authenticated_user_snapshot(
        db,
        user,
        session_id=durable_session_id_from_db(db),
    )
    user = snapshot.user
    comp = (
        await db.execute(
            select(Completion)
            .where(Completion.id == comp_id, Completion.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not comp:
        raise _http("not_found", "completion not found", 404)
    if comp.status == CompletionStatus.CANCELED.value:
        return {"status": comp.status, "cancel_requested": True}
    if comp.status not in (
        CompletionStatus.QUEUED.value,
        CompletionStatus.STREAMING.value,
    ):
        raise _http("not_cancelable", f"status is {comp.status}", 409)

    redis = get_redis()
    now = datetime.now(timezone.utc)
    comp.cancel_requested_at = getattr(comp, "cancel_requested_at", None) or now
    if comp.status == CompletionStatus.STREAMING.value or (
        comp.status == CompletionStatus.QUEUED.value
        and completion_cancel_requires_durable_settlement(comp)
    ):
        await db.commit()
        await _notify_task_cancel(redis, comp.id, kind="completion")
        return {"status": "canceling", "cancel_requested": True}

    comp.status = CompletionStatus.CANCELED.value
    comp.progress_stage = CompletionStage.FINALIZING.value
    comp.finished_at = now
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
    request: Request = None,
) -> dict[str, str]:
    snapshot = await lock_authenticated_user_snapshot(
        db,
        user,
        session_id=durable_session_id(request),
    )
    user = snapshot.user
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

    comp.status = CompletionStatus.QUEUED.value
    comp.progress_stage = CompletionStage.QUEUED.value
    comp.attempt = 0
    comp.execution_epoch = _next_execution_epoch(comp)
    comp.error_code = None
    comp.error_message = None
    comp.started_at = None
    comp.finished_at = None
    comp.cancel_requested_at = None
    _reset_completion_execution_fields(comp)
    previous_retry_count = _completion_billing_retry_count(comp)
    held_retry = False
    if await _task_should_release_wallet_hold(db, user):
        held_retry = await _hold_completion_retry_wallet(
            db,
            user.id,
            comp,
            previous_retry_count,
        )
    upstream_request = _clear_completion_execution_request(comp)
    upstream_request["billing_retry_count"] = previous_retry_count + 1
    comp.upstream_request = upstream_request or None

    payload = {
        "task_id": comp.id,
        "user_id": user.id,
        "kind": "completion",
        "execution_epoch": comp.execution_epoch,
    }
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    if held_retry:
        await invalidate_balance_cache(user.id)

    await _clear_task_cancel_notification(redis, comp.id, kind="completion")
    await _publish_queued(payload, comp.message_id)
    return {"status": comp.status}


# ---------- aggregate ----------

router.include_router(_task_listing.router)


async def _publish_queued(payload: dict, message_id: str) -> None:
    await _publish_queued_retry(
        payload,
        message_id,
        get_redis=get_redis,
        get_arq_pool=get_arq_pool,
        publish_sse_event=publish_sse_event,
        publish_error_counter=task_publish_errors_total,
        logger=logger,
    )
