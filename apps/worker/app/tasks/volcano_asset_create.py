"""Create-asset workflow for Volcano asset operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_AIGC_GROUP_TYPE = "AIGC"
_INTENT_LOCK_PREFIX = "video-assets:create-intent:"
_INTENT_LOCK_RETRY_SECONDS = 5


class _IntentLockBusyError(RuntimeError):
    retry_after_seconds = _INTENT_LOCK_RETRY_SECONDS


def _runtime() -> Any:
    from . import volcano_assets

    return volcano_assets


@dataclass
class _CreateAssetState:
    redis: Any
    operation: dict[str, Any]
    operation_id: str
    lock_key: str
    lock_token: str
    lease_lost: asyncio.Event
    provider: Any = None
    client: Any = None
    quota_key: Any = None
    group_id: str = ""
    reservation_acquired: bool = False
    deferred: bool = False
    intent_lock_key: str = ""
    intent_lock_owned: bool = False
    release_intent_lock: bool = False


def _ensure_lease(state: _CreateAssetState) -> None:
    if state.lease_lost.is_set():
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
    runtime = _runtime()
    await runtime._update_operation(
        state.redis,
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
    ttl = runtime.VOLCANO_ASSET_OPERATION_TTL_SECONDS
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
            runtime.VOLCANO_ASSET_OPERATION_TTL_SECONDS,
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


async def _prepare_scope(state: _CreateAssetState) -> None:
    runtime = _runtime()
    _ensure_lease(state)
    await runtime._update_operation(
        state.redis,
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
        state.redis,
        state.operation,
        recovered,
        provider=state.provider,
    )


async def _reserve_asset_quota(state: _CreateAssetState) -> None:
    runtime = _runtime()
    _ensure_lease(state)
    await runtime._update_operation(
        state.redis,
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
    await runtime._update_operation(
        state.redis,
        state.operation,
        progress_stage=(
            "normalizing_image"
            if state.operation.get("asset_type") == "Image"
            else "normalizing_video"
        ),
    )
    return await runtime._source_url_for_submit(state.redis, state.operation)


async def _wait_for_submit_slot(
    state: _CreateAssetState,
) -> dict[str, Any] | None:
    runtime = _runtime()
    await runtime._update_operation(
        state.redis,
        state.operation,
        progress_stage="waiting_submit_slot",
    )
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
        await runtime._defer_for_rate_limit(state.redis, state.operation, exc)
        state.deferred = True
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
            await runtime._update_operation(
                state.redis,
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
        await runtime._update_operation(
            state.redis,
            state.operation,
            submit_started_at=None,
            submit_outcome_uncertain=False,
            baseline_asset_ids=[],
        )
        raise mapped_failure from exc
    return raw_asset, False


async def _normalize_submitted_asset(
    state: _CreateAssetState,
    raw_asset: dict[str, Any],
    *,
    already_normalized: bool,
) -> dict[str, Any]:
    runtime = _runtime()
    if already_normalized:
        return raw_asset
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
        return asset
    await _confirm_intent_lock(state)
    recovered = await runtime._reconcile_ambiguous_submit(
        state.client,
        state.provider,
        state.operation,
    )
    if recovered is None:
        raise runtime._ambiguous_create_asset_failure()
    return recovered


async def _submit_asset(
    state: _CreateAssetState,
    public_url: str,
) -> dict[str, Any]:
    runtime = _runtime()
    await _confirm_intent_lock(state)
    baseline_asset_ids = await runtime._snapshot_group_asset_ids(
        state.client,
        state.provider,
        state.operation,
    )
    await runtime._update_operation(
        state.redis,
        state.operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
        submit_outcome_uncertain=True,
        baseline_asset_ids=baseline_asset_ids,
    )
    await runtime._confirm_operation_lock(
        state.redis,
        state.lock_key,
        state.lock_token,
        state.lease_lost,
    )
    await _confirm_intent_lock(state)
    raw_asset, normalized = await _request_asset(state, public_url)
    asset = await _normalize_submitted_asset(
        state,
        raw_asset,
        already_normalized=normalized,
    )
    result = await runtime._complete_operation(
        state.redis,
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
    runtime = _runtime()
    try:
        await runtime._update_operation(
            state.redis,
            state.operation,
            status="failed",
            progress_stage="failed",
            retryable=failure.retryable,
            retry_after_seconds=failure.retry_after_seconds,
            result=None,
            error={
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
                "retry_after_seconds": failure.retry_after_seconds,
            },
            completed_at=runtime._utc_iso(),
        )
    except runtime.VolcanoAssetRedisUnavailable:
        if not state.deferred:
            await _release_reservation(state)
        raise
    try:
        await runtime._write_audit(
            state.operation,
            event_type="video_asset.create.failed",
            details={
                "operation_id": state.operation_id,
                "group_id": state.operation.get("group_id"),
                "asset_type": state.operation.get("asset_type"),
                "local_source_id": state.operation.get("local_source_id"),
                "model": state.operation.get("model"),
                "provider_name": state.operation.get("provider_name"),
                "project_name": state.operation.get("project_name"),
                "error_code": failure.code,
                "retryable": failure.retryable,
            },
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "video_asset.failure_audit_failed operation_id=%s",
            state.operation_id,
            exc_info=True,
        )
    finally:
        if not state.deferred:
            await _release_reservation(state)
    return {
        "status": "failed",
        "operation_id": state.operation_id,
        "error": {
            "code": failure.code,
            "message": failure.message,
            "retryable": failure.retryable,
        },
    }


async def _process_create_asset(
    redis: Any,
    operation: dict[str, Any],
    failure: Any | None,
    *,
    lock_key: str,
    lock_token: str,
    lease_lost: asyncio.Event,
) -> dict[str, Any]:
    runtime = _runtime()
    state = _CreateAssetState(
        redis=redis,
        operation=operation,
        operation_id=str(operation.get("id") or ""),
        lock_key=lock_key,
        lock_token=lock_token,
        lease_lost=lease_lost,
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
            result = await _record_create_failure(state, mapped)
            state.release_intent_lock = state.operation.get(
                "submit_outcome_uncertain"
            ) is False or not state.operation.get("submit_started_at")
            return result
        state.release_intent_lock = True
        return result
    finally:
        await _release_intent_lock(state)


__all__ = ["_process_create_asset"]
