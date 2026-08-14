"""Create-asset workflow for Volcano asset operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from arq import Retry

from .volcano_asset_runtime import (
    VolcanoAssetRuntimeContext,
    VolcanoAssetRuntimeSlot,
    VolcanoAssetRuntimeView,
)
from .volcano_asset_retry_policy import (
    VOLCANO_ASSET_PRE_SUBMIT_RETRY_LIMIT,
    VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT,
)

logger = logging.getLogger(__name__)

_AIGC_GROUP_TYPE = "AIGC"
_INTENT_LOCK_PREFIX = "video-assets:create-intent:"
_INTENT_LOCK_RETRY_SECONDS = 5
_INTENT_LOCK_TTL_SECONDS = 30 * 60
_SUBMIT_LOCK_PREFIX = "video-assets:create-submit:"
_SUBMIT_LOCK_RETRY_SECONDS = 2
_SUBMIT_LOCK_TTL_SECONDS = 120
_SUBMIT_LOCK_RENEW_INTERVAL_SECONDS = 30
_AUTOMATIC_RETRYABLE_CODES = frozenset(
    {
        "volcano_asset_connection_failed",
        "volcano_asset_create_reconcile_ambiguous",
        "volcano_asset_inventory_incomplete",
        "volcano_asset_proxy_unavailable",
        "volcano_asset_timeout",
    }
)
_RUNTIME = VolcanoAssetRuntimeSlot(
    owner=__name__,
    dependencies=frozenset(
        {
            "VOLCANO_ASSET_MAX_ASSETS",
            "VolcanoAssetClient",
            "VolcanoAssetCreateRateLimited",
            "VolcanoAssetMediaError",
            "VolcanoAssetQuotaExceeded",
            "VolcanoAssetRedisUnavailable",
            "VolcanoAssetServiceError",
            "_LeaseLostError",
            "_OperationFailure",
            "_RELEASE_OPERATION_LOCK_SCRIPT",
            "_RENEW_OPERATION_LOCK_SCRIPT",
            "_SuccessPersistenceError",
            "_ambiguous_create_asset_failure",
            "_complete_operation",
            "_confirm_operation_lock",
            "_defer_for_rate_limit",
            "_defer_for_submit_slot",
            "_media_failure",
            "_provider_for_operation",
            "_reconcile_ambiguous_submit",
            "_record_operation_failure",
            "_release_quota_best_effort",
            "_require_group_scope",
            "_retry_redis_call",
            "_service_failure",
            "_snapshot_group_asset_ids",
            "_source_url_for_submit",
            "_utc_iso",
            "acquire_volcano_create_rate_limit",
            "normalize_asset",
            "normalize_asset_list",
            "normalize_volcano_asset_name",
            "reserve_volcano_asset_quota",
            "video_provider_binding_fingerprint",
            "volcano_asset_quota_key",
            "volcano_asset_quota_scope",
        }
    ),
)


class _IntentLockBusyError(RuntimeError):
    retry_after_seconds = _INTENT_LOCK_RETRY_SECONDS


def install_runtime(context: VolcanoAssetRuntimeContext) -> None:
    _RUNTIME.install(context)


def _runtime() -> VolcanoAssetRuntimeView:
    return _RUNTIME.get()


@dataclass
class _CreateAssetState:
    redis: Any
    operation: dict[str, Any]
    operation_id: str
    persistence: Any
    storage_writes: Any = None
    provider: Any = None
    client: Any = None
    quota_key: Any = None
    group_id: str = ""
    reservation_acquired: bool = False
    deferred: bool = False
    intent_lock_key: str = ""
    intent_lock_owned: bool = False
    release_intent_lock: bool = False
    submit_lock_key: str = ""
    submit_lock_owner: str = ""
    submit_lock_owned: bool = False
    submit_lock_deadline: float = 0.0
    submit_lock_lost: asyncio.Event = field(default_factory=asyncio.Event)
    submit_lock_heartbeat: asyncio.Task[None] | None = None


def _ensure_lease(state: _CreateAssetState) -> None:
    if state.persistence.fence.lease_lost.is_set():
        raise _runtime()._LeaseLostError("Volcano asset operation lease was lost")


def _intent_lock_key(state: _CreateAssetState) -> str:
    runtime = _runtime()
    normalized_name = runtime.normalize_volcano_asset_name(
        state.operation.get("name"),
        fallback_id=state.operation_id,
    )
    binding = runtime.video_provider_binding_fingerprint(state.provider)
    payload = "\0".join(
        (
            binding,
            state.group_id,
            normalized_name,
            str(state.operation.get("asset_type") or ""),
        )
    ).encode("utf-8")
    digest = hashlib.sha256(b"lumen-volcano-create-intent-v1\0" + payload).hexdigest()
    return f"{_INTENT_LOCK_PREFIX}{digest}"


async def _wait_for_intent_lock(state: _CreateAssetState) -> None:
    await state.persistence.update(
        state.operation,
        status="queued",
        progress_stage="waiting_intent_lock",
        retryable=True,
        retry_after_seconds=_INTENT_LOCK_RETRY_SECONDS,
        error=None,
        completed_at=None,
    )
    raise _IntentLockBusyError("matching CreateAsset intent is already active")


async def _acquire_intent_lock(state: _CreateAssetState) -> None:
    runtime = _runtime()
    key = _intent_lock_key(state)
    owner = state.operation_id
    ttl = _INTENT_LOCK_TTL_SECONDS
    acquired = await runtime._retry_redis_call(
        lambda: state.redis.set(
            key,
            owner,
            nx=True,
            ex=ttl,
        )
    )
    if not acquired:
        current = await runtime._retry_redis_call(lambda: state.redis.get(key))
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="replace")
        if current != owner:
            await _wait_for_intent_lock(state)
        renewed = await runtime._retry_redis_call(
            lambda: state.redis.eval(
                runtime._RENEW_OPERATION_LOCK_SCRIPT,
                1,
                key,
                owner,
                ttl,
            )
        )
        if not renewed:
            await _wait_for_intent_lock(state)
    state.intent_lock_key = key
    state.intent_lock_owned = True


async def _confirm_intent_lock(state: _CreateAssetState) -> None:
    runtime = _runtime()
    if not state.intent_lock_owned or not state.intent_lock_key:
        raise runtime._LeaseLostError("Volcano asset intent lease was lost")
    renewed = await runtime._retry_redis_call(
        lambda: state.redis.eval(
            runtime._RENEW_OPERATION_LOCK_SCRIPT,
            1,
            state.intent_lock_key,
            state.operation_id,
            _INTENT_LOCK_TTL_SECONDS,
        )
    )
    if not renewed:
        state.intent_lock_owned = False
        raise runtime._LeaseLostError("Volcano asset intent lease was lost")


async def _release_intent_lock(state: _CreateAssetState) -> None:
    if (
        not state.intent_lock_owned
        or not state.release_intent_lock
        or not state.intent_lock_key
    ):
        return
    runtime = _runtime()
    try:
        await runtime._retry_redis_call(
            lambda: state.redis.eval(
                runtime._RELEASE_OPERATION_LOCK_SCRIPT,
                1,
                state.intent_lock_key,
                state.operation_id,
            )
        )
    except runtime.VolcanoAssetRedisUnavailable:
        logger.warning(
            "video_asset.intent_lock_release_failed operation_id=%s",
            state.operation_id,
            exc_info=True,
        )
    finally:
        state.intent_lock_owned = False


async def _acquire_submit_lock(state: _CreateAssetState) -> bool:
    runtime = _runtime()
    key = f"{_SUBMIT_LOCK_PREFIX}{runtime.volcano_asset_quota_scope(state.quota_key)}"
    owner = f"{state.operation_id}:{max(1, int(state.operation.get('attempt') or 1))}"
    acquired = await runtime._retry_redis_call(
        lambda: state.redis.set(
            key,
            owner,
            nx=True,
            ex=_SUBMIT_LOCK_TTL_SECONDS,
        )
    )
    if not acquired:
        current = await runtime._retry_redis_call(lambda: state.redis.get(key))
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="replace")
        if current == owner:
            acquired = await runtime._retry_redis_call(
                lambda: state.redis.eval(
                    runtime._RENEW_OPERATION_LOCK_SCRIPT,
                    1,
                    key,
                    owner,
                    _SUBMIT_LOCK_TTL_SECONDS,
                )
            )
    if not acquired:
        await runtime._defer_for_submit_slot(
            state.persistence,
            state.operation,
            retry_after_seconds=_SUBMIT_LOCK_RETRY_SECONDS,
        )
        state.deferred = True
        return False
    state.submit_lock_key = key
    state.submit_lock_owner = owner
    state.submit_lock_owned = True
    state.submit_lock_deadline = time.monotonic() + _SUBMIT_LOCK_TTL_SECONDS
    state.submit_lock_lost.clear()
    state.submit_lock_heartbeat = asyncio.create_task(
        _submit_lock_heartbeat(state),
        name=f"volcano-submit-lock:{state.operation_id}",
    )
    return True


def _mark_submit_lock_lost(state: _CreateAssetState) -> None:
    state.submit_lock_owned = False
    state.submit_lock_lost.set()


def _ensure_submit_lock(state: _CreateAssetState) -> None:
    if (
        not state.submit_lock_owned
        or state.submit_lock_lost.is_set()
        or time.monotonic() >= state.submit_lock_deadline
    ):
        _mark_submit_lock_lost(state)
        raise _runtime()._LeaseLostError("Volcano asset submit lease was lost")


async def _renew_submit_lock(state: _CreateAssetState) -> bool:
    return bool(
        await _runtime()._retry_redis_call(
            lambda: state.redis.eval(
                _runtime()._RENEW_OPERATION_LOCK_SCRIPT,
                1,
                state.submit_lock_key,
                state.submit_lock_owner,
                _SUBMIT_LOCK_TTL_SECONDS,
            )
        )
    )


async def _confirm_submit_lock(state: _CreateAssetState) -> None:
    _ensure_submit_lock(state)
    renewed = await _renew_submit_lock(state)
    if not renewed:
        _mark_submit_lock_lost(state)
        raise _runtime()._LeaseLostError("Volcano asset submit lease was lost")
    state.submit_lock_deadline = time.monotonic() + _SUBMIT_LOCK_TTL_SECONDS


async def _submit_lock_heartbeat(state: _CreateAssetState) -> None:
    runtime = _runtime()
    while state.submit_lock_owned:
        remaining = state.submit_lock_deadline - time.monotonic()
        if remaining <= 0:
            _mark_submit_lock_lost(state)
            return
        await asyncio.sleep(min(_SUBMIT_LOCK_RENEW_INTERVAL_SECONDS, remaining))
        if not state.submit_lock_owned:
            return
        try:
            renewed = await _renew_submit_lock(state)
        except runtime.VolcanoAssetRedisUnavailable:
            logger.warning(
                "video_asset.submit_lock_renew_unavailable operation_id=%s",
                state.operation_id,
                exc_info=True,
            )
            continue
        if not renewed:
            _mark_submit_lock_lost(state)
            return
        state.submit_lock_deadline = time.monotonic() + _SUBMIT_LOCK_TTL_SECONDS


async def _stop_submit_lock_heartbeat(state: _CreateAssetState) -> None:
    task = state.submit_lock_heartbeat
    state.submit_lock_heartbeat = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _release_submit_lock(state: _CreateAssetState) -> None:
    await _stop_submit_lock_heartbeat(state)
    if (
        not state.submit_lock_owned
        or not state.submit_lock_key
        or not state.submit_lock_owner
    ):
        return
    runtime = _runtime()
    try:
        await runtime._retry_redis_call(
            lambda: state.redis.eval(
                runtime._RELEASE_OPERATION_LOCK_SCRIPT,
                1,
                state.submit_lock_key,
                state.submit_lock_owner,
            )
        )
    except runtime.VolcanoAssetRedisUnavailable:
        logger.warning(
            "video_asset.submit_lock_release_failed operation_id=%s",
            state.operation_id,
            exc_info=True,
        )
    finally:
        state.submit_lock_owned = False


async def _release_reservation(state: _CreateAssetState) -> None:
    if not state.reservation_acquired or state.quota_key is None:
        return
    released = await _runtime()._release_quota_best_effort(
        state.redis,
        state.quota_key,
        state.operation_id,
    )
    if released:
        state.reservation_acquired = False


def _automatic_retry_delay(
    failure: Any,
    *,
    retry_count: int,
    uncertain_submit: bool,
) -> int:
    if failure.retry_after_seconds is not None:
        return max(1, int(failure.retry_after_seconds))
    if uncertain_submit:
        schedule = (10, 15, 20, 30, 45)
        if retry_count <= len(schedule):
            return schedule[retry_count - 1]
        return 60
    return min(30, 2 ** min(retry_count, 5))


async def _schedule_automatic_retry(
    state: _CreateAssetState,
    failure: Any,
) -> int | None:
    if not failure.retryable or failure.code not in _AUTOMATIC_RETRYABLE_CODES:
        return None
    uncertain_submit = bool(
        state.operation.get("submit_started_at")
        and state.operation.get("submit_outcome_uncertain")
    )
    retry_count = (
        max(
            0,
            int(state.operation.get("automatic_retry_count") or 0),
        )
        + 1
    )
    retry_limit = (
        VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT
        if uncertain_submit
        else VOLCANO_ASSET_PRE_SUBMIT_RETRY_LIMIT
    )
    if retry_count > retry_limit:
        return None
    delay_s = _automatic_retry_delay(
        failure,
        retry_count=retry_count,
        uncertain_submit=uncertain_submit,
    )
    now = datetime.now(timezone.utc)
    await _release_reservation(state)
    await state.persistence.update(
        state.operation,
        status="queued",
        progress_stage=(
            "reconciling_submit" if uncertain_submit else "retrying_upstream"
        ),
        automatic_retry_count=retry_count,
        retry_not_before=(now + timedelta(seconds=delay_s)).isoformat(),
        retryable=True,
        retry_after_seconds=delay_s,
        completed_at=None,
        result=None,
        error={
            "code": failure.code,
            "message": failure.message,
            "retryable": True,
            "retry_after_seconds": delay_s,
        },
    )
    state.deferred = True
    state.release_intent_lock = not uncertain_submit
    logger.info(
        "video_asset.automatic_retry operation_id=%s code=%s count=%s "
        "limit=%s delay_s=%s uncertain_submit=%s",
        state.operation_id,
        failure.code,
        retry_count,
        retry_limit,
        delay_s,
        uncertain_submit,
    )
    return delay_s


async def _prepare_scope(state: _CreateAssetState) -> None:
    runtime = _runtime()
    _ensure_lease(state)
    await state.persistence.update(
        state.operation,
        status="running",
        progress_stage="validating_scope",
        retryable=False,
        retry_after_seconds=None,
        error=None,
    )
    state.provider = await runtime._provider_for_operation(state.operation)
    state.quota_key = runtime.volcano_asset_quota_key(state.provider)
    state.client = runtime.VolcanoAssetClient(state.provider)
    state.group_id = str(state.operation.get("group_id") or "")
    raw_group = await state.client.request(
        "GetAssetGroup",
        {
            "Id": state.group_id,
            "ProjectName": state.provider.project_name,
        },
    )
    runtime._require_group_scope(raw_group, state.provider, state.group_id)


async def _recover_prior_submission(
    state: _CreateAssetState,
) -> dict[str, Any] | None:
    runtime = _runtime()
    if not (
        state.operation.get("submit_started_at") and state.operation.get("source_url")
    ):
        return None
    await _confirm_intent_lock(state)
    recovered = await runtime._reconcile_ambiguous_submit(
        state.client,
        state.provider,
        state.operation,
    )
    if recovered is None:
        raise runtime._ambiguous_create_asset_failure()
    return await runtime._complete_operation(
        state.persistence,
        state.operation,
        recovered,
        provider=state.provider,
    )


async def _reserve_asset_quota(state: _CreateAssetState) -> None:
    runtime = _runtime()
    _ensure_lease(state)
    await state.persistence.update(
        state.operation,
        progress_stage="checking_quota",
    )
    listed = runtime.normalize_asset_list(
        await state.client.request(
            "ListAssets",
            {
                "ProjectName": state.provider.project_name,
                "Filter": {"GroupType": _AIGC_GROUP_TYPE},
                "PageNumber": 1,
                "PageSize": 1,
            },
        ),
        project_name=state.provider.project_name,
        page_number=1,
        page_size=1,
    )
    await runtime.reserve_volcano_asset_quota(
        state.redis,
        state.quota_key,
        resource="assets",
        operation_id=state.operation_id,
        upstream_total=listed["total_count"],
        limit=runtime.VOLCANO_ASSET_MAX_ASSETS,
        now_ms=int(time.time() * 1000),
    )
    state.reservation_acquired = True


async def _prepare_source_url(state: _CreateAssetState) -> str:
    runtime = _runtime()
    _ensure_lease(state)
    await state.persistence.update(
        state.operation,
        progress_stage=(
            "normalizing_image"
            if state.operation.get("asset_type") == "Image"
            else "normalizing_video"
        ),
    )
    return await runtime._source_url_for_submit(
        state.persistence,
        state.operation,
        state.storage_writes,
    )


async def _wait_for_submit_slot(
    state: _CreateAssetState,
) -> dict[str, Any] | None:
    runtime = _runtime()
    await state.persistence.update(
        state.operation,
        progress_stage="waiting_submit_slot",
    )
    if not await _acquire_submit_lock(state):
        return {
            "status": "deferred",
            "operation_id": state.operation_id,
            "retry_after_ms": _SUBMIT_LOCK_RETRY_SECONDS * 1000,
        }
    try:
        await runtime.acquire_volcano_create_rate_limit(
            state.redis,
            state.quota_key,
            bucket="submit",
            operation_id=(
                f"{state.operation_id}:"
                f"{max(1, int(state.operation.get('attempt') or 1))}"
            ),
            now_ms=int(time.time() * 1000),
        )
    except runtime.VolcanoAssetCreateRateLimited as exc:
        await runtime._defer_for_rate_limit(
            state.persistence,
            state.operation,
            exc,
        )
        state.deferred = True
        await _release_submit_lock(state)
        return {
            "status": "deferred",
            "operation_id": state.operation_id,
            "retry_after_ms": exc.retry_after_ms,
        }
    return None


async def _request_asset(
    state: _CreateAssetState,
    public_url: str,
) -> tuple[dict[str, Any], bool]:
    runtime = _runtime()
    await _confirm_submit_lock(state)
    try:
        raw_asset = await state.client.request(
            "CreateAsset",
            {
                "GroupId": state.group_id,
                "URL": public_url,
                "Name": str(state.operation.get("name") or ""),
                "AssetType": str(state.operation.get("asset_type") or ""),
                "ProjectName": state.provider.project_name,
            },
        )
    except runtime.VolcanoAssetServiceError as exc:
        mapped_failure = runtime._service_failure(exc)
        if exc.status_code == 429:
            await state.persistence.update(
                state.operation,
                submit_started_at=None,
                submit_outcome_uncertain=False,
                baseline_asset_ids=[],
            )
            raise mapped_failure from exc
        if mapped_failure.retryable:
            await _confirm_intent_lock(state)
            recovered = await runtime._reconcile_ambiguous_submit(
                state.client,
                state.provider,
                state.operation,
            )
            if recovered is not None:
                return recovered, True
            raise runtime._ambiguous_create_asset_failure() from exc
        await state.persistence.update(
            state.operation,
            submit_started_at=None,
            submit_outcome_uncertain=False,
            baseline_asset_ids=[],
        )
        raise mapped_failure from exc
    await _confirm_submit_lock(state)
    return raw_asset, False


async def _normalize_submitted_asset(
    state: _CreateAssetState,
    raw_asset: dict[str, Any],
    *,
    already_normalized: bool,
) -> dict[str, Any]:
    runtime = _runtime()
    if already_normalized:
        asset = dict(raw_asset)
    else:
        asset = runtime.normalize_asset(
            raw_asset,
            project_name=state.provider.project_name,
            fallback={
                "group_id": state.group_id,
                "name": str(state.operation.get("name") or ""),
                "asset_type": str(state.operation.get("asset_type") or ""),
                "status": "Processing",
                "project_name": state.provider.project_name,
            },
        )
    valid = (
        bool(asset.get("id"))
        and asset.get("group_id") == state.group_id
        and asset.get("project_name") == state.provider.project_name
    )
    if valid:
        preview_url = str(state.operation.get("preview_url") or "")
        if preview_url.startswith(("/api/images/", "/api/videos/")):
            asset["preview_url"] = preview_url
        return asset
    await _confirm_intent_lock(state)
    recovered = await runtime._reconcile_ambiguous_submit(
        state.client,
        state.provider,
        state.operation,
    )
    if recovered is None:
        raise runtime._ambiguous_create_asset_failure()
    preview_url = str(state.operation.get("preview_url") or "")
    if preview_url.startswith(("/api/images/", "/api/videos/")):
        recovered = {**recovered, "preview_url": preview_url}
    return recovered


async def _submit_asset(
    state: _CreateAssetState,
    public_url: str,
) -> dict[str, Any]:
    runtime = _runtime()
    await _confirm_submit_lock(state)
    await _confirm_intent_lock(state)
    baseline_asset_ids = await runtime._snapshot_group_asset_ids(
        state.client,
        state.provider,
        state.operation,
    )
    await _confirm_submit_lock(state)
    await state.persistence.update(
        state.operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
        submit_outcome_uncertain=True,
        baseline_asset_ids=baseline_asset_ids,
    )
    await runtime._confirm_operation_lock(state.persistence)
    await _confirm_intent_lock(state)
    raw_asset, normalized = await _request_asset(state, public_url)
    asset = await _normalize_submitted_asset(
        state,
        raw_asset,
        already_normalized=normalized,
    )
    result = await runtime._complete_operation(
        state.persistence,
        state.operation,
        asset,
        provider=state.provider,
    )
    await _release_reservation(state)
    return result


async def _run_create_asset(
    state: _CreateAssetState,
    failure: Any | None,
) -> dict[str, Any]:
    if failure is not None:
        raise failure
    await _prepare_scope(state)
    await _acquire_intent_lock(state)
    recovered = await _recover_prior_submission(state)
    if recovered is not None:
        return recovered
    await _reserve_asset_quota(state)
    public_url = await _prepare_source_url(state)
    deferred = await _wait_for_submit_slot(state)
    if deferred is not None:
        return deferred
    return await _submit_asset(state, public_url)


def _failure_from_exception(state: _CreateAssetState, exc: Exception) -> Any:
    runtime = _runtime()
    if isinstance(exc, runtime.VolcanoAssetServiceError):
        return runtime._service_failure(exc)
    if isinstance(exc, runtime.VolcanoAssetMediaError):
        return runtime._media_failure(exc)
    if isinstance(exc, runtime.VolcanoAssetQuotaExceeded):
        logger.info(
            "video_asset.quota_exceeded operation_id=%s "
            "upstream_total=%s reservations=%s",
            state.operation_id,
            exc.upstream_total,
            exc.local_reservations,
        )
        return runtime._OperationFailure(
            "volcano_asset_quota_exceeded",
            "the Volcano project already has 50 assets",
            retryable=True,
        )
    if isinstance(exc, runtime._OperationFailure):
        return exc
    logger.exception(
        "video_asset.worker_failed operation_id=%s",
        state.operation_id,
        exc_info=exc,
    )
    return runtime._OperationFailure(
        "video_asset_operation_failed",
        "video asset operation failed",
        retryable=True,
        retry_after_seconds=10,
    )


async def _record_create_failure(
    state: _CreateAssetState,
    failure: Any,
) -> dict[str, Any]:
    try:
        return await _runtime()._record_operation_failure(
            state.persistence,
            state.operation,
            failure,
        )
    finally:
        if not state.deferred:
            await _release_reservation(state)


async def _process_create_asset(
    redis: Any,
    operation: dict[str, Any],
    failure: Any | None,
    *,
    persistence: Any,
    storage_writes: Any = None,
) -> dict[str, Any]:
    runtime = _runtime()
    state = _CreateAssetState(
        redis=redis,
        operation=operation,
        operation_id=str(operation.get("id") or ""),
        persistence=persistence,
        storage_writes=storage_writes,
    )
    try:
        try:
            result = await _run_create_asset(state, failure)
        except Exception as exc:
            if isinstance(
                exc,
                (
                    _IntentLockBusyError,
                    runtime._SuccessPersistenceError,
                    runtime.VolcanoAssetRedisUnavailable,
                    runtime._LeaseLostError,
                ),
            ):
                await _release_reservation(state)
                raise
            mapped = _failure_from_exception(state, exc)
            retry_delay = await _schedule_automatic_retry(state, mapped)
            if retry_delay is not None:
                raise Retry(
                    defer=retry_delay + random.uniform(0, min(2.0, retry_delay / 4))
                ) from exc
            result = await _record_create_failure(state, mapped)
            state.release_intent_lock = state.operation.get(
                "submit_outcome_uncertain"
            ) is False or not state.operation.get("submit_started_at")
            return result
        state.release_intent_lock = True
        return result
    finally:
        await _release_submit_lock(state)
        await _release_intent_lock(state)


__all__ = ["_process_create_asset", "install_runtime"]
