from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import math
import secrets
from typing import Any

from sqlalchemy import select

from lumen_core.models import MemoryExtractionRun, Message

from .contracts import (
    CompletedMemoryExtraction,
    completed_memory_extraction,
    copied_dict_items,
    drop_expired_undo_operations,
    merge_memory_writes,
    parse_datetime,
)
from .run_state import (
    invalid_memory_extraction_context,
    lock_memory_extraction_entities,
    lock_memory_extraction_run,
    lock_row,
)


@dataclass(frozen=True)
class CommittedDeliveryDependencies:
    session_factory: Any
    advisory_xact_lock: Callable[[Any, str], Awaitable[None]]
    append_writes_to_message: Callable[
        [Any, str, list[dict[str, Any]]],
        Awaitable[Message | None],
    ]
    now: Callable[[], datetime]
    logger: logging.Logger
    undo_ttl_seconds: int
    undo_cleanup_batch: int


@dataclass(frozen=True)
class RepairedUndoOperations:
    operations: list[dict[str, Any]]
    expiries: list[datetime]
    changed: bool


@dataclass(frozen=True)
class CommittedDeliveryRepair:
    writes: list[dict[str, Any]]
    undo_operations: list[dict[str, Any]]
    undo_status: str
    undo_expires_at: datetime | None
    assistant_projection: list[dict[str, Any]]
    run_changed: bool
    assistant_projection_changed: bool


async def append_memory_writes(
    session: Any,
    assistant_message_id: str,
    writes: list[dict[str, Any]],
) -> Message | None:
    if not writes:
        return await lock_row(session, Message, assistant_message_id)
    assistant_message = await lock_row(session, Message, assistant_message_id)
    if assistant_message is None:
        return None
    content = (
        dict(assistant_message.content)
        if isinstance(assistant_message.content, dict)
        else {}
    )
    content["memory_writes"] = merge_memory_writes(
        content.get("memory_writes"),
        writes,
    )
    assistant_message.content = content
    await session.flush()
    return assistant_message


def _normalize_undo_operation(
    operation: dict[str, Any],
    writes: list[dict[str, Any]],
    *,
    now: datetime,
    undo_ttl_seconds: int,
    token_factory: Callable[[], str],
) -> tuple[dict[str, Any] | None, datetime | None, bool]:
    write_index = operation.get("write_index")
    payload = operation.get("payload")
    if (
        not isinstance(write_index, int)
        or isinstance(write_index, bool)
        or write_index < 0
        or write_index >= len(writes)
        or not isinstance(payload, dict)
    ):
        return None, None, True

    changed = False
    token = operation.get("token")
    expires_at = parse_datetime(operation.get("expires_at"))
    if not isinstance(token, str) or not token:
        token = token_factory()
        changed = True
    if expires_at is None:
        expires_at = now + timedelta(seconds=undo_ttl_seconds)
        changed = True
    repaired = {
        "write_index": write_index,
        "token": token,
        "payload": dict(payload),
        "expires_at": expires_at.isoformat(),
    }
    if writes[write_index].get("undo_token") != token:
        writes[write_index]["undo_token"] = token
        changed = True
    return repaired, expires_at, changed


def repair_undo_operations(
    operations: list[dict[str, Any]],
    writes: list[dict[str, Any]],
    *,
    now: datetime,
    undo_ttl_seconds: int,
    token_factory: Callable[[], str] | None = None,
) -> RepairedUndoOperations:
    new_token = token_factory or (lambda: secrets.token_urlsafe(24))
    repaired_operations: list[dict[str, Any]] = []
    repaired_expiries: list[datetime] = []
    changed = False
    for operation in operations:
        repaired, expires_at, operation_changed = _normalize_undo_operation(
            operation,
            writes,
            now=now,
            undo_ttl_seconds=undo_ttl_seconds,
            token_factory=new_token,
        )
        changed = changed or operation_changed
        if repaired is None or expires_at is None:
            continue
        repaired_operations.append(repaired)
        repaired_expiries.append(expires_at)
    return RepairedUndoOperations(
        operations=repaired_operations,
        expiries=repaired_expiries,
        changed=changed,
    )


def _collection_length(value: Any) -> int:
    try:
        return len(value or [])
    except TypeError:
        return 0


def _expected_undo_status(
    current_status: str,
    operations: list[dict[str, Any]],
) -> str:
    if operations and current_status in {"pending", "ready"}:
        return current_status
    return "pending" if operations else "none"


def repair_committed_delivery_state(
    *,
    memory_writes: Any,
    undo_operations: Any,
    undo_status: str,
    undo_expires_at: Any,
    assistant_writes: Any,
    now: datetime,
    undo_ttl_seconds: int,
    token_factory: Callable[[], str] | None = None,
) -> CommittedDeliveryRepair:
    writes = copied_dict_items(memory_writes)
    original_operation_count = _collection_length(undo_operations)
    remaining_operations, expired_operations_dropped = drop_expired_undo_operations(
        writes,
        copied_dict_items(undo_operations),
        now=now,
    )
    repaired = repair_undo_operations(
        remaining_operations,
        writes,
        now=now,
        undo_ttl_seconds=undo_ttl_seconds,
        token_factory=token_factory,
    )
    expected_status = _expected_undo_status(undo_status, repaired.operations)
    expected_expiry = min(repaired.expiries) if repaired.expiries else None
    status_changed = (
        bool(repaired.operations) and undo_status not in {"pending", "ready"}
    ) or (not repaired.operations and undo_status != "none")
    expiry_changed = parse_datetime(undo_expires_at) != expected_expiry
    run_changed = (
        len(remaining_operations) != original_operation_count
        or expired_operations_dropped
        or repaired.changed
        or status_changed
        or expiry_changed
    )
    assistant_projection = merge_memory_writes(assistant_writes, writes)
    assistant_projection_changed = assistant_projection != copied_dict_items(
        assistant_writes
    )
    return CommittedDeliveryRepair(
        writes=writes,
        undo_operations=repaired.operations,
        undo_status=expected_status,
        undo_expires_at=expected_expiry,
        assistant_projection=assistant_projection,
        run_changed=run_changed,
        assistant_projection_changed=assistant_projection_changed,
    )


async def prune_expired_memory_extraction_undo(
    dependencies: CommittedDeliveryDependencies,
    event_id: str,
    *,
    now: datetime,
) -> bool:
    async with dependencies.session_factory() as session:
        preview = (
            await session.execute(
                select(MemoryExtractionRun).where(
                    MemoryExtractionRun.event_id == event_id
                )
            )
        ).scalar_one_or_none()
        if preview is None:
            await session.rollback()
            return False
        entities = await lock_memory_extraction_entities(
            session,
            conversation_id=preview.conversation_id,
            source_message_id=preview.source_message_id,
            assistant_message_id=preview.assistant_message_id,
            user_id=preview.user_id,
        )
        run = await lock_memory_extraction_run(
            session,
            event_id=event_id,
            advisory_xact_lock=dependencies.advisory_xact_lock,
        )
        if run is None or run.status != "committed":
            await session.rollback()
            return False

        writes = copied_dict_items(run.memory_writes)
        remaining, changed = drop_expired_undo_operations(
            writes,
            copied_dict_items(run.undo_operations),
            now=now,
        )
        if not changed:
            await session.rollback()
            return False

        if entities.assistant_message is not None:
            await dependencies.append_writes_to_message(
                session,
                run.assistant_message_id,
                writes,
            )
        remaining_expiries = [
            expires_at
            for operation in remaining
            if (expires_at := parse_datetime(operation.get("expires_at"))) is not None
        ]
        run.memory_writes = writes
        run.undo_operations = remaining
        run.undo_status = (
            run.undo_status
            if remaining and run.undo_status in {"pending", "ready"}
            else ("pending" if remaining else "none")
        )
        run.undo_expires_at = min(remaining_expiries) if remaining_expiries else None
        await session.commit()
        return True


async def load_committed_memory_extraction(
    dependencies: CommittedDeliveryDependencies,
    event_id: str,
) -> CompletedMemoryExtraction | None:
    async with dependencies.session_factory() as session:
        preview = (
            await session.execute(
                select(MemoryExtractionRun).where(
                    MemoryExtractionRun.event_id == event_id
                )
            )
        ).scalar_one_or_none()
        if preview is None:
            await session.rollback()
            return None
        entities = await lock_memory_extraction_entities(
            session,
            conversation_id=preview.conversation_id,
            source_message_id=preview.source_message_id,
            assistant_message_id=preview.assistant_message_id,
            user_id=preview.user_id,
        )
        run = await lock_memory_extraction_run(
            session,
            event_id=event_id,
            advisory_xact_lock=dependencies.advisory_xact_lock,
        )
        if run is None or run.status != "committed":
            await session.rollback()
            return None
        invalid_reason = invalid_memory_extraction_context(
            entities,
            conversation_id=run.conversation_id,
            source_message_id=run.source_message_id,
            assistant_message_id=run.assistant_message_id,
            user_id=run.user_id,
        )
        if invalid_reason is not None:
            await session.rollback()
            return None

        assistant_content = (
            entities.assistant_message.content
            if entities.assistant_message is not None
            and isinstance(entities.assistant_message.content, dict)
            else {}
        )
        repair = repair_committed_delivery_state(
            memory_writes=run.memory_writes,
            undo_operations=run.undo_operations,
            undo_status=run.undo_status,
            undo_expires_at=run.undo_expires_at,
            assistant_writes=assistant_content.get("memory_writes"),
            now=dependencies.now(),
            undo_ttl_seconds=dependencies.undo_ttl_seconds,
        )
        if repair.assistant_projection_changed:
            assistant_message = await dependencies.append_writes_to_message(
                session,
                run.assistant_message_id,
                repair.writes,
            )
            if assistant_message is None:
                await session.rollback()
                return None
        if repair.run_changed:
            run.memory_writes = repair.writes
            run.undo_operations = repair.undo_operations
            run.undo_status = repair.undo_status
            run.undo_expires_at = repair.undo_expires_at
        completed = completed_memory_extraction(run)
        if repair.run_changed or repair.assistant_projection_changed:
            await session.commit()
        else:
            await session.rollback()
        return completed


async def mark_undo_delivery_ready(
    dependencies: CommittedDeliveryDependencies,
    completed: CompletedMemoryExtraction,
) -> None:
    try:
        async with dependencies.session_factory() as session:
            run = await lock_memory_extraction_run(
                session,
                event_id=completed.event_id,
                advisory_xact_lock=dependencies.advisory_xact_lock,
            )
            if (
                run is None
                or run.id != completed.run_id
                or run.status != "committed"
                or run.fence != completed.fence
            ):
                await session.rollback()
                return
            run.undo_status = "ready" if run.undo_operations else "none"
            await session.commit()
    except Exception:  # noqa: BLE001
        dependencies.logger.warning(
            "memory_extraction.undo_delivery_status_failed event=%s fence=%s",
            completed.event_id,
            completed.fence,
            exc_info=True,
        )


async def restore_undo_tokens(
    dependencies: CommittedDeliveryDependencies,
    redis: Any,
    completed: CompletedMemoryExtraction,
) -> None:
    now = dependencies.now()
    for operation in completed.undo_operations:
        token = operation.get("token")
        payload = operation.get("payload")
        expires_at = parse_datetime(operation.get("expires_at"))
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(payload, dict)
            or expires_at is None
        ):
            continue
        ttl = math.ceil((expires_at - now).total_seconds())
        if ttl <= 0:
            continue
        await redis.setex(
            f"memory:undo:{token}",
            ttl,
            json.dumps(payload, separators=(",", ":")),
        )
    await mark_undo_delivery_ready(dependencies, completed)


async def cleanup_expired_memory_extraction_undo(
    dependencies: CommittedDeliveryDependencies,
    now: datetime,
) -> int:
    async with dependencies.session_factory() as session:
        event_ids = (
            (
                await session.execute(
                    select(MemoryExtractionRun.event_id)
                    .where(
                        MemoryExtractionRun.status == "committed",
                        MemoryExtractionRun.undo_expires_at.is_not(None),
                        MemoryExtractionRun.undo_expires_at <= now,
                    )
                    .order_by(
                        MemoryExtractionRun.undo_expires_at,
                        MemoryExtractionRun.event_id,
                    )
                    .limit(dependencies.undo_cleanup_batch)
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()
    cleaned = 0
    for event_id in event_ids:
        if await prune_expired_memory_extraction_undo(
            dependencies,
            event_id,
            now=now,
        ):
            cleaned += 1
    return cleaned
