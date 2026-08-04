"""Redis-backed journal for paid Telegram generation submissions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, cast

from redis import asyncio as aioredis

from .generation_state import (
    DurableGenerationSubmission,
    SubmissionJournalConflict,
    SubmissionJournalStatus,
    generation_request_fingerprint,
    generation_submission_idempotency_key,
    generation_submission_identity,
    generation_submission_operation_id,
)

_SUBMISSION_PREFIX = "tg:generation:submission:"
_UPDATE_PREFIX = "tg:generation:submission-update:"
_PENDING_PREFIX = "tg:generation:submission-pending:"
SUBMISSION_TTL_SECONDS = 90 * 24 * 3600

_STAGE_LUA = """
local update_operation = redis.call('GET', KEYS[1])
if update_operation then
  return {'update', update_operation}
end
local pending_operation = redis.call('GET', KEYS[2])
if pending_operation then
  redis.call('SET', KEYS[1], pending_operation, 'EX', tonumber(ARGV[8]))
  return {'pending', pending_operation}
end
redis.call(
  'HSET',
  KEYS[3],
  'operation_id', ARGV[1],
  'identity_hash', ARGV[2],
  'update_token', ARGV[3],
  'request_fingerprint', ARGV[4],
  'idempotency_key', ARGV[5],
  'payload', ARGV[6],
  'status', ARGV[7]
)
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[8]))
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[8]))
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[8]))
return {'created', ARGV[1]}
"""

_FINISH_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], 'request_fingerprint') ~= ARGV[1] then
  return -1
end
redis.call('HSET', KEYS[1], 'status', ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
if ARGV[3] == '1' then
  if redis.call('GET', KEYS[3]) == ARGV[5] then
    redis.call('DEL', KEYS[3])
  end
else
  local pending_operation = redis.call('GET', KEYS[3])
  if not pending_operation or pending_operation == ARGV[5] then
    redis.call('SET', KEYS[3], ARGV[5], 'EX', tonumber(ARGV[4]))
  end
end
return 1
"""


def _submission_key(operation_id: str) -> str:
    return f"{_SUBMISSION_PREFIX}{operation_id}"


def _update_key(identity_hash: str, update_token: str) -> str:
    update_hash = hashlib.sha256(update_token.encode("utf-8")).hexdigest()
    return f"{_UPDATE_PREFIX}{identity_hash}:{update_hash}"


def _pending_key(identity_hash: str, request_fingerprint: str) -> str:
    return f"{_PENDING_PREFIX}{identity_hash}:{request_fingerprint}"


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _load(
    client: aioredis.Redis,
    operation_id: str,
) -> DurableGenerationSubmission | None:
    raw = await cast(
        Awaitable[dict[Any, Any]],
        client.hgetall(_submission_key(operation_id)),
    )
    if not raw:
        return None
    data = {_decode(key): _decode(value) for key, value in raw.items()}
    try:
        payload = json.loads(data["payload"])
        status = SubmissionJournalStatus(data["status"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SubmissionJournalConflict(
            "generation submission journal record is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise SubmissionJournalConflict(
            "generation submission journal payload is invalid"
        )
    required = {
        "operation_id",
        "identity_hash",
        "request_fingerprint",
        "idempotency_key",
        "update_token",
    }
    if any(not data.get(field) for field in required):
        raise SubmissionJournalConflict(
            "generation submission journal record is incomplete"
        )
    return DurableGenerationSubmission(
        operation_id=data["operation_id"],
        identity_hash=data["identity_hash"],
        idempotency_key=data["idempotency_key"],
        request_fingerprint=data["request_fingerprint"],
        payload=payload,
        status=status,
        update_token=data["update_token"],
    )


async def stage_generation_submission(
    client: aioredis.Redis,
    *,
    chat_id: int,
    tg_user_id: int,
    update_token: str,
    payload: dict[str, Any],
) -> DurableGenerationSubmission:
    """Persist or recover a paid generation identity before HTTP submission."""

    if not update_token:
        raise ValueError("generation submission requires an update token")
    identity_hash = generation_submission_identity(chat_id, tg_user_id)
    request_fingerprint = generation_request_fingerprint(payload)
    operation_id = generation_submission_operation_id(
        chat_id,
        tg_user_id,
        update_token,
    )
    idempotency_key = generation_submission_idempotency_key(
        chat_id,
        tg_user_id,
        update_token,
    )
    canonical_payload = {
        **{key: value for key, value in payload.items() if key != "idempotency_key"},
        "idempotency_key": idempotency_key,
    }
    payload_json = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    update_key = _update_key(identity_hash, update_token)
    pending_key = _pending_key(identity_hash, request_fingerprint)
    operation_key = _submission_key(operation_id)

    for _attempt in range(2):
        result = await cast(
            Awaitable[list[Any]],
            client.eval(
                _STAGE_LUA,
                3,
                update_key,
                pending_key,
                operation_key,
                operation_id,
                identity_hash,
                update_token,
                request_fingerprint,
                idempotency_key,
                payload_json,
                SubmissionJournalStatus.PREPARED.value,
                str(SUBMISSION_TTL_SECONDS),
            ),
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise SubmissionJournalConflict(
                "generation submission journal returned an invalid reservation"
            )
        source = _decode(result[0])
        resolved_operation_id = _decode(result[1])
        submission = await _load(client, resolved_operation_id)
        if submission is None:
            stale_key = update_key if source == "update" else pending_key
            await client.delete(stale_key)
            continue
        if submission.request_fingerprint != request_fingerprint:
            raise SubmissionJournalConflict(
                "telegram update was already used for a different generation"
            )
        return submission
    raise SubmissionJournalConflict(
        "generation submission journal could not recover its reservation"
    )


async def finish_generation_submission(
    client: aioredis.Redis,
    submission: DurableGenerationSubmission,
    status: SubmissionJournalStatus,
) -> None:
    if status is SubmissionJournalStatus.PREPARED:
        raise ValueError("prepared is not a terminal submission update")
    if not submission.identity_hash:
        raise SubmissionJournalConflict(
            "generation submission journal is missing Telegram identity"
        )
    result = int(
        await cast(
            Awaitable[Any],
            client.eval(
                _FINISH_LUA,
                3,
                _submission_key(submission.operation_id),
                _update_key(submission.identity_hash, submission.update_token),
                _pending_key(
                    submission.identity_hash,
                    submission.request_fingerprint,
                ),
                submission.request_fingerprint,
                status.value,
                (
                    "1"
                    if status
                    in {
                        SubmissionJournalStatus.ACCEPTED,
                        SubmissionJournalStatus.REJECTED,
                    }
                    else "0"
                ),
                str(SUBMISSION_TTL_SECONDS),
                submission.operation_id,
            ),
        )
    )
    if result <= 0:
        raise SubmissionJournalConflict(
            "generation submission journal could not update its reservation"
        )
