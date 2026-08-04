"""Stable dispatch identities shared by generation producers and consumers."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from arq.connections import job_key_prefix, result_key_prefix
from arq.constants import in_progress_key_prefix


DISPATCH_ACTIVE_PREFIX = "generation:dispatch:active:"
DISPATCH_REVISION_PREFIX = "generation:dispatch:revision:"
DISPATCH_RESERVATION_TTL_S = 180
DISPATCH_CONSUMED_TTL_S = 180
DISPATCH_REVISION_TTL_S = 7 * 24 * 3600
DISPATCH_CONTEXT_KEY = "_generation_dispatch_identity"

BEGIN_DISPATCH_LUA = """
local active_key = KEYS[1]
local revision_key = KEYS[2]
local attempt = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local revision_ttl = tonumber(ARGV[3])
local replace_value = ARGV[4]

local current = redis.call('GET', active_key)
if current then
  local current_attempt = tonumber(string.match(current, '^(%d+)|'))
  if current_attempt and current_attempt > attempt then
    return {0, current}
  end
  if current_attempt == attempt and current ~= replace_value then
    return {0, current}
  end
end

local revision = redis.call('INCR', revision_key)
redis.call('EXPIRE', revision_key, revision_ttl)
local value = tostring(attempt) .. '|' .. tostring(revision) .. '|reserved|'
redis.call('SET', active_key, value, 'EX', ttl)
return {1, value}
"""

MARK_DISPATCH_ENQUEUED_LUA = """
local current = redis.call('GET', KEYS[1])
if current ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""

CONSUME_DISPATCH_LUA = """
local current = redis.call('GET', KEYS[1])
local prefix = ARGV[1]
if not current or string.sub(current, 1, string.len(prefix)) ~= prefix then
  return 0
end
local phase = string.match(current, '^%d+|%d+|([^|]+)|')
if phase ~= 'reserved' and phase ~= 'enqueued' then
  return 0
end
redis.call('SET', KEYS[1], prefix .. 'consumed|' .. ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""

FINISH_DISPATCH_LUA = """
local current = redis.call('GET', KEYS[1])
local prefix = ARGV[1]
if not current or string.sub(current, 1, string.len(prefix)) ~= prefix then
  return 0
end
return redis.call('DEL', KEYS[1])
"""


@dataclass(frozen=True, slots=True)
class DispatchIdentity:
    generation_id: str
    attempt: int
    revision: int

    @property
    def value_prefix(self) -> str:
        return f"{self.attempt}|{self.revision}|"

    @property
    def reserved_value(self) -> str:
        return f"{self.value_prefix}reserved|"

    @property
    def job_id(self) -> str:
        return (
            f"lumen:generation:{self.generation_id}:attempt:{self.attempt}:"
            f"dispatch:{self.revision}"
        )


@dataclass(frozen=True, slots=True)
class DispatchBeginResult:
    identity: DispatchIdentity
    created: bool
    phase: str


@dataclass(frozen=True, slots=True)
class DispatchEnqueueResult:
    identity: DispatchIdentity
    created: bool
    enqueued: bool
    durable_evidence: bool

    @property
    def accepted(self) -> bool:
        return self.durable_evidence


def dispatch_active_key(task_id: str) -> str:
    return f"{DISPATCH_ACTIVE_PREFIX}{task_id}"


def dispatch_revision_key(task_id: str, attempt: int) -> str:
    return f"{DISPATCH_REVISION_PREFIX}{task_id}:{attempt}"


def dispatch_identity_from_context(ctx: dict[str, Any]) -> DispatchIdentity | None:
    value = ctx.get(DISPATCH_CONTEXT_KEY)
    return value if isinstance(value, DispatchIdentity) else None


def _redis_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _identity_from_value(task_id: str, value: Any) -> DispatchIdentity:
    parts = _redis_text(value).split("|", 3)
    if len(parts) < 3:
        raise ValueError("invalid generation dispatch state")
    return DispatchIdentity(
        generation_id=task_id,
        attempt=int(parts[0]),
        revision=int(parts[1]),
    )


def _phase_from_value(value: Any) -> str:
    parts = _redis_text(value).split("|", 3)
    if len(parts) < 3 or not parts[2]:
        raise ValueError("invalid generation dispatch state")
    return parts[2]


async def _has_durable_dispatch_evidence(
    redis: Any,
    identity: DispatchIdentity,
) -> bool:
    raw = await redis.get(dispatch_active_key(identity.generation_id))
    if raw is not None:
        value = _redis_text(raw)
        if value.startswith(identity.value_prefix):
            phase = _phase_from_value(value)
            if phase in {"enqueued", "consumed"}:
                return True
    return bool(
        await redis.exists(
            f"{job_key_prefix}{identity.job_id}",
            f"{result_key_prefix}{identity.job_id}",
            f"{in_progress_key_prefix}{identity.job_id}",
        )
    )


async def _mark_dispatch_enqueued_best_effort(
    redis: Any,
    identity: DispatchIdentity,
) -> None:
    # The ARQ record is the acceptance proof; this marker accelerates later dedupe.
    with suppress(Exception):
        await mark_generation_dispatch_enqueued(redis, identity)


async def begin_generation_dispatch(
    redis: Any,
    *,
    task_id: str,
    attempt: int,
    replace: DispatchIdentity | None = None,
) -> DispatchBeginResult:
    if attempt <= 0:
        raise ValueError("generation dispatch attempt must be positive")
    replace_value = ""
    if replace is not None:
        if replace.generation_id != task_id:
            raise ValueError("replacement dispatch belongs to another generation")
        raw = await redis.get(dispatch_active_key(task_id))
        if raw is not None and _redis_text(raw).startswith(replace.value_prefix):
            replace_value = _redis_text(raw)
    raw_result = await redis.eval(
        BEGIN_DISPATCH_LUA,
        2,
        dispatch_active_key(task_id),
        dispatch_revision_key(task_id, attempt),
        str(attempt),
        str(DISPATCH_RESERVATION_TTL_S),
        str(DISPATCH_REVISION_TTL_S),
        replace_value,
    )
    created_raw, value = raw_result
    return DispatchBeginResult(
        identity=_identity_from_value(task_id, value),
        created=bool(int(created_raw)),
        phase=_phase_from_value(value),
    )


async def mark_generation_dispatch_enqueued(
    redis: Any,
    identity: DispatchIdentity,
) -> bool:
    enqueued_value = f"{identity.value_prefix}enqueued|{identity.job_id}"
    result = await redis.eval(
        MARK_DISPATCH_ENQUEUED_LUA,
        1,
        dispatch_active_key(identity.generation_id),
        identity.reserved_value,
        enqueued_value,
        str(DISPATCH_RESERVATION_TTL_S),
    )
    return bool(int(result or 0))


async def enqueue_generation_dispatch(
    redis: Any,
    *,
    task_id: str,
    attempt: int,
    defer_by: int | float | None = None,
    job_try: int | None = None,
    replace: DispatchIdentity | None = None,
) -> DispatchEnqueueResult:
    begun = await begin_generation_dispatch(
        redis,
        task_id=task_id,
        attempt=attempt,
        replace=replace,
    )
    identity = begun.identity
    if not begun.created and begun.phase in {"enqueued", "consumed"}:
        return DispatchEnqueueResult(
            identity=identity,
            created=False,
            enqueued=False,
            durable_evidence=True,
        )
    if not begun.created and begun.phase != "reserved":
        return DispatchEnqueueResult(
            identity=identity,
            created=False,
            enqueued=False,
            durable_evidence=await _has_durable_dispatch_evidence(redis, identity),
        )
    kwargs: dict[str, Any] = {
        "_job_id": identity.job_id,
    }
    if defer_by is not None and defer_by > 0:
        kwargs["_defer_by"] = defer_by
    if job_try is not None:
        kwargs["_job_try"] = job_try
    try:
        job = await redis.enqueue_job(
            "run_generation",
            task_id,
            identity.attempt,
            identity.revision,
            **kwargs,
        )
    except Exception:
        try:
            durable_evidence = await _has_durable_dispatch_evidence(redis, identity)
        except Exception:
            durable_evidence = False
        if not durable_evidence:
            raise
        await _mark_dispatch_enqueued_best_effort(redis, identity)
        return DispatchEnqueueResult(
            identity=identity,
            created=begun.created,
            enqueued=False,
            durable_evidence=True,
        )
    if job is not None:
        await _mark_dispatch_enqueued_best_effort(redis, identity)
        durable_evidence = True
    else:
        durable_evidence = await _has_durable_dispatch_evidence(redis, identity)
        if durable_evidence:
            await _mark_dispatch_enqueued_best_effort(redis, identity)
    return DispatchEnqueueResult(
        identity=identity,
        created=begun.created,
        enqueued=job is not None,
        durable_evidence=durable_evidence,
    )


async def consume_generation_dispatch(
    redis: Any,
    identity: DispatchIdentity,
    *,
    worker_id: str,
) -> bool:
    result = await redis.eval(
        CONSUME_DISPATCH_LUA,
        1,
        dispatch_active_key(identity.generation_id),
        identity.value_prefix,
        worker_id,
        str(DISPATCH_CONSUMED_TTL_S),
    )
    return bool(int(result or 0))


async def finish_generation_dispatch(
    redis: Any,
    identity: DispatchIdentity,
) -> bool:
    result = await redis.eval(
        FINISH_DISPATCH_LUA,
        1,
        dispatch_active_key(identity.generation_id),
        identity.value_prefix,
    )
    return bool(int(result or 0))


__all__ = [
    "BEGIN_DISPATCH_LUA",
    "CONSUME_DISPATCH_LUA",
    "DISPATCH_CONTEXT_KEY",
    "DispatchBeginResult",
    "DispatchEnqueueResult",
    "DispatchIdentity",
    "FINISH_DISPATCH_LUA",
    "MARK_DISPATCH_ENQUEUED_LUA",
    "begin_generation_dispatch",
    "consume_generation_dispatch",
    "dispatch_active_key",
    "dispatch_identity_from_context",
    "dispatch_revision_key",
    "enqueue_generation_dispatch",
    "finish_generation_dispatch",
    "mark_generation_dispatch_enqueued",
]
