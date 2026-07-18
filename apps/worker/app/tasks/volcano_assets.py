"""Background Volcano AIGC asset operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from arq import Retry
from lumen_core.models import AuditLog, Image, Video
from lumen_core.video_providers import (
    VideoProviderDefinition,
    parse_video_provider_config_json,
    video_provider_binding_fingerprint,
)
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
    VolcanoAssetMediaError,
    ensure_volcano_asset_image_variant,
    ensure_volcano_asset_video_variant,
)
from lumen_core.volcano_assets import (
    VOLCANO_ASSET_MAX_ASSETS,  # noqa: F401 - compatibility runtime export
    VOLCANO_ASSET_MAX_GROUPS,  # noqa: F401 - compatibility runtime export
    VOLCANO_ASSET_OPERATION_TTL_SECONDS,
    VolcanoAssetClient,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaExceeded,  # noqa: F401 - compatibility runtime export
    VolcanoAssetQuotaKey,
    VolcanoAssetRedisUnavailable,
    VolcanoAssetServiceError,
    acquire_volcano_create_rate_limit,  # noqa: F401 - compatibility runtime export
    normalize_asset,  # noqa: F401 - compatibility runtime export
    normalize_asset_group,
    normalize_asset_group_list,  # noqa: F401 - compatibility runtime export
    normalize_asset_list,
    normalize_volcano_asset_name,  # noqa: F401 - compatibility runtime export
    release_volcano_asset_quota,
    reserve_volcano_asset_quota,  # noqa: F401 - compatibility runtime export
    volcano_asset_operation_key,
    volcano_asset_quota_key,  # noqa: F401 - compatibility runtime export
    volcano_asset_reference_url,
)
from sqlalchemy import select

from .. import runtime_settings
from ..config import settings
from ..db import SessionLocal
from . import (
    volcano_asset_actions as _action_parts,
)
from . import (
    volcano_asset_create as _create_parts,
)
from . import (
    volcano_asset_dispatch as _dispatch_parts,
)
from .volcano_assets_parts.receipts import (
    AIGC_GROUP_TYPE as _AIGC_GROUP_TYPE,
    LEGACY_SUCCESS_RECEIPT_EVENT as _LEGACY_SUCCESS_RECEIPT_EVENT,  # noqa: F401
    RECEIPT_BINDING_FIELDS as _RECEIPT_BINDING_FIELDS,  # noqa: F401
    SUCCESS_RECEIPT_EVENT as _SUCCESS_RECEIPT_EVENT,  # noqa: F401
    operation_has_value as _operation_has_value,
    read_success_receipt as _read_success_receipt_impl,
    receipt_asset as _receipt_asset,  # noqa: F401
    receipt_binding_matches as _receipt_binding_matches,  # noqa: F401
    receipt_fence_matches as _receipt_fence_matches,  # noqa: F401
    receipt_group as _receipt_group,  # noqa: F401
    receipt_result as _receipt_result,  # noqa: F401
    success_receipt_details as _success_receipt_details,  # noqa: F401
    validated_receipt_result as _validated_receipt_result,  # noqa: F401
    write_success_receipt as _write_success_receipt_impl,
)

logger = logging.getLogger(__name__)

_REFERENCE_TOKEN_TTL = timedelta(hours=24)
_JOB_NAME = "process_volcano_asset_operation"
_OPERATION_LOCK_TTL_SECONDS = 10 * 60
_OPERATION_LOCK_RENEW_INTERVAL_SECONDS = 60
_REDIS_RETRY_ATTEMPTS = 3
_REDIS_RETRY_BASE_DELAY_SECONDS = 0.02
_AMBIGUOUS_RECONCILE_ATTEMPTS = 3
_GROUP_RECONCILE_BEFORE_SECONDS = 2 * 60
_GROUP_RECONCILE_AFTER_SECONDS = 10 * 60
_ASSET_SCAN_PAGE_SIZE = 100
_ASSET_SCAN_MAX_ITEMS = 3000
_SUPPORTED_ACTIONS = frozenset(
    {
        "create_group",
        "update_group",
        "delete_group",
        "create_asset",
        "update_asset",
        "delete_asset",
    }
)
_OPERATION_FENCING_KEY_PREFIX = "video-assets:operation-fencing:"
_RELEASE_OPERATION_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW_OPERATION_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_ALLOCATE_OPERATION_FENCING_SCRIPT = """
-- volcano-operation-fence-allocate
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
local fencing = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return fencing
"""
_CONFIRM_OPERATION_FENCE_SCRIPT = """
-- volcano-operation-fence-confirm
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return -1
end
if tostring(redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then
  return -2
end
if ARGV[3] ~= '' then
  local raw = redis.call('GET', KEYS[3])
  if not raw then
    return -3
  end
  local ok, operation = pcall(cjson.decode, raw)
  if not ok or type(operation) ~= 'table' then
    return -4
  end
  if tonumber(operation['attempt'] or 1) ~= tonumber(ARGV[3]) then
    return -5
  end
end
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""
_SET_FENCED_OPERATION_SCRIPT = """
-- volcano-operation-fence-set
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return -1
end
if tostring(redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then
  return -2
end
local raw = redis.call('GET', KEYS[3])
if not raw then
  return -3
end
local ok, current = pcall(cjson.decode, raw)
if not ok or type(current) ~= 'table' then
  return -4
end
if tonumber(current['attempt'] or 1) ~= tonumber(ARGV[3]) then
  return -5
end
local current_status = tostring(current['status'] or '')
if (current_status == 'succeeded' or current_status == 'failed')
    and ARGV[7] ~= '1' then
  return -6
end
redis.call('SET', KEYS[3], ARGV[4], 'EX', ARGV[5])
redis.call('EXPIRE', KEYS[1], ARGV[6])
return 1
"""


class _OperationFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class _SuccessPersistenceError(RuntimeError):
    """The upstream asset exists but durable local success state is unavailable."""


class _LeaseLostError(RuntimeError):
    """The operation lease is no longer owned by this worker."""


@dataclass
class _OperationFence:
    operation_id: str
    lock_key: str
    lock_token: str
    fencing_key: str
    fencing: int
    lease_lost: asyncio.Event
    lease_deadline: float
    attempt: int | None = None

    def bind(self, operation: dict[str, Any]) -> None:
        attempt = max(1, int(operation.get("attempt") or 1))
        if self.attempt is None:
            self.attempt = attempt
            return
        if self.attempt != attempt:
            self.mark_lost()
            raise _LeaseLostError(
                "Volcano asset operation attempt fence was superseded"
            )

    def mark_confirmed(self) -> None:
        self.lease_deadline = time.monotonic() + _OPERATION_LOCK_TTL_SECONDS

    def mark_lost(self) -> None:
        self.lease_lost.set()

    def expired(self) -> bool:
        return time.monotonic() >= self.lease_deadline

    def details(self) -> dict[str, Any]:
        if self.attempt is None:
            raise RuntimeError("Volcano asset operation fence is not bound")
        return {
            "lock_token": self.lock_token,
            "attempt": self.attempt,
            "fencing": self.fencing,
        }


@dataclass
class _OperationPersistence:
    redis: Any
    fence: _OperationFence

    def bind(self, operation: dict[str, Any]) -> None:
        self.fence.bind(operation)

    async def confirm(self) -> None:
        await _confirm_operation_fence(self.redis, self.fence)

    async def update(
        self,
        operation: dict[str, Any],
        **changes: Any,
    ) -> None:
        candidate = {
            **operation,
            **changes,
            "updated_at": _utc_iso(),
        }
        await self.replace(operation, candidate)

    async def replace(
        self,
        operation: dict[str, Any],
        candidate: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        self.bind(candidate)
        await _set_fenced_operation(
            self.redis,
            self.fence,
            candidate,
            terminal=terminal,
        )
        operation.clear()
        operation.update(candidate)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _retry_redis_call(
    call: Callable[[], Awaitable[Any]],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(_REDIS_RETRY_ATTEMPTS):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= _REDIS_RETRY_ATTEMPTS:
                break
            delay = _REDIS_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            await asyncio.sleep(delay + random.uniform(0, delay))
    if last_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("Redis operation failed")
    raise VolcanoAssetRedisUnavailable(
        f"Volcano asset Redis operation failed ({type(last_error).__name__})"
    ) from None


async def _get_operation(redis: Any, operation_id: str) -> dict[str, Any] | None:
    raw = await _retry_redis_call(
        lambda: redis.get(volcano_asset_operation_key(operation_id))
    )
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def _set_operation(redis: Any, operation: dict[str, Any]) -> None:
    await _retry_redis_call(
        lambda: redis.set(
            volcano_asset_operation_key(str(operation["id"])),
            json.dumps(
                operation,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            ex=VOLCANO_ASSET_OPERATION_TTL_SECONDS,
        )
    )


async def _update_operation(
    redis: Any,
    operation: dict[str, Any],
    **changes: Any,
) -> None:
    operation.update(changes)
    operation["updated_at"] = _utc_iso()
    await _set_operation(redis, operation)


def _operation_fencing_key(operation_id: str) -> str:
    return f"{_OPERATION_FENCING_KEY_PREFIX}{operation_id}"


async def _allocate_operation_fencing(
    redis: Any,
    *,
    lock_key: str,
    lock_token: str,
    fencing_key: str,
) -> int:
    fencing = await _retry_redis_call(
        lambda: redis.eval(
            _ALLOCATE_OPERATION_FENCING_SCRIPT,
            2,
            lock_key,
            fencing_key,
            lock_token,
            VOLCANO_ASSET_OPERATION_TTL_SECONDS,
        )
    )
    return max(0, int(fencing or 0))


def _raise_lost_fence(fence: _OperationFence) -> None:
    fence.mark_lost()
    raise _LeaseLostError("Volcano asset operation lease was lost")


async def _confirm_operation_fence(
    redis: Any,
    fence: _OperationFence,
) -> None:
    if fence.lease_lost.is_set() or fence.expired():
        _raise_lost_fence(fence)
    try:
        confirmed = await _retry_redis_call(
            lambda: redis.eval(
                _CONFIRM_OPERATION_FENCE_SCRIPT,
                3,
                fence.lock_key,
                fence.fencing_key,
                volcano_asset_operation_key(fence.operation_id),
                fence.lock_token,
                fence.fencing,
                "" if fence.attempt is None else fence.attempt,
                _OPERATION_LOCK_TTL_SECONDS,
            )
        )
    except VolcanoAssetRedisUnavailable:
        if fence.expired():
            _raise_lost_fence(fence)
        raise
    if int(confirmed or 0) != 1:
        _raise_lost_fence(fence)
    fence.mark_confirmed()


async def _set_fenced_operation(
    redis: Any,
    fence: _OperationFence,
    operation: dict[str, Any],
    *,
    terminal: bool,
) -> None:
    if fence.attempt is None:
        fence.bind(operation)
    if fence.lease_lost.is_set() or fence.expired():
        _raise_lost_fence(fence)
    payload = json.dumps(
        operation,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        stored = await _retry_redis_call(
            lambda: redis.eval(
                _SET_FENCED_OPERATION_SCRIPT,
                3,
                fence.lock_key,
                fence.fencing_key,
                volcano_asset_operation_key(fence.operation_id),
                fence.lock_token,
                fence.fencing,
                fence.attempt,
                payload,
                VOLCANO_ASSET_OPERATION_TTL_SECONDS,
                _OPERATION_LOCK_TTL_SECONDS,
                1 if terminal else 0,
            )
        )
    except VolcanoAssetRedisUnavailable:
        if fence.expired():
            _raise_lost_fence(fence)
        raise
    if int(stored or 0) != 1:
        _raise_lost_fence(fence)
    fence.mark_confirmed()


async def _provider_for_operation(
    operation: dict[str, Any],
) -> VideoProviderDefinition:
    raw_video = await runtime_settings.resolve("video.providers")
    raw_shared = await runtime_settings.resolve("providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise _OperationFailure(
            "video_provider_config_invalid",
            "video provider configuration is invalid",
            retryable=True,
        )
    selected = next(
        (
            provider
            for provider in providers
            if provider.name == operation.get("provider_name")
        ),
        None,
    )
    if (
        selected is None
        or selected.kind != "volcano"
        or selected.project_name != operation.get("project_name")
        or selected.region != operation.get("region")
        or not selected.asset_management_ready
        or not selected.supports(str(operation.get("model") or ""), "reference")
    ):
        raise _OperationFailure(
            "video_asset_provider_snapshot_unavailable",
            "the queued Volcano provider configuration is no longer available",
            retryable=True,
        )
    expected_binding = str(operation.get("provider_binding") or "")
    if (
        expected_binding
        and video_provider_binding_fingerprint(selected) != expected_binding
    ):
        raise _OperationFailure(
            "video_asset_provider_snapshot_unavailable",
            "the queued Volcano provider credentials or route have changed",
            retryable=True,
        )
    return selected


def _ensure_reference_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> str:
    existing_token = str(metadata.get(token_key) or "")
    raw_expires_at = str(metadata.get(expires_key) or "")
    try:
        expires_at = datetime.fromisoformat(raw_expires_at)
    except ValueError:
        expires_at = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if existing_token and expires_at > datetime.now(timezone.utc):
            return existing_token
    token = secrets.token_urlsafe(32)
    metadata[token_key] = token
    metadata[expires_key] = (
        datetime.now(timezone.utc) + _REFERENCE_TOKEN_TTL
    ).isoformat()
    return token


async def _normalized_source_url(
    operation: dict[str, Any],
) -> tuple[str, str]:
    source_id = str(operation.get("local_source_id") or "")
    user_id = str(operation.get("user_id") or "")
    asset_type = str(operation.get("asset_type") or "")
    public_base_url = str(operation.get("public_base_url") or "")
    async with SessionLocal() as session:
        if asset_type == "Image":
            image = (
                await session.execute(
                    select(Image).where(
                        Image.id == source_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if image is None:
                raise _OperationFailure(
                    "video_asset_image_not_found",
                    "asset image was not found",
                    retryable=False,
                )
            await ensure_volcano_asset_image_variant(
                session,
                image,
                storage_root=settings.storage_root,
            )
            image = (
                await session.execute(
                    select(Image)
                    .where(
                        Image.id == source_id,
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if image is None:
                raise _OperationFailure(
                    "video_asset_image_not_found",
                    "asset image was not found",
                    retryable=False,
                )
            metadata = dict(image.metadata_jsonb or {})
            token = _ensure_reference_token(
                metadata,
                token_key="video_reference_access_token",
                expires_key="video_reference_access_token_expires_at",
            )
            image.metadata_jsonb = metadata
            await session.commit()
            return (
                volcano_asset_reference_url(
                    public_base_url,
                    resource_id=image.id,
                    asset_type="Image",
                    token=token,
                ),
                VOLCANO_ASSET_IMAGE_KIND,
            )

        if asset_type == "Video":
            video = (
                await session.execute(
                    select(Video).where(
                        Video.id == source_id,
                        Video.user_id == user_id,
                        Video.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if video is None:
                raise _OperationFailure(
                    "video_asset_video_not_found",
                    "asset video was not found",
                    retryable=False,
                )
            await ensure_volcano_asset_video_variant(
                session,
                video,
                storage_root=settings.storage_root,
            )
            video = (
                await session.execute(
                    select(Video)
                    .where(
                        Video.id == source_id,
                        Video.user_id == user_id,
                        Video.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if video is None:
                raise _OperationFailure(
                    "video_asset_video_not_found",
                    "asset video was not found",
                    retryable=False,
                )
            metadata = dict(video.metadata_jsonb or {})
            token = _ensure_reference_token(
                metadata,
                token_key="reference_access_token",
                expires_key="reference_access_token_expires_at",
            )
            video.metadata_jsonb = metadata
            await session.commit()
            return (
                volcano_asset_reference_url(
                    public_base_url,
                    resource_id=video.id,
                    asset_type="Video",
                    token=token,
                ),
                VOLCANO_ASSET_VIDEO_KIND,
            )

    raise _OperationFailure(
        "video_asset_type_invalid",
        "asset type must be Image or Video",
        retryable=False,
    )


def _require_group_scope(
    raw: Any,
    provider: VideoProviderDefinition,
    group_id: str,
) -> None:
    group = normalize_asset_group(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": group_id,
            "project_name": provider.project_name,
        },
    )
    if (
        group.get("id") != group_id
        or str(group.get("group_type") or "").upper() != _AIGC_GROUP_TYPE
        or group.get("project_name") != provider.project_name
    ):
        raise _OperationFailure(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the configured AIGC project",
            retryable=False,
        )


async def _write_audit(
    operation: dict[str, Any],
    *,
    event_type: str,
    details: dict[str, Any],
) -> None:
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=str(operation.get("user_id") or "") or None,
                event_type=event_type,
                actor_email_hash=operation.get("actor_email_hash"),
                actor_ip_hash=operation.get("actor_ip_hash"),
                details=details,
            )
        )
        await session.commit()


async def _read_success_receipt(
    operation: dict[str, Any],
    *,
    fence: _OperationFence,
) -> dict[str, Any] | None:
    return await _read_success_receipt_impl(
        operation,
        fence=fence,
        session_factory=SessionLocal,
    )


async def _write_success_receipt(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    fence: _OperationFence,
) -> None:
    await _write_success_receipt_impl(
        operation,
        result,
        fence=fence,
        session_factory=SessionLocal,
    )


def _service_failure(exc: VolcanoAssetServiceError) -> _OperationFailure:
    retryable = exc.status_code in {429, 502, 503, 504}
    retry_after_seconds = (
        max(1, math.ceil(exc.retry_after_ms / 1000))
        if exc.retry_after_ms is not None
        else None
    )
    return _OperationFailure(
        exc.code,
        exc.message,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _media_failure(exc: VolcanoAssetMediaError) -> _OperationFailure:
    return _OperationFailure(
        exc.code,
        exc.message,
        retryable=exc.status_code >= 500,
    )


def _source_url_is_fresh(operation: dict[str, Any]) -> bool:
    source_url = str(operation.get("source_url") or "")
    created_at = str(operation.get("source_url_created_at") or "")
    if not source_url or not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(hours=23)


async def _source_url_for_submit(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
) -> str:
    if _source_url_is_fresh(operation):
        return str(operation["source_url"])
    public_url, variant_kind = await _normalized_source_url(operation)
    await persistence.update(
        operation,
        source_url=public_url,
        source_variant=variant_kind,
        source_url_created_at=_utc_iso(),
        submit_started_at=None,
    )
    return public_url


def _explicit_asset_total(raw: Any) -> int | None:
    if not isinstance(raw, dict):
        return None
    for key in ("TotalCount", "Total", "total_count", "total"):
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _asset_matches_operation(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> bool:
    return (
        bool(asset.get("id"))
        and asset.get("group_id") == str(operation.get("group_id") or "")
        and asset.get("name") == str(operation.get("name") or "")
        and asset.get("asset_type") == str(operation.get("asset_type") or "")
        and asset.get("project_name") == provider.project_name
    )


async def _scan_operation_assets(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    group_id = str(operation.get("group_id") or "")
    name = str(operation.get("name") or "")
    if not group_id or not name:
        return [], True
    seen_ids: set[str] = set()
    matches: list[dict[str, Any]] = []
    page_number = 1
    scanned_items = 0
    known_total: int | None = None
    while scanned_items < _ASSET_SCAN_MAX_ITEMS:
        raw = await client.request(
            "ListAssets",
            {
                "ProjectName": provider.project_name,
                "Filter": {
                    "GroupType": _AIGC_GROUP_TYPE,
                    "GroupIds": [group_id],
                    "Name": name,
                },
                "PageNumber": page_number,
                "PageSize": _ASSET_SCAN_PAGE_SIZE,
            },
        )
        listed = normalize_asset_list(
            raw,
            project_name=provider.project_name,
            page_number=page_number,
            page_size=_ASSET_SCAN_PAGE_SIZE,
        )
        items = listed["items"]
        explicit_total = _explicit_asset_total(raw)
        if explicit_total is not None:
            known_total = max(known_total or 0, explicit_total)
        if not items:
            return matches, True
        remaining = _ASSET_SCAN_MAX_ITEMS - scanned_items
        scanned_items += min(len(items), remaining)
        items = items[:remaining]
        page_ids = {str(asset.get("id") or "") for asset in items if asset.get("id")}
        new_ids = page_ids - seen_ids
        if not new_ids:
            return matches, False
        for asset in items:
            asset_id = str(asset.get("id") or "")
            if asset_id in new_ids and _asset_matches_operation(
                asset,
                provider,
                operation,
            ):
                matches.append(asset)
        seen_ids.update(new_ids)
        if known_total is not None and len(seen_ids) >= known_total:
            return matches, True
        if scanned_items >= _ASSET_SCAN_MAX_ITEMS:
            return matches, False
        page_number += 1
    return matches, False


async def _find_existing_submitted_asset(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    assets, complete = await _scan_operation_assets(client, provider, operation)
    if not complete:
        return None
    baseline_asset_ids = {
        str(asset_id)
        for asset_id in (
            operation.get("baseline_asset_ids")
            if isinstance(operation.get("baseline_asset_ids"), list)
            else []
        )
        if asset_id is not None and str(asset_id)
    }
    submit_started_at = _parse_operation_time(operation.get("submit_started_at"))
    lower_bound = (
        submit_started_at - timedelta(seconds=_GROUP_RECONCILE_BEFORE_SECONDS)
        if submit_started_at is not None
        else None
    )
    upper_bound = (
        submit_started_at + timedelta(seconds=_GROUP_RECONCILE_AFTER_SECONDS)
        if submit_started_at is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset.get("id") or "")
        created_at = _parse_operation_time(asset.get("create_time"))
        if (
            asset_id
            and asset_id not in baseline_asset_ids
            and (
                created_at is None
                or lower_bound is None
                or upper_bound is None
                or lower_bound <= created_at <= upper_bound
            )
        ):
            matches.append(asset)
    return matches[0] if len(matches) == 1 else None


async def _snapshot_group_asset_ids(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> list[str]:
    assets, complete = await _scan_operation_assets(client, provider, operation)
    if not complete:
        raise _OperationFailure(
            "volcano_asset_inventory_incomplete",
            "could not safely inventory existing Volcano assets",
            retryable=True,
            retry_after_seconds=10,
        )
    return sorted(str(asset["id"]) for asset in assets)


async def _reconcile_ambiguous_submit(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    for attempt in range(_AMBIGUOUS_RECONCILE_ATTEMPTS):
        if attempt:
            delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
            await asyncio.sleep(delay + random.uniform(0, delay))
        try:
            asset = await _find_existing_submitted_asset(
                client,
                provider,
                operation,
            )
        except VolcanoAssetServiceError:
            continue
        if asset is not None:
            return asset
    return None


async def _persist_terminal_operation(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    *,
    status: str,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
    retryable: bool,
    retry_after_seconds: int | None,
    receipt_exists: bool = False,
) -> None:
    persistence.bind(operation)
    if status == "succeeded" and not receipt_exists:
        assert result is not None
        await persistence.confirm()
        try:
            await _write_success_receipt(
                operation,
                result,
                fence=persistence.fence,
            )
        except Exception as exc:  # noqa: BLE001
            raise _SuccessPersistenceError(
                "could not persist Volcano asset ownership receipt"
            ) from exc
    candidate = dict(operation)
    if status == "succeeded":
        candidate.pop("source_url", None)
        candidate.pop("source_url_created_at", None)
        candidate.pop("baseline_asset_ids", None)
        candidate.pop("submit_outcome_uncertain", None)
    candidate.update(
        {
            "status": status,
            "progress_stage": "completed" if status == "succeeded" else "failed",
            "retryable": retryable,
            "retry_after_seconds": retry_after_seconds,
            "result": result,
            "error": error,
            "completed_at": _utc_iso(),
            "updated_at": _utc_iso(),
            **persistence.fence.details(),
        }
    )
    try:
        await persistence.replace(
            operation,
            candidate,
            terminal=True,
        )
    except VolcanoAssetRedisUnavailable as exc:
        if status == "succeeded":
            raise _SuccessPersistenceError(
                "could not persist completed Volcano asset operation"
            ) from exc
        raise


async def _release_quota_best_effort(
    redis: Any,
    quota_key: VolcanoAssetQuotaKey,
    operation_id: str,
    *,
    resource: str = "assets",
) -> bool:
    try:
        await release_volcano_asset_quota(
            redis,
            quota_key,
            resource=resource,
            operation_id=operation_id,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "video_asset.quota_release_failed operation_id=%s resource=%s",
            operation_id,
            resource,
            exc_info=True,
        )
    return False


async def _renew_operation_lock(
    redis: Any,
    lock_key: str,
    lock_token: str,
) -> bool:
    renewed = await _retry_redis_call(
        lambda: redis.eval(
            _RENEW_OPERATION_LOCK_SCRIPT,
            1,
            lock_key,
            lock_token,
            _OPERATION_LOCK_TTL_SECONDS,
        )
    )
    return bool(renewed)


async def _operation_lock_heartbeat(
    persistence: _OperationPersistence,
) -> None:
    while True:
        remaining = persistence.fence.lease_deadline - time.monotonic()
        if remaining <= 0:
            persistence.fence.mark_lost()
            return
        await asyncio.sleep(min(_OPERATION_LOCK_RENEW_INTERVAL_SECONDS, remaining))
        if persistence.fence.expired():
            persistence.fence.mark_lost()
            return
        try:
            await persistence.confirm()
        except VolcanoAssetRedisUnavailable:
            logger.warning(
                "video_asset.operation_lock_renew_unavailable",
                exc_info=True,
            )
            continue
        except _LeaseLostError:
            return


async def _confirm_operation_lock(
    persistence: _OperationPersistence,
) -> None:
    await persistence.confirm()


async def _defer_for_rate_limit(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    exc: VolcanoAssetCreateRateLimited,
) -> None:
    redis = persistence.redis
    retry_after_seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
    delivery_generation = max(0, int(operation.get("delivery_generation") or 0)) + 1
    retry_not_before = (
        datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
    ).isoformat()
    await persistence.update(
        operation,
        status="queued",
        progress_stage="waiting_rate_limit",
        delivery_generation=delivery_generation,
        delivery_enqueued=False,
        retry_not_before=retry_not_before,
        retryable=True,
        retry_after_seconds=retry_after_seconds,
        error={
            "code": "volcano_asset_create_rate_limited",
            "message": "CreateAsset is waiting for the 3 per 60 seconds limit",
            "retryable": True,
            "retry_after_seconds": retry_after_seconds,
        },
    )
    await _retry_redis_call(
        lambda: redis.enqueue_job(
            _JOB_NAME,
            str(operation["id"]),
            max(1, int(operation.get("attempt") or 1)),
            delivery_generation,
            _defer_by=timedelta(seconds=retry_after_seconds),
            _job_id=(
                f"volcano-asset:{operation['id']}:"
                f"{operation.get('attempt', 1)}:{delivery_generation}"
            ),
        )
    )
    await persistence.update(
        operation,
        delivery_enqueued=True,
    )


async def _recover_unconfirmed_delivery(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
) -> bool:
    redis = persistence.redis
    if (
        operation.get("status") != "queued"
        or operation.get("progress_stage") != "waiting_rate_limit"
        or operation.get("delivery_enqueued") is not False
    ):
        return False
    retry_after_seconds = 1
    raw_not_before = str(operation.get("retry_not_before") or "")
    try:
        not_before = datetime.fromisoformat(raw_not_before)
    except ValueError:
        not_before = None
    if not_before is not None:
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        retry_after_seconds = max(
            1,
            math.ceil((not_before - datetime.now(timezone.utc)).total_seconds()),
        )
    delivery_generation = max(
        0,
        int(operation.get("delivery_generation") or 0),
    )
    await _retry_redis_call(
        lambda: redis.enqueue_job(
            _JOB_NAME,
            str(operation["id"]),
            max(1, int(operation.get("attempt") or 1)),
            delivery_generation,
            _defer_by=timedelta(seconds=retry_after_seconds),
            _job_id=(
                f"volcano-asset:{operation['id']}:"
                f"{operation.get('attempt', 1)}:{delivery_generation}"
            ),
        )
    )
    await persistence.update(
        operation,
        delivery_enqueued=True,
    )
    return True


async def _complete_operation(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    provider: VideoProviderDefinition | None = None,
    receipt_exists: bool = False,
) -> dict[str, Any]:
    if provider is not None:
        operation["provider_name"] = provider.name
        operation["region"] = provider.region
        operation["provider_binding"] = video_provider_binding_fingerprint(provider)
    await _persist_terminal_operation(
        persistence,
        operation,
        status="succeeded",
        result=result,
        error=None,
        retryable=False,
        retry_after_seconds=None,
        receipt_exists=receipt_exists,
    )
    if provider is not None:
        action = str(operation.get("action") or "")
        event_type = {
            "create_group": "video_asset_group.create",
            "update_group": "video_asset_group.update",
            "delete_group": "video_asset_group.delete",
            "create_asset": "video_asset.create",
            "update_asset": "video_asset.update",
            "delete_asset": "video_asset.delete",
        }.get(action)
        details = {
            "operation_id": operation.get("id"),
            "action": action,
            "model": operation.get("model"),
            "provider_name": provider.name,
            "project_name": provider.project_name,
            **persistence.fence.details(),
        }
        if action.endswith("_group"):
            details["group_id"] = result.get("id") or operation.get("group_id")
        else:
            details["asset_id"] = result.get("id") or operation.get("asset_id")
            details["group_id"] = result.get("group_id") or operation.get("group_id")
        if action == "create_asset":
            details.update(
                {
                    "asset_type": operation.get("asset_type"),
                    "local_source_id": operation.get("local_source_id"),
                }
            )
        elif action == "update_group":
            details["changed_fields"] = [
                key
                for key in ("name", "description")
                if _operation_has_value(operation, key)
            ]
        elif action == "update_asset":
            details["changed_fields"] = ["name"]
        elif action in {"delete_group", "delete_asset"}:
            details["already_deleted"] = bool(result.get("already_deleted"))
        try:
            if event_type is not None:
                await _write_audit(
                    operation,
                    event_type=event_type,
                    details=details,
                )
        except Exception:  # noqa: BLE001
            logger.error(
                "video_asset.success_audit_failed operation_id=%s",
                operation.get("id"),
                exc_info=True,
            )
    return {
        "status": "succeeded",
        "operation_id": str(operation.get("id") or ""),
        "result": result,
    }


_is_not_found = _action_parts._is_not_found
_parse_operation_time = _action_parts._parse_operation_time
_require_asset_scope = _action_parts._require_asset_scope
_get_scoped_group = _action_parts._get_scoped_group
_get_scoped_asset = _action_parts._get_scoped_asset
_group_target_reached = _action_parts._group_target_reached
_asset_target_reached = _action_parts._asset_target_reached
_reconcile_update_group = _action_parts._reconcile_update_group
_reconcile_update_asset = _action_parts._reconcile_update_asset
_resource_is_deleted = _action_parts._resource_is_deleted
_operation_deleted_asset_ids = _action_parts._operation_deleted_asset_ids
_list_group_asset_ids_best_effort = _action_parts._list_group_asset_ids_best_effort
_delete_group_result = _action_parts._delete_group_result
_delete_asset_result = _action_parts._delete_asset_result
_ambiguous_create_group_failure = _action_parts._ambiguous_create_group_failure
_ambiguous_create_asset_failure = _action_parts._ambiguous_create_asset_failure
_process_create_group = _action_parts._process_create_group
_process_update_group = _action_parts._process_update_group
_process_delete_group = _action_parts._process_delete_group
_process_update_asset = _action_parts._process_update_asset
_process_delete_asset = _action_parts._process_delete_asset
_record_operation_failure = _action_parts._record_operation_failure
_process_management_action = _action_parts._process_management_action
_operation_contract_failure = _action_parts._operation_contract_failure
_process_create_asset = _create_parts._process_create_asset


_process_locked = _dispatch_parts._process_locked


async def _schedule_lock_recovery(
    redis: Any,
    operation_id: str,
    expected_attempt: int | None,
    expected_delivery_generation: int | None,
    lock_key: str,
) -> bool:
    current_token = await _retry_redis_call(lambda: redis.get(lock_key))
    if isinstance(current_token, bytes):
        current_token = current_token.decode("utf-8", errors="replace")
    if not isinstance(current_token, str) or not current_token:
        return False
    token_digest = hashlib.sha256(current_token.encode("utf-8")).hexdigest()[:12]
    attempt_key = expected_attempt if expected_attempt is not None else "current"
    delivery_key = (
        expected_delivery_generation
        if expected_delivery_generation is not None
        else "current"
    )
    marker_key = (
        f"video-assets:operation-lock-recovery:{operation_id}:"
        f"{attempt_key}:{delivery_key}:{token_digest}"
    )
    marker_token = secrets.token_hex(12)
    claimed = await _retry_redis_call(
        lambda: redis.set(
            marker_key,
            marker_token,
            nx=True,
            ex=_OPERATION_LOCK_TTL_SECONDS,
        )
    )
    if not claimed:
        return False
    try:
        await _retry_redis_call(
            lambda: redis.enqueue_job(
                _JOB_NAME,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                _defer_by=timedelta(seconds=_OPERATION_LOCK_TTL_SECONDS + 5),
                _job_id=(
                    f"volcano-asset:{operation_id}:lock-recovery:"
                    f"{token_digest}:{marker_token[:8]}"
                ),
            )
        )
    except VolcanoAssetRedisUnavailable:
        with suppress(VolcanoAssetRedisUnavailable):
            await _retry_redis_call(
                lambda: redis.eval(
                    _RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    marker_key,
                    marker_token,
                )
            )
        raise
    return True


async def process_volcano_asset_operation(
    ctx: dict[str, Any],
    operation_id: str,
    expected_attempt: int | None = None,
    expected_delivery_generation: int | None = None,
) -> dict[str, Any]:
    """Run one operation under a Redis lease to prevent duplicate submits."""
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("Redis is required for Volcano asset operations")
    lock_key = f"video-assets:operation-lock:{operation_id}"
    lock_token = secrets.token_hex(16)
    try:
        locked = await _retry_redis_call(
            lambda: redis.set(
                lock_key,
                lock_token,
                nx=True,
                ex=_OPERATION_LOCK_TTL_SECONDS,
            )
        )
    except VolcanoAssetRedisUnavailable as exc:
        raise Retry(defer=5 + random.uniform(0, 2)) from exc
    if not locked:
        try:
            recovery_scheduled = await _schedule_lock_recovery(
                redis,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                lock_key,
            )
        except VolcanoAssetRedisUnavailable as exc:
            raise Retry(defer=5 + random.uniform(0, 2)) from exc
        return {
            "status": "locked",
            "operation_id": operation_id,
            "retry_after_seconds": _OPERATION_LOCK_TTL_SECONDS + 5,
            "recovery_scheduled": recovery_scheduled,
        }
    lease_lost = asyncio.Event()
    fencing_key = _operation_fencing_key(operation_id)
    try:
        fencing = await _allocate_operation_fencing(
            redis,
            lock_key=lock_key,
            lock_token=lock_token,
            fencing_key=fencing_key,
        )
    except VolcanoAssetRedisUnavailable as exc:
        with suppress(VolcanoAssetRedisUnavailable):
            await _retry_redis_call(
                lambda: redis.eval(
                    _RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            )
        raise Retry(defer=5 + random.uniform(0, 2)) from exc
    if fencing <= 0:
        lease_lost.set()
        raise Retry(defer=5 + random.uniform(0, 2))
    persistence = _OperationPersistence(
        redis=redis,
        fence=_OperationFence(
            operation_id=operation_id,
            lock_key=lock_key,
            lock_token=lock_token,
            fencing_key=fencing_key,
            fencing=fencing,
            lease_lost=lease_lost,
            lease_deadline=time.monotonic() + _OPERATION_LOCK_TTL_SECONDS,
        ),
    )
    heartbeat = asyncio.create_task(_operation_lock_heartbeat(persistence))
    try:
        try:
            return await _process_locked(
                ctx,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                persistence=persistence,
            )
        except _create_parts._IntentLockBusyError as exc:
            delay = exc.retry_after_seconds
            raise Retry(defer=delay + random.uniform(0, delay / 4)) from exc
        except (
            _LeaseLostError,
            _SuccessPersistenceError,
            VolcanoAssetRedisUnavailable,
        ) as exc:
            job_try = max(1, int(ctx.get("job_try") or 1))
            delay = min(60.0, 5.0 * (2 ** min(job_try - 1, 3)))
            raise Retry(defer=delay + random.uniform(0, delay / 4)) from exc
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        try:
            await _retry_redis_call(
                lambda: redis.eval(
                    _RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            )
        except VolcanoAssetRedisUnavailable:
            logger.warning(
                "video_asset.operation_lock_release_failed operation_id=%s",
                operation_id,
                exc_info=True,
            )


__all__ = ["process_volcano_asset_operation"]
