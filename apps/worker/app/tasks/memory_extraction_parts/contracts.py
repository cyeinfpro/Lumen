from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from typing import Any

from lumen_core.memory import ExtractedMemory
from lumen_core.models import (
    Conversation,
    MemoryExtractionRun,
    Message,
    User,
)


@dataclass(frozen=True)
class MemoryExtractionEntities:
    user: User | None
    conversation: Conversation | None
    source_message: Message | None
    assistant_message: Message | None


@dataclass(frozen=True)
class MemoryExtractionClaim:
    run_id: str
    conversation_id: str
    user_id: str
    source_message_id: str
    assistant_message_id: str
    event_id: str
    owner: str
    job_id: str | None
    fence: int
    text: str
    extraction_threshold: float
    scope_hint: str | None


@dataclass(frozen=True)
class CompletedMemoryExtraction:
    run_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    assistant_message_id: str
    event_id: str
    fence: int
    writes: list[dict[str, Any]]
    undo_operations: list[dict[str, Any]]


@dataclass(frozen=True)
class PreparedMemoryCandidate:
    candidate: ExtractedMemory
    embedding: str


@dataclass(frozen=True)
class UndoTokenRequest:
    write_index: int
    payload: dict[str, Any]


def memory_extraction_event_id(
    source_message_id: str,
    assistant_message_id: str,
) -> str:
    return f"memory-extract:{source_message_id}:{assistant_message_id}"


def memory_extraction_owner(
    ctx: dict[str, Any],
    assistant_message_id: str,
) -> tuple[str, str | None]:
    raw_job_id = ctx.get("job_id")
    job_id = raw_job_id if isinstance(raw_job_id, str) and raw_job_id else None
    owner_prefix = job_id or f"inline:{assistant_message_id}"
    return f"{owner_prefix}:{secrets.token_urlsafe(18)}", job_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def copied_dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def memory_extraction_claim_matches(
    run: MemoryExtractionRun,
    claim: MemoryExtractionClaim,
) -> bool:
    return (
        run.id == claim.run_id
        and run.status == "running"
        and run.assistant_message_id == claim.assistant_message_id
        and run.event_id == claim.event_id
        and run.owner == claim.owner
        and run.fence == claim.fence
    )


def completed_memory_extraction(
    run: MemoryExtractionRun,
) -> CompletedMemoryExtraction:
    return CompletedMemoryExtraction(
        run_id=run.id,
        user_id=run.user_id,
        conversation_id=run.conversation_id,
        source_message_id=run.source_message_id,
        assistant_message_id=run.assistant_message_id,
        event_id=run.event_id,
        fence=run.fence,
        writes=copied_dict_items(run.memory_writes),
        undo_operations=copied_dict_items(run.undo_operations),
    )


def cancel_memory_extraction_run(
    run: MemoryExtractionRun,
    *,
    reason: str,
    now: datetime,
) -> None:
    run.status = "canceled"
    run.owner = None
    run.fence += 1
    run.lease_expires_at = None
    run.undo_expires_at = None
    run.canceled_at = now
    run.cancel_reason = reason[:160]
    run.retry_reason = None


def mark_memory_extraction_committed(
    run: MemoryExtractionRun,
    *,
    writes: list[dict[str, Any]],
    undo_operations: list[dict[str, Any]],
    now: datetime,
) -> None:
    run.status = "committed"
    run.lease_expires_at = None
    run.committed_at = now
    run.undo_expires_at = None
    run.retry_reason = None
    run.memory_writes = [dict(item) for item in writes]
    run.undo_operations = [dict(item) for item in undo_operations]
    run.undo_status = "pending" if undo_operations else "none"


def write_payload(
    *,
    id: str | None,
    kind: str,
    type: str | None,
    content: str,
    source_excerpt: str | None,
    undo_token: str | None = None,
    scope_id: str | None = None,
    recommended_scope_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "type": type,
        "content": content,
        "source_excerpt": source_excerpt,
        "undo_token": undo_token,
        "scope_id": scope_id,
        "recommended_scope_id": recommended_scope_id,
    }


def memory_write_identity(write: dict[str, Any]) -> tuple[Any, ...]:
    return (
        write.get("id"),
        write.get("kind"),
        write.get("type"),
        write.get("content"),
        write.get("source_excerpt"),
        write.get("scope_id"),
        write.get("recommended_scope_id"),
    )


def merge_memory_writes(
    existing: Any,
    writes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = copied_dict_items(existing)
    for write in writes:
        identity = memory_write_identity(write)
        existing_index = next(
            (
                index
                for index, item in enumerate(merged)
                if memory_write_identity(item) == identity
            ),
            None,
        )
        if existing_index is None:
            merged.append(write)
        else:
            merged[existing_index] = write
    return merged


def materialize_undo_operations(
    writes: list[dict[str, Any]],
    requests: list[UndoTokenRequest],
    *,
    token_factory: Callable[[], str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    new_token = token_factory or (lambda: secrets.token_urlsafe(24))
    materialized = [dict(write) for write in writes]
    operations: list[dict[str, Any]] = []
    for request in requests:
        if request.write_index < 0 or request.write_index >= len(materialized):
            continue
        token = new_token()
        materialized[request.write_index]["undo_token"] = token
        operations.append(
            {
                "write_index": request.write_index,
                "token": token,
                "payload": dict(request.payload),
                "expires_at": None,
            }
        )
    return materialized, operations


def drop_expired_undo_operations(
    writes: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    remaining: list[dict[str, Any]] = []
    changed = False
    for operation in operations:
        expires_at = parse_datetime(operation.get("expires_at"))
        if expires_at is None or expires_at > now:
            remaining.append(operation)
            continue
        write_index = operation.get("write_index")
        if (
            isinstance(write_index, int)
            and not isinstance(write_index, bool)
            and 0 <= write_index < len(writes)
        ):
            writes[write_index].pop("undo_token", None)
        changed = True
    return remaining, changed
