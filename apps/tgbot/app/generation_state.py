"""Generation flow epochs and durable submission-journal contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any
import uuid

from aiogram.fsm.context import FSMContext

_GENERATION_FLOW_EPOCH_FIELD = "generation_flow_epoch"
_PENDING_GENERATION_FIELD = "pending_generation"


class SubmissionDisposition(Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class SubmissionJournalStatus(Enum):
    PREPARED = "prepared"
    AMBIGUOUS = "ambiguous"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DurableGenerationSubmission:
    operation_id: str
    identity_hash: str
    idempotency_key: str
    request_fingerprint: str
    payload: dict[str, Any]
    status: SubmissionJournalStatus
    update_token: str


class SubmissionJournalConflict(RuntimeError):
    pass


def semantic_generation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "idempotency_key"}


def generation_request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        semantic_generation_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generation_submission_identity(chat_id: int, tg_user_id: int) -> str:
    return hashlib.sha256(f"{chat_id}\0{tg_user_id}".encode("utf-8")).hexdigest()


def generation_submission_operation_id(
    chat_id: int,
    tg_user_id: int,
    update_token: str,
) -> str:
    return hashlib.sha256(
        f"{chat_id}\0{tg_user_id}\0{update_token}".encode("utf-8")
    ).hexdigest()


def generation_submission_idempotency_key(
    chat_id: int,
    tg_user_id: int,
    update_token: str,
) -> str:
    operation_id = generation_submission_operation_id(
        chat_id,
        tg_user_id,
        update_token,
    )
    return f"tg:{operation_id[:61]}"


def new_generation_flow_epoch() -> str:
    return uuid.uuid4().hex


def generation_flow_epoch(data: dict[str, Any]) -> str | None:
    raw = data.get(_GENERATION_FLOW_EPOCH_FIELD)
    if not isinstance(raw, str) or not raw:
        return None
    return raw


async def ensure_generation_flow_epoch(
    state: FSMContext,
    data: dict[str, Any] | None = None,
) -> str:
    current = generation_flow_epoch(data if data is not None else await state.get_data())
    if current is not None:
        return current
    current = new_generation_flow_epoch()
    await state.update_data(**{_GENERATION_FLOW_EPOCH_FIELD: current})
    return current


async def generation_flow_is_current(state: FSMContext, expected_epoch: str) -> bool:
    return generation_flow_epoch(await state.get_data()) == expected_epoch


def pending_generation(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get(_PENDING_GENERATION_FIELD)
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt")
    idempotency_key = raw.get("idempotency_key")
    if not isinstance(prompt, str) or not prompt:
        return None
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return None
    return dict(raw)


def _semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return semantic_generation_payload(payload)


async def resolve_or_stage_generation(
    state: FSMContext,
    candidate: dict[str, Any],
    *,
    expected_flow_epoch: str | None = None,
) -> dict[str, Any] | None:
    """Reuse a pending payload, or stage the first exact payload before HTTP I/O."""

    data = await state.get_data()
    if (
        expected_flow_epoch is not None
        and generation_flow_epoch(data) != expected_flow_epoch
    ):
        return None
    existing = pending_generation(data)
    if existing is not None:
        if _semantic_payload(existing) != _semantic_payload(candidate):
            return None
        return existing

    payload = dict(candidate)
    prompt = payload.get("prompt")
    idempotency_key = payload.get("idempotency_key")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("generation payload requires a prompt")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("generation payload requires an idempotency_key")
    await state.update_data(**{_PENDING_GENERATION_FIELD: payload})
    return payload
