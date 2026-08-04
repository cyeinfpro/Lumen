"""Durable idempotency for paid prompt-enhancement streams."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.media_workflows import WorkflowRun

from ...idempotency.advisory import lock_user_key
from ...services.active_user import ActiveUserSnapshot
from .upstream import has_nonempty_text, terminal_chunk_kind, text_delta_from_chunk


TEXT_PROMPT_ENHANCE_OPERATION = "prompt_enhancement.text"
VIDEO_PROMPT_ENHANCE_OPERATION = "prompt_enhancement.video"

_LOCK_NAMESPACE = "paid-prompt-enhancement"
_RECORD_TYPE = "prompt_enhancement_operation"
_RECORD_METADATA_KEY = "paid_prompt_enhancement_idempotency"
PROMPT_OPERATION_RECORD_CONFIGS = (
    (_RECORD_TYPE, _RECORD_METADATA_KEY),
    (
        "telegram_prompt_enhancement_operation",
        "telegram_prompt_enhancement_idempotency",
    ),
)
_MAX_IDEMPOTENCY_KEY_LENGTH = 96
_RECORD_ID_NAMESPACE = uuid.UUID("5adb30b8-27bd-4a26-845b-3cd7e91ea913")
_LEASE_SECONDS = 45.0
_RECORD_LOCK_NAMESPACE = "paid-prompt-enhancement-record"
_FINALIZATION_ACTIONS = frozenset(
    {"none", "charge", "settle_default", "release", "preserve_hold"}
)


class AttemptOwnershipLost(RuntimeError):
    """Raised when an expired or superseded producer tries to mutate an operation."""


@dataclass(frozen=True, slots=True)
class PromptEnhanceOperation:
    user_id: str
    idempotency_key: str
    client_key_hash: str
    operation_namespace: str
    request_fingerprint: str
    record_id: str
    record_type: str = _RECORD_TYPE
    metadata_key: str = _RECORD_METADATA_KEY
    lock_namespace: str = _LOCK_NAMESPACE
    lock_subject: str | None = None


@dataclass(frozen=True, slots=True)
class PromptEnhanceAttempt:
    number: int
    owner_token: str
    lease_expires_at: datetime
    billing_request_id: str


@dataclass(frozen=True, slots=True)
class PromptEnhanceRecovery:
    response_chunks: tuple[str, ...]
    terminal_chunk: str
    terminal_state: str
    billing_action: str
    billing_capture: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PromptEnhanceReservation:
    replay_chunks: tuple[str, ...] | None = None
    attempt: PromptEnhanceAttempt | None = None
    billing_snapshot: dict[str, Any] | None = None
    recovery: PromptEnhanceRecovery | None = None
    active_user_snapshot: ActiveUserSnapshot | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def resolve_client_idempotency_key(raw_key: str | None) -> str:
    if raw_key is None:
        raise _http(
            "idempotency_key_required",
            "Idempotency-Key is required for paid prompt enhancement",
            422,
        )
    if (
        not raw_key
        or raw_key != raw_key.strip()
        or len(raw_key) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in raw_key)
    ):
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must be 1 to 96 printable ASCII characters",
            422,
        )
    return raw_key


def canonical_request_fingerprint(
    operation_namespace: str,
    payload: Any,
) -> str:
    encoded = json.dumps(
        {
            "operation_namespace": operation_namespace,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_record_id(user_id: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            _RECORD_ID_NAMESPACE,
            f"{user_id}\0{idempotency_key}",
        )
    )


def prompt_enhance_operation(
    *,
    user_id: str,
    idempotency_key: str,
    operation_namespace: str,
    payload: Any,
) -> PromptEnhanceOperation:
    return PromptEnhanceOperation(
        user_id=user_id,
        idempotency_key=idempotency_key,
        client_key_hash=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
        operation_namespace=operation_namespace,
        request_fingerprint=canonical_request_fingerprint(
            operation_namespace,
            payload,
        ),
        record_id=_operation_record_id(user_id, idempotency_key),
    )


def _record(
    run: WorkflowRun,
    operation: PromptEnhanceOperation,
) -> dict[str, Any] | None:
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    record = metadata.get(operation.metadata_key)
    return record if isinstance(record, dict) else None


def _replace_record(
    run: WorkflowRun,
    operation: PromptEnhanceOperation,
    record: dict[str, Any],
) -> None:
    metadata = dict(run.metadata_jsonb or {})
    metadata[operation.metadata_key] = record
    run.metadata_jsonb = metadata


def _ensure_matching_operation(
    run: WorkflowRun,
    operation: PromptEnhanceOperation,
) -> dict[str, Any]:
    record = _record(run, operation)
    if (
        run.user_id != operation.user_id
        or run.type != operation.record_type
        or record is None
        or record.get("client_key_hash") != operation.client_key_hash
        or record.get("operation_namespace") != operation.operation_namespace
        or record.get("request_fingerprint") != operation.request_fingerprint
    ):
        raise _http(
            "idempotency_conflict",
            "idempotency_key was already used for a different operation",
            409,
        )
    return record


async def _load_run(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
) -> WorkflowRun | None:
    if isinstance(db, AsyncSession):
        return (
            await db.execute(
                select(WorkflowRun)
                .where(WorkflowRun.id == operation.record_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    return await db.get(WorkflowRun, operation.record_id)


async def _lock_operation(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
) -> None:
    await lock_user_key(
        db,
        operation.lock_namespace,
        operation.lock_subject or operation.user_id,
        operation.idempotency_key,
    )
    await lock_prompt_operation_record(db, operation.record_id)


async def lock_prompt_operation_record(db: AsyncSession, record_id: str) -> None:
    await lock_user_key(db, _RECORD_LOCK_NAMESPACE, "record", record_id)


def _parse_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _attempt_from_record(record: dict[str, Any]) -> PromptEnhanceAttempt | None:
    number = record.get("attempt")
    owner_token = record.get("lease_owner")
    lease_expires_at = _parse_datetime(record.get("lease_expires_at"))
    billing_request_id = record.get("billing_request_id")
    if (
        not isinstance(number, int)
        or number <= 0
        or not isinstance(owner_token, str)
        or not owner_token
        or lease_expires_at is None
        or not isinstance(billing_request_id, str)
        or not billing_request_id
    ):
        return None
    return PromptEnhanceAttempt(
        number=number,
        owner_token=owner_token,
        lease_expires_at=lease_expires_at,
        billing_request_id=billing_request_id,
    )


def _new_attempt(
    record: dict[str, Any],
    *,
    billing_request_id: str,
    now: datetime,
    lease_seconds: float,
) -> PromptEnhanceAttempt:
    current = record.get("attempt")
    number = max(0, current if isinstance(current, int) else 0) + 1
    attempt = PromptEnhanceAttempt(
        number=number,
        owner_token=uuid.uuid4().hex,
        lease_expires_at=now + timedelta(seconds=max(1.0, lease_seconds)),
        billing_request_id=billing_request_id,
    )
    record.update(
        {
            "attempt": attempt.number,
            "lease_owner": attempt.owner_token,
            "lease_expires_at": attempt.lease_expires_at.isoformat(),
            "billing_request_id": attempt.billing_request_id,
        }
    )
    return attempt


def _require_attempt_owner(
    record: dict[str, Any],
    attempt: PromptEnhanceAttempt,
    *,
    now: datetime | None = None,
) -> None:
    active = _attempt_from_record(record)
    current = now or _utcnow()
    if (
        record.get("state") != "running"
        or active is None
        or active.number != attempt.number
        or active.owner_token != attempt.owner_token
        or active.billing_request_id != attempt.billing_request_id
        or active.lease_expires_at <= current
    ):
        raise AttemptOwnershipLost(
            "prompt enhancement attempt lease expired or ownership changed"
        )


def _refresh_attempt_lease(
    record: dict[str, Any],
    attempt: PromptEnhanceAttempt,
    *,
    lease_seconds: float | None = None,
) -> None:
    _require_attempt_owner(record, attempt)
    expires_at = _utcnow() + timedelta(
        seconds=max(1.0, lease_seconds or _LEASE_SECONDS)
    )
    record["lease_expires_at"] = expires_at.isoformat()


def _validated_replay_chunks(record: dict[str, Any]) -> tuple[str, ...]:
    raw_chunks = record.get("response_chunks")
    if not isinstance(raw_chunks, list):
        raise _http(
            "idempotency_replay_unavailable",
            "idempotency response is not available",
            409,
        )
    chunks = tuple(chunk for chunk in raw_chunks if isinstance(chunk, str))
    terminal_state = record.get("state")
    valid_terminal = bool(chunks) and terminal_chunk_kind(chunks[-1]) == terminal_state
    valid_success = terminal_state != "succeeded" or has_nonempty_text(chunks)
    if len(chunks) != len(raw_chunks) or not valid_terminal or not valid_success:
        raise _http(
            "idempotency_replay_unavailable",
            "idempotency response is invalid",
            409,
        )
    return chunks


def _billing_snapshot(record: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = record.get("billing")
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _billing_action_for_interruption(record: dict[str, Any]) -> str:
    snapshot = _billing_snapshot(record)
    if snapshot is None or snapshot.get("mode") != "wallet":
        return "none"
    hold_amount = snapshot.get("hold_amount_micro")
    if not isinstance(hold_amount, int) or hold_amount <= 0:
        return "none"
    return "settle_default"


def _interrupted_cost_possible(record: dict[str, Any]) -> bool:
    chunks = record.get("response_chunks")
    return bool(
        record.get("dispatch_inflight")
        or record.get("upstream_cost_possible")
        or (isinstance(chunks, list) and has_nonempty_text(chunks))
    )


def _finalization_from_record(
    record: dict[str, Any],
) -> PromptEnhanceRecovery | None:
    finalization = record.get("finalization")
    if not isinstance(finalization, dict):
        return None
    terminal_state = finalization.get("terminal_state")
    terminal_chunk = finalization.get("terminal_chunk")
    billing_action = finalization.get("billing_action")
    reason = finalization.get("reason")
    capture = finalization.get("billing_capture")
    raw_chunks = record.get("response_chunks")
    if (
        terminal_state not in {"succeeded", "failed"}
        or not isinstance(terminal_chunk, str)
        or terminal_chunk_kind(terminal_chunk) != terminal_state
        or billing_action not in _FINALIZATION_ACTIONS
        or not isinstance(raw_chunks, list)
        or any(not isinstance(chunk, str) for chunk in raw_chunks)
        or (terminal_state == "succeeded" and not has_nonempty_text(raw_chunks))
        or (capture is not None and not isinstance(capture, dict))
        or (reason is not None and not isinstance(reason, str))
    ):
        raise RuntimeError("prompt enhancement recovery checkpoint is invalid")
    return PromptEnhanceRecovery(
        response_chunks=tuple(raw_chunks),
        terminal_chunk=terminal_chunk,
        terminal_state=terminal_state,
        billing_action=billing_action,
        billing_capture=dict(capture) if isinstance(capture, dict) else None,
        reason=reason,
    )


def _set_finalization(
    record: dict[str, Any],
    *,
    terminal_state: str,
    terminal_chunk: str,
    billing_action: str,
    billing_capture: dict[str, Any] | None,
    reason: str | None,
) -> None:
    if terminal_state not in {"succeeded", "failed"}:
        raise ValueError("invalid prompt enhancement terminal state")
    if terminal_chunk_kind(terminal_chunk) != terminal_state:
        raise ValueError("prompt enhancement terminal chunk does not match state")
    if billing_action not in _FINALIZATION_ACTIONS:
        raise ValueError("invalid prompt enhancement billing action")
    chunks = record.get("response_chunks")
    if terminal_state == "succeeded" and (
        not isinstance(chunks, list) or not has_nonempty_text(chunks)
    ):
        raise ValueError("successful prompt enhancement must contain non-empty text")
    record["finalization"] = {
        "terminal_state": terminal_state,
        "terminal_chunk": terminal_chunk,
        "billing_action": billing_action,
        "billing_capture": (
            dict(billing_capture) if isinstance(billing_capture, dict) else None
        ),
        "reason": reason,
        "checkpointed_at": _utcnow().isoformat(),
    }
    record["phase"] = "finalizing"
    record["dispatch_inflight"] = False


def _new_operation_record(
    operation: PromptEnhanceOperation,
    *,
    now: datetime,
    lease_seconds: float,
) -> tuple[dict[str, Any], PromptEnhanceAttempt]:
    record: dict[str, Any] = {
        "client_key_hash": operation.client_key_hash,
        "operation_namespace": operation.operation_namespace,
        "request_fingerprint": operation.request_fingerprint,
        "state": "running",
        "phase": "reserved",
        "response_chunks": [],
        "dispatch_inflight": False,
        "upstream_cost_possible": False,
    }
    attempt = _new_attempt(
        record,
        billing_request_id=operation.record_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    return record, attempt


def _takeover_record(
    record: dict[str, Any],
    *,
    operation: PromptEnhanceOperation,
    now: datetime,
    lease_seconds: float,
) -> tuple[PromptEnhanceAttempt, PromptEnhanceRecovery | None]:
    recovery = _finalization_from_record(record)
    if recovery is None and _interrupted_cost_possible(record):
        _set_finalization(
            record,
            terminal_state="failed",
            terminal_chunk='data: {"error": "upstream_error"}\n\n',
            billing_action=_billing_action_for_interruption(record),
            billing_capture=None,
            reason="stale_attempt_interrupted",
        )
        recovery = _finalization_from_record(record)
    if recovery is None:
        record.update(
            {
                "phase": "billing_ready" if _billing_snapshot(record) else "reserved",
                "response_chunks": [],
                "dispatch_inflight": False,
                "upstream_cost_possible": False,
            }
        )
        record.pop("finalization", None)
    billing_request_id = record.get("billing_request_id")
    if not isinstance(billing_request_id, str) or not billing_request_id:
        billing_request_id = operation.record_id
    attempt = _new_attempt(
        record,
        billing_request_id=billing_request_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    record["phase"] = "recovering" if recovery is not None else record["phase"]
    return attempt, recovery


async def reserve_prompt_enhance_operation(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    *,
    lease_seconds: float | None = None,
    before_write: Callable[[], Awaitable[ActiveUserSnapshot]] | None = None,
) -> PromptEnhanceReservation:
    await _lock_operation(db, operation)
    now = _utcnow()
    active_lease_seconds = lease_seconds or _LEASE_SECONDS
    existing = await _load_run(db, operation)
    if existing is None:
        active_user_snapshot = await before_write() if before_write is not None else None
        record, attempt = _new_operation_record(
            operation,
            now=now,
            lease_seconds=active_lease_seconds,
        )
        run = WorkflowRun(
            id=operation.record_id,
            conversation_id=None,
            user_id=operation.user_id,
            type=operation.record_type,
            status="running",
            title="",
            user_prompt="",
            product_image_ids=[],
            current_step=operation.operation_namespace,
            quality_mode="standard",
            deleted_at=now,
            metadata_jsonb={operation.metadata_key: record},
        )
        db.add(run)
        await db.flush()
        return PromptEnhanceReservation(
            attempt=attempt,
            active_user_snapshot=active_user_snapshot,
        )

    record = _ensure_matching_operation(existing, operation)
    if record.get("state") != "running":
        return PromptEnhanceReservation(replay_chunks=_validated_replay_chunks(record))
    active_attempt = _attempt_from_record(record)
    if active_attempt is not None and active_attempt.lease_expires_at > now:
        raise _http(
            "idempotency_in_progress",
            "the original prompt enhancement is still running",
            425,
        )

    active_user_snapshot = await before_write() if before_write is not None else None
    updated = dict(record)
    attempt, recovery = _takeover_record(
        updated,
        operation=operation,
        now=now,
        lease_seconds=active_lease_seconds,
    )
    _replace_record(existing, operation, updated)
    existing.status = "running"
    return PromptEnhanceReservation(
        attempt=attempt,
        billing_snapshot=_billing_snapshot(updated),
        recovery=recovery,
        active_user_snapshot=active_user_snapshot,
    )


async def bind_billing_snapshot(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
    snapshot: dict[str, Any],
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise RuntimeError("prompt enhancement idempotency record disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _require_attempt_owner(record, attempt)
    existing = _billing_snapshot(record)
    if existing is not None and existing != snapshot:
        raise RuntimeError("prompt enhancement billing identity changed")
    record["billing"] = dict(snapshot)
    record["phase"] = "billing_ready"
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)


async def renew_attempt_lease(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)
    await db.commit()


async def assert_attempt_owner(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    _require_attempt_owner(_ensure_matching_operation(run, operation), attempt)


async def record_dispatch_intent(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _require_attempt_owner(record, attempt)
    record["phase"] = "dispatching"
    record["dispatch_inflight"] = True
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)
    await db.commit()


async def record_candidate_outcome(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
    *,
    upstream_cost_possible: bool,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _require_attempt_owner(record, attempt)
    record["dispatch_inflight"] = False
    record["upstream_cost_possible"] = bool(
        record.get("upstream_cost_possible") or upstream_cost_possible
    )
    record["phase"] = "streaming" if record.get("response_chunks") else "billing_ready"
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)
    await db.commit()


async def checkpoint_response_chunk(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
    *,
    sequence: int,
    chunk: str,
) -> None:
    if text_delta_from_chunk(chunk) is None:
        raise ValueError("only prompt enhancement text chunks can be checkpointed")
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _require_attempt_owner(record, attempt)
    raw_chunks = record.get("response_chunks")
    chunks = list(raw_chunks) if isinstance(raw_chunks, list) else []
    if len(chunks) == sequence:
        chunks.append(chunk)
    elif len(chunks) <= sequence or chunks[sequence] != chunk:
        raise AttemptOwnershipLost("prompt enhancement response checkpoint diverged")
    record["response_chunks"] = chunks
    record["phase"] = "streaming"
    record["dispatch_inflight"] = False
    record["upstream_cost_possible"] = True
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)
    await db.commit()


async def checkpoint_finalization(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    attempt: PromptEnhanceAttempt,
    *,
    terminal_state: str,
    terminal_chunk: str,
    billing_action: str,
    billing_capture: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise AttemptOwnershipLost("prompt enhancement operation disappeared")
    record = dict(_ensure_matching_operation(run, operation))
    _require_attempt_owner(record, attempt)
    _set_finalization(
        record,
        terminal_state=terminal_state,
        terminal_chunk=terminal_chunk,
        billing_action=billing_action,
        billing_capture=billing_capture,
        reason=reason,
    )
    _refresh_attempt_lease(record, attempt)
    _replace_record(run, operation, record)
    await db.commit()


def _record_terminal_response(
    run: WorkflowRun,
    operation: PromptEnhanceOperation,
    *,
    attempt: PromptEnhanceAttempt | None,
    chunks: list[str],
    terminal_state: str,
) -> None:
    record = dict(_ensure_matching_operation(run, operation))
    current_state = record.get("state")
    if current_state != "running":
        if current_state == terminal_state and record.get("response_chunks") == chunks:
            return
        raise AttemptOwnershipLost("prompt enhancement terminal state already changed")
    if attempt is not None:
        _require_attempt_owner(record, attempt)
    if (
        not chunks
        or terminal_chunk_kind(chunks[-1]) != terminal_state
        or (terminal_state == "succeeded" and not has_nonempty_text(chunks))
    ):
        raise ValueError("invalid prompt enhancement terminal response")
    recovery = _finalization_from_record(record)
    if recovery is not None and (
        recovery.terminal_state != terminal_state
        or recovery.terminal_chunk != chunks[-1]
    ):
        raise AttemptOwnershipLost("prompt enhancement finalization checkpoint changed")
    completed_at = _utcnow().isoformat()
    record.update(
        {
            "state": terminal_state,
            "phase": "terminal",
            "response_chunks": list(chunks),
            "completed_at": completed_at,
            "lease_expires_at": completed_at,
        }
    )
    _replace_record(run, operation, record)
    run.status = terminal_state


async def persist_terminal_response(
    db: AsyncSession,
    operation: PromptEnhanceOperation,
    *,
    chunks: list[str],
    terminal_state: str,
    attempt: PromptEnhanceAttempt | None = None,
) -> None:
    await _lock_operation(db, operation)
    run = await _load_run(db, operation)
    if run is None:
        raise RuntimeError("prompt enhancement idempotency record disappeared")
    _record_terminal_response(
        run,
        operation,
        attempt=attempt,
        chunks=chunks,
        terminal_state=terminal_state,
    )
    await db.commit()
