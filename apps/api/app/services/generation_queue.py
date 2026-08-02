"""Generation queue state cleanup shared by routes and services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lumen_core.upstream_billing import (
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
    has_upstream_response_receipt,
)


_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX = "generation:image_queue:provider_active:"
_IMAGE_QUEUE_RESERVATION_PREFIX = "generation:image_queue:reservation:"
_DUAL_RACE_SENTINEL_PREFIX = "__dr:"
_LEASE_EXECUTION_EPOCH_RE = re.compile(r"(?:^|:)execution:(\d+)(?::|$)")
_COMPLETION_USAGE_EXECUTION_EPOCH_KEY = "completion_usage_execution_epoch"
_COMPLETION_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "reasoning_tokens",
    "image_output_tokens",
)

_RELEASE_GENERATION_QUEUE_STATE_LUA = """
local provider_zset = KEYS[1]
local global_zset = KEYS[2]
local task_provider_key = KEYS[3]
local task_lease_key = KEYS[4]
local reservation_key = KEYS[5]

local expected_provider = ARGV[1]
local expected_lease = ARGV[2]
local expected_reservation = ARGV[3]
local task_id = ARGV[4]
local active_member = ARGV[5]
local is_dual_race = ARGV[6]

if redis.call('GET', task_provider_key) ~= expected_provider then
  return 0
end
local current_lease = redis.call('GET', task_lease_key)
if expected_lease == '' then
  if current_lease then
    return 0
  end
elseif current_lease ~= expected_lease then
  return 0
end
local current_reservation = redis.call('GET', reservation_key)
if expected_reservation == '' then
  if current_reservation then
    return 0
  end
elseif current_reservation ~= expected_reservation then
  return 0
end

if is_dual_race == '1' then
  redis.call('ZREM', global_zset, active_member)
else
  redis.call('ZREM', provider_zset, task_id)
  redis.call('ZREM', global_zset, active_member)
end
redis.call('DEL', task_provider_key)
redis.call('DEL', task_lease_key)
redis.call('DEL', reservation_key)
return 1
"""


@dataclass(frozen=True, slots=True)
class GenerationQueueReleaseToken:
    task_id: str
    execution_epoch: int
    provider_name: str
    lease_token: str | None
    reservation_token: str | None


def _task_provider_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def _provider_active_key(provider_name: str) -> str:
    return f"{_IMAGE_QUEUE_PROVIDER_ACTIVE_PREFIX}{provider_name}"


def _reservation_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_RESERVATION_PREFIX}{task_id}"


def _redis_text(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return None


def current_execution_epoch(task: object) -> int:
    try:
        return max(0, int(getattr(task, "execution_epoch", 0) or 0))
    except (TypeError, ValueError):
        return 0


def queued_generation_cleanup_entries(
    cleanup: dict[str, Any],
) -> list[tuple[str, int, GenerationQueueReleaseToken]]:
    raw_ids = cleanup.get("queued_generation_ids")
    raw_epochs = cleanup.get("queued_generation_execution_epochs")
    raw_tokens = cleanup.get("queued_generation_queue_tokens")
    if (
        not isinstance(raw_ids, list)
        or not isinstance(raw_epochs, dict)
        or not isinstance(raw_tokens, dict)
    ):
        return []
    entries: list[tuple[str, int, GenerationQueueReleaseToken]] = []
    seen: set[str] = set()
    for task_id in raw_ids:
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            continue
        try:
            execution_epoch = max(0, int(raw_epochs[task_id]))
        except (KeyError, TypeError, ValueError):
            continue
        token = _coerce_release_token(
            raw_tokens.get(task_id),
            task_id=task_id,
            execution_epoch=execution_epoch,
        )
        if token is None:
            continue
        seen.add(task_id)
        entries.append((task_id, execution_epoch, token))
    return entries


def completion_has_trustworthy_persisted_usage(completion: object) -> bool:
    request = getattr(completion, "upstream_request", None)
    if not isinstance(request, dict):
        return False
    try:
        usage_epoch = max(
            0,
            int(request.get(_COMPLETION_USAGE_EXECUTION_EPOCH_KEY)),
        )
    except (TypeError, ValueError):
        return False
    if usage_epoch != current_execution_epoch(completion):
        return False
    for field in _COMPLETION_USAGE_FIELDS:
        try:
            if int(getattr(completion, field, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_current_billable_upstream_receipt(task: object) -> bool:
    return bool(
        has_upstream_response_receipt(task)
        or (
            has_upstream_dispatch_receipt(task)
            and not has_proven_undelivered_dispatch(task)
        )
    )


def generation_cancel_requires_durable_settlement(generation: object) -> bool:
    return _has_current_billable_upstream_receipt(generation)


def completion_cancel_requires_durable_settlement(completion: object) -> bool:
    return bool(
        completion_has_trustworthy_persisted_usage(completion)
        or _has_current_billable_upstream_receipt(completion)
    )


def _lease_execution_epoch(lease_token: str) -> int | None:
    match = _LEASE_EXECUTION_EPOCH_RE.search(lease_token)
    if match is None:
        return None
    return int(match.group(1))


def _coerce_release_token(
    value: object,
    *,
    task_id: str,
    execution_epoch: int,
) -> GenerationQueueReleaseToken | None:
    if isinstance(value, GenerationQueueReleaseToken):
        token = value
    elif isinstance(value, dict):
        provider_name = value.get("provider_name")
        lease_token = value.get("lease_token")
        reservation_token = value.get("reservation_token")
        if not isinstance(provider_name, str) or not provider_name:
            return None
        if lease_token is not None and not isinstance(lease_token, str):
            return None
        if reservation_token is not None and not isinstance(reservation_token, str):
            return None
        try:
            token = GenerationQueueReleaseToken(
                task_id=str(value.get("task_id") or task_id),
                execution_epoch=max(
                    0,
                    int(value.get("execution_epoch", execution_epoch)),
                ),
                provider_name=provider_name,
                lease_token=lease_token,
                reservation_token=reservation_token,
            )
        except (TypeError, ValueError):
            return None
    else:
        return None
    if token.task_id != task_id or token.execution_epoch != execution_epoch:
        return None
    if not token.lease_token and not token.reservation_token:
        return None
    return token


async def capture_generation_queue_state(
    redis: Any,
    task_id: str,
    *,
    expected_execution_epoch: int,
) -> GenerationQueueReleaseToken | None:
    """Snapshot queue ownership while the caller still holds the task row lock."""
    expected_epoch = max(0, int(expected_execution_epoch))
    provider_name = _redis_text(await redis.get(_task_provider_key(task_id)))
    if not provider_name:
        return None
    lease_token = _redis_text(await redis.get(f"task:{task_id}:lease"))
    reservation_token = _redis_text(await redis.get(_reservation_key(task_id)))
    if lease_token and _lease_execution_epoch(lease_token) != expected_epoch:
        return None
    if not lease_token and not reservation_token:
        return None
    return GenerationQueueReleaseToken(
        task_id=task_id,
        execution_epoch=expected_epoch,
        provider_name=provider_name,
        lease_token=lease_token,
        reservation_token=reservation_token,
    )


async def release_generation_queue_state(
    redis: Any,
    task_id: str,
    *,
    expected_execution_epoch: int | None = None,
    ownership_token: GenerationQueueReleaseToken | None = None,
) -> bool:
    """Release only queue state still owned by the canceled execution epoch."""
    if expected_execution_epoch is None or ownership_token is None:
        return False
    expected_epoch = max(0, int(expected_execution_epoch))
    token = _coerce_release_token(
        ownership_token,
        task_id=task_id,
        execution_epoch=expected_epoch,
    )
    if token is None:
        return False
    task_provider_key = _task_provider_key(task_id)
    task_lease_key = f"task:{task_id}:lease"
    reservation_key = _reservation_key(task_id)
    eval_fn = getattr(redis, "eval", None)
    if not callable(eval_fn):
        return False
    provider_name = token.provider_name
    dual_race = provider_name.startswith(_DUAL_RACE_SENTINEL_PREFIX)
    active_member = provider_name if dual_race else task_id
    result = await eval_fn(
        _RELEASE_GENERATION_QUEUE_STATE_LUA,
        5,
        _provider_active_key(provider_name),
        _IMAGE_QUEUE_ACTIVE_KEY,
        task_provider_key,
        task_lease_key,
        reservation_key,
        provider_name,
        token.lease_token or "",
        token.reservation_token or "",
        task_id,
        active_member,
        "1" if dual_race else "0",
    )
    return int(result or 0) == 1


__all__ = [
    "GenerationQueueReleaseToken",
    "capture_generation_queue_state",
    "completion_cancel_requires_durable_settlement",
    "completion_has_trustworthy_persisted_usage",
    "current_execution_epoch",
    "generation_cancel_requires_durable_settlement",
    "queued_generation_cleanup_entries",
    "release_generation_queue_state",
]
