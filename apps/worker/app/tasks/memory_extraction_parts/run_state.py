from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from lumen_core.constants import MessageStatus, Role
from lumen_core.memory import canonical_memory_text
from lumen_core.models import (
    Conversation,
    MemoryAudit,
    MemoryExtractionRun,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)

from .contracts import (
    CompletedMemoryExtraction,
    MemoryExtractionClaim,
    MemoryExtractionEntities,
    PreparedMemoryCandidate,
    UndoTokenRequest,
    cancel_memory_extraction_run,
    completed_memory_extraction,
    mark_memory_extraction_committed,
    materialize_undo_operations,
    memory_extraction_claim_matches,
    parse_datetime,
    write_payload,
)


@dataclass(frozen=True)
class MemoryExtractionStateDependencies:
    session_factory: Any
    advisory_xact_lock: Callable[[Any, str], Awaitable[None]]
    append_writes_to_message: Callable[
        [Any, str, list[dict[str, Any]]],
        Awaitable[Message | None],
    ]
    default_scope: Callable[[Any, str], Awaitable[UserMemoryScope]]
    text_from_message: Callable[[Message | None], str]
    topic_key: Callable[[str], str]
    bump_positive_signal: Callable[[Any], None]
    now: Callable[[], datetime]
    lease_seconds: int
    staging_ttl_days: int


async def lock_row(session: Any, model: Any, row_id: str) -> Any:
    return (
        await session.execute(
            select(model)
            .where(model.id == row_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def lock_memory_extraction_entities(
    session: Any,
    *,
    conversation_id: str,
    source_message_id: str,
    assistant_message_id: str,
    user_id: str | None = None,
) -> MemoryExtractionEntities:
    resolved_user_id = user_id
    if resolved_user_id is None:
        resolved_user_id = (
            await session.execute(
                select(Conversation.user_id).where(Conversation.id == conversation_id)
            )
        ).scalar_one_or_none()
    user = (
        await lock_row(session, User, resolved_user_id)
        if isinstance(resolved_user_id, str) and resolved_user_id
        else None
    )
    conversation = await lock_row(session, Conversation, conversation_id)
    source_message = await lock_row(session, Message, source_message_id)
    assistant_message = await lock_row(session, Message, assistant_message_id)
    return MemoryExtractionEntities(
        user=user,
        conversation=conversation,
        source_message=source_message,
        assistant_message=assistant_message,
    )


def invalid_memory_extraction_context(
    entities: MemoryExtractionEntities,
    *,
    conversation_id: str,
    source_message_id: str,
    assistant_message_id: str,
    user_id: str | None = None,
) -> str | None:
    user = entities.user
    conversation = entities.conversation
    source_message = entities.source_message
    assistant_message = entities.assistant_message
    if user is None:
        return "user_missing"
    if user_id is not None and user.id != user_id:
        return "user_mismatch"
    if getattr(user, "deleted_at", None) is not None:
        return "user_deleted"
    if conversation is None:
        return "conversation_missing"
    if conversation.id != conversation_id or conversation.user_id != user.id:
        return "conversation_mismatch"
    if getattr(conversation, "deleted_at", None) is not None:
        return "conversation_deleted"
    if source_message is None:
        return "source_message_missing"
    if (
        source_message.id != source_message_id
        or source_message.conversation_id != conversation_id
        or source_message.role != Role.USER.value
    ):
        return "source_message_mismatch"
    if getattr(source_message, "deleted_at", None) is not None:
        return "source_message_deleted"
    if assistant_message is None:
        return "assistant_message_missing"
    if (
        assistant_message.id != assistant_message_id
        or assistant_message.conversation_id != conversation_id
        or assistant_message.role != Role.ASSISTANT.value
        or assistant_message.parent_message_id != source_message_id
    ):
        return "assistant_message_mismatch"
    if getattr(assistant_message, "deleted_at", None) is not None:
        return "assistant_message_deleted"
    if assistant_message.status in {
        MessageStatus.CANCELED.value,
        "cancelled",
    }:
        return "assistant_message_canceled"
    return None


async def lock_memory_extraction_run(
    session: Any,
    *,
    event_id: str,
    advisory_xact_lock: Callable[[Any, str], Awaitable[None]],
) -> MemoryExtractionRun | None:
    await advisory_xact_lock(session, f"memory_extract_run:{event_id}")
    return (
        await session.execute(
            select(MemoryExtractionRun)
            .where(MemoryExtractionRun.event_id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def claim_memory_extraction(
    dependencies: MemoryExtractionStateDependencies,
    *,
    conversation_id: str,
    source_message_id: str,
    assistant_message_id: str,
    event_id: str,
    owner: str,
    job_id: str | None,
) -> MemoryExtractionClaim | CompletedMemoryExtraction | None:
    async with dependencies.session_factory() as session:
        entities = await lock_memory_extraction_entities(
            session,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            assistant_message_id=assistant_message_id,
        )
        run = await lock_memory_extraction_run(
            session,
            event_id=event_id,
            advisory_xact_lock=dependencies.advisory_xact_lock,
        )
        invalid_reason = invalid_memory_extraction_context(
            entities,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            assistant_message_id=assistant_message_id,
        )
        if invalid_reason is not None:
            if run is not None and run.status in {"pending", "running", "retryable"}:
                cancel_memory_extraction_run(
                    run,
                    reason=invalid_reason,
                    now=dependencies.now(),
                )
                await session.commit()
            else:
                await session.rollback()
            return None

        user = entities.user
        conversation = entities.conversation
        source_message = entities.source_message
        if user is None or conversation is None or source_message is None:
            await session.rollback()
            return None
        if user.memory_disabled or user.memory_paused or conversation.memory_disabled:
            if run is not None and run.status in {"pending", "running", "retryable"}:
                cancel_memory_extraction_run(
                    run,
                    reason="memory_disabled",
                    now=dependencies.now(),
                )
                await session.commit()
            else:
                await session.rollback()
            return None

        if run is None:
            run = MemoryExtractionRun(
                event_id=event_id,
                user_id=user.id,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                assistant_message_id=assistant_message_id,
                status="pending",
            )
            session.add(run)
            await session.flush()
        elif (
            run.user_id != user.id
            or run.conversation_id != conversation_id
            or run.source_message_id != source_message_id
            or run.assistant_message_id != assistant_message_id
        ):
            await session.rollback()
            return None

        if run.status == "committed":
            completed = completed_memory_extraction(run)
            await session.rollback()
            return completed
        if run.status == "canceled":
            await session.rollback()
            return None

        active_scope = (
            await lock_row(session, UserMemoryScope, conversation.active_scope_id)
            if conversation.active_scope_id
            else None
        )
        scope_hint = (
            active_scope.name
            if active_scope is not None and not active_scope.is_default
            else None
        )
        now = dependencies.now()
        lease_expires_at = parse_datetime(run.lease_expires_at)
        lease_active = (
            run.status == "running"
            and lease_expires_at is not None
            and lease_expires_at > now
        )
        if lease_active and run.owner != owner:
            await session.rollback()
            return None

        previous_owner = run.owner
        if previous_owner and previous_owner != owner:
            run.recovery_count += 1
        run.status = "running"
        run.owner = owner
        run.job_id = job_id
        run.fence += 1
        run.attempt += 1
        run.claimed_at = now
        run.lease_expires_at = now + timedelta(seconds=dependencies.lease_seconds)
        run.retry_reason = None
        run.canceled_at = None
        run.cancel_reason = None
        claim = MemoryExtractionClaim(
            run_id=run.id,
            conversation_id=conversation_id,
            user_id=user.id,
            source_message_id=source_message_id,
            assistant_message_id=assistant_message_id,
            event_id=event_id,
            owner=owner,
            job_id=job_id,
            fence=run.fence,
            text=dependencies.text_from_message(source_message),
            extraction_threshold=max(
                0.6,
                min(0.95, float(user.extraction_threshold or 0.80)),
            ),
            scope_hint=scope_hint,
        )
        await session.commit()
        return claim


async def abandon_memory_extraction_claim(
    dependencies: MemoryExtractionStateDependencies,
    claim: MemoryExtractionClaim,
    *,
    reason: str,
) -> bool:
    async with dependencies.session_factory() as session:
        run = await lock_memory_extraction_run(
            session,
            event_id=claim.event_id,
            advisory_xact_lock=dependencies.advisory_xact_lock,
        )
        if run is None or not memory_extraction_claim_matches(run, claim):
            await session.rollback()
            return False
        run.status = "retryable"
        run.lease_expires_at = dependencies.now()
        run.retry_reason = reason[:160]
        await session.commit()
        return True


async def finalize_memory_extraction(
    dependencies: MemoryExtractionStateDependencies,
    claim: MemoryExtractionClaim,
    *,
    prepared_candidates: list[PreparedMemoryCandidate],
    rejected_pii: bool,
) -> CompletedMemoryExtraction | None:
    async with dependencies.session_factory() as session:
        entities = await lock_memory_extraction_entities(
            session,
            conversation_id=claim.conversation_id,
            source_message_id=claim.source_message_id,
            assistant_message_id=claim.assistant_message_id,
            user_id=claim.user_id,
        )
        run = await lock_memory_extraction_run(
            session,
            event_id=claim.event_id,
            advisory_xact_lock=dependencies.advisory_xact_lock,
        )
        if run is None or not memory_extraction_claim_matches(run, claim):
            await session.rollback()
            return None

        invalid_reason = invalid_memory_extraction_context(
            entities,
            conversation_id=claim.conversation_id,
            source_message_id=claim.source_message_id,
            assistant_message_id=claim.assistant_message_id,
            user_id=claim.user_id,
        )
        if invalid_reason is not None:
            cancel_memory_extraction_run(
                run,
                reason=invalid_reason,
                now=dependencies.now(),
            )
            await session.commit()
            return None

        conversation = entities.conversation
        source_message = entities.source_message
        user = entities.user
        if conversation is None or source_message is None or user is None:
            await session.rollback()
            return None
        if user.memory_disabled or user.memory_paused or conversation.memory_disabled:
            mark_memory_extraction_committed(
                run,
                writes=[],
                undo_operations=[],
                now=dependencies.now(),
            )
            await session.commit()
            return completed_memory_extraction(run)
        if not prepared_candidates:
            writes = (
                [
                    write_payload(
                        id=None,
                        kind="rejected_pii",
                        type=None,
                        content="",
                        source_excerpt=None,
                    )
                ]
                if rejected_pii
                else []
            )
            await dependencies.append_writes_to_message(
                session,
                claim.assistant_message_id,
                writes,
            )
            mark_memory_extraction_committed(
                run,
                writes=writes,
                undo_operations=[],
                now=dependencies.now(),
            )
            await session.commit()
            return completed_memory_extraction(run)

        await dependencies.advisory_xact_lock(
            session,
            f"memory_extract_user:{claim.user_id}",
        )
        default_scope = await dependencies.default_scope(session, user.id)
        scope_id = conversation.active_scope_id or default_scope.id
        existing = (
            (
                await session.execute(
                    select(UserMemory).where(
                        UserMemory.user_id == user.id,
                        UserMemory.disabled.is_(False),
                        UserMemory.superseded_by.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        writes: list[dict[str, Any]] = []
        undo_requests: list[UndoTokenRequest] = []
        if rejected_pii:
            writes.append(
                write_payload(
                    id=None,
                    kind="rejected_pii",
                    type=None,
                    content="",
                    source_excerpt=None,
                )
            )

        for prepared in prepared_candidates:
            candidate = prepared.candidate
            duplicate = next(
                (
                    memory
                    for memory in existing
                    if memory.type == candidate.type
                    and canonical_memory_text(memory.content)
                    == canonical_memory_text(candidate.content)
                ),
                None,
            )
            if duplicate is not None:
                dependencies.bump_positive_signal(duplicate)
                session.add(
                    MemoryAudit(
                        user_id=user.id,
                        memory_id=duplicate.id,
                        event_type="merged",
                        old_content=duplicate.content,
                        new_content=duplicate.content,
                        source_message_id=source_message.id,
                        details={"source": "auto"},
                    )
                )
                undo_requests.append(
                    UndoTokenRequest(
                        write_index=len(writes),
                        payload={
                            "user_id": user.id,
                            "action": "merged",
                            "memory_id": duplicate.id,
                            "candidate": {
                                "type": candidate.type,
                                "content": candidate.content,
                                "source_excerpt": candidate.source_excerpt,
                                "source_message_id": source_message.id,
                                "scope_id": scope_id,
                                "source": "auto",
                                "confidence": candidate.confidence,
                            },
                        },
                    )
                )
                writes.append(
                    write_payload(
                        id=duplicate.id,
                        kind="merged",
                        type=duplicate.type,
                        content=duplicate.content,
                        source_excerpt=candidate.source_excerpt,
                        scope_id=duplicate.scope_id,
                        recommended_scope_id=scope_id,
                    )
                )
                continue

            conflict = next(
                (
                    memory
                    for memory in existing
                    if dependencies.topic_key(memory.content)
                    and dependencies.topic_key(memory.content)
                    == dependencies.topic_key(candidate.content)
                    and memory.type != candidate.type
                ),
                None,
            )
            if (
                candidate.confidence < claim.extraction_threshold
                and candidate.intent_kind != "directive"
            ):
                staging = UserMemoryStaging(
                    user_id=user.id,
                    type=candidate.type,
                    content=candidate.content,
                    source_message_id=source_message.id,
                    source_excerpt=candidate.source_excerpt,
                    source="auto",
                    embedding=prepared.embedding,
                    confidence=candidate.confidence,
                    scope_id=scope_id,
                    recommended_scope_id=scope_id,
                    decision="pending",
                    expires_at=dependencies.now()
                    + timedelta(days=dependencies.staging_ttl_days),
                )
                session.add(staging)
                await session.flush()
                undo_requests.append(
                    UndoTokenRequest(
                        write_index=len(writes),
                        payload={
                            "user_id": user.id,
                            "action": "staged",
                            "staging_id": staging.id,
                        },
                    )
                )
                writes.append(
                    write_payload(
                        id=staging.id,
                        kind="staged",
                        type=staging.type,
                        content=staging.content,
                        source_excerpt=staging.source_excerpt,
                        scope_id=staging.scope_id,
                        recommended_scope_id=staging.recommended_scope_id,
                    )
                )
                continue

            memory = UserMemory(
                user_id=user.id,
                type=candidate.type,
                content=candidate.content,
                source_message_id=source_message.id,
                source_excerpt=candidate.source_excerpt,
                source=("explicit" if candidate.intent_kind == "directive" else "auto"),
                embedding=prepared.embedding,
                confidence=max(candidate.confidence, claim.extraction_threshold),
                scope_id=scope_id,
                last_used_at=dependencies.now(),
            )
            session.add(memory)
            await session.flush()
            kind = "added"
            details: dict[str, Any] = {"source": memory.source}
            if conflict is not None:
                conflict.superseded_by = memory.id
                kind = "superseded"
                details["superseded_memory_id"] = conflict.id
            session.add(
                MemoryAudit(
                    user_id=user.id,
                    memory_id=memory.id,
                    event_type=kind,
                    old_content=conflict.content if conflict is not None else None,
                    new_content=memory.content,
                    source_message_id=source_message.id,
                    details=details,
                )
            )
            undo_requests.append(
                UndoTokenRequest(
                    write_index=len(writes),
                    payload={
                        "user_id": user.id,
                        "action": kind,
                        "memory_id": memory.id,
                        "old_memory_id": (
                            conflict.id if conflict is not None else None
                        ),
                    },
                )
            )
            writes.append(
                write_payload(
                    id=memory.id,
                    kind=kind,
                    type=memory.type,
                    content=memory.content,
                    source_excerpt=memory.source_excerpt,
                    scope_id=memory.scope_id,
                    recommended_scope_id=scope_id,
                )
            )
            existing.append(memory)

        writes, undo_operations = materialize_undo_operations(
            writes,
            undo_requests,
        )
        assistant_message = await dependencies.append_writes_to_message(
            session,
            claim.assistant_message_id,
            writes,
        )
        if assistant_message is None:
            await session.rollback()
            return None
        mark_memory_extraction_committed(
            run,
            writes=writes,
            undo_operations=undo_operations,
            now=dependencies.now(),
        )
        await session.commit()
        return completed_memory_extraction(run)
