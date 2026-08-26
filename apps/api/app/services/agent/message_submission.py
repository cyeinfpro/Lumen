"""Atomic Agent user-message, placeholder, run, hold, and Outbox creation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import (
    AGENT_RUN_ACTIVE_STATUSES,
    AGENT_TOOL_CREATE_IMAGE,
    EV_AGENT_RUN_QUEUED,
    AgentRunStatus,
)
from lumen_core.constants import MessageStatus, Role
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentSessionImage,
    AgentSession,
    Conversation,
    Generation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import (
    AGENT_MAX_SESSION_IMAGES,
    AgentMessageCreateIn,
    AgentMessageCreateOut,
    MessageOut,
    agent_message_request_fingerprint,
    stable_reference_label,
)

from ...audit import hash_email, request_ip_hash, write_audit
from ...config import settings
from ...deps import durable_session_id_from_db
from ..active_user import (
    ActiveUserFenceError,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from .common import (
    agent_provider_call_budget,
    agent_setting_int,
    http_error,
    publish_agent_events_best_effort,
    reserve_agent_text,
    stage_agent_event,
    stage_agent_run_dispatch,
)
from .repository import (
    get_owned_agent_session,
    load_agent_run_out,
    retention_filter,
)
from .session_images import session_image_slot_count
from .presentation import agent_default_params
from .reference_validation import (
    validate_reference_artifact as _validate_reference_artifact,
    validate_reference_images,
)
from .submission_planning import (
    ContinuationPlan as AgentContinuationSubmission,
    ExecutionPin as _ExecutionPin,
    SubmissionReference as _SessionReference,
    resolve_execution_pin as _resolve_execution_pin,
)


_PI_NATIVE_OUTPUT_RESERVE_TOKENS = 16_384


async def _idempotent_agent_message(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> AgentMessageCreateOut | None:
    run = (
        await db.execute(
            select(AgentRun)
            .join(AgentSession, AgentSession.id == AgentRun.agent_session_id)
            .join(Conversation, Conversation.id == AgentSession.conversation_id)
            .where(
                AgentRun.agent_session_id == session_id,
                AgentRun.user_id == user_id,
                AgentRun.idempotency_key == idempotency_key,
                AgentSession.user_id == user_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    if run.request_fingerprint != request_fingerprint:
        raise http_error(
            "idempotency_conflict",
            "idempotency_key was used with a different Agent request",
            409,
        )
    messages = list(
        (
            await db.execute(
                select(Message).where(
                    Message.id.in_([run.user_message_id, run.assistant_message_id]),
                    Message.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {message.id: message for message in messages}
    user_message = by_id.get(run.user_message_id)
    assistant_message = by_id.get(run.assistant_message_id)
    if user_message is None or assistant_message is None:
        raise http_error(
            "agent_snapshot_incomplete", "Agent snapshot is incomplete", 409
        )
    return AgentMessageCreateOut(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
        agent_run=await load_agent_run_out(db, run),
    )


@dataclass(frozen=True, slots=True)
class _StagedSubmission:
    user_message: Message
    assistant_message: Message
    run: AgentRun
    queued_event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SnapshotInput:
    body: AgentMessageCreateIn
    pin: _ExecutionPin
    catalog_references: tuple[_SessionReference, ...]
    run_reference_content: tuple[dict[str, Any], ...]
    max_image_tool_calls: int
    max_images_per_run: int


async def _request_snapshot(
    db: AsyncSession,
    value: _SnapshotInput,
) -> dict[str, Any]:
    body = value.body
    credential = value.pin.credential
    continuation = value.pin.continuation
    return {
        "runtime_request_version": 3,
        "image_defaults": body.image_defaults.model_dump(mode="json"),
        "allow_image": body.allow_image,
        "allowed_tools": [AGENT_TOOL_CREATE_IMAGE] if body.allow_image else [],
        "references": [
            {
                "reference_label": item["reference_label"],
                "role": item["role"],
                "display_label": item["label"],
            }
            for item in value.run_reference_content
        ],
        "session_catalog": [
            {
                "image_id": reference.image_id,
                "reference_label": reference.reference_label,
                "role": reference.role,
                "display_label": reference.label,
            }
            for reference in value.catalog_references
        ],
        "execution_policy": "pi-native",
        "tool_policy": {
            "max_image_tool_calls": (
                value.max_image_tool_calls if body.allow_image else 0
            ),
            "max_images_per_run": value.max_images_per_run,
        },
        "provider_dispatch": {
            "version": 1,
            "max_dispatches": agent_provider_call_budget(
                value.max_image_tool_calls if body.allow_image else 0
            ),
        },
        "context_plan": {
            "version": 1,
            "mode": value.pin.context_plan,
            "estimated_input_tokens": value.pin.estimated_input_tokens,
            "context_window": value.pin.context_window,
            "max_output_tokens": value.pin.max_output_tokens,
        },
        "internal_agent_callback_base_url": settings.agent_internal_callback_base_url,
        "tool_receipt": {"version": 2},
        "reference_policy": {
            "max_reference_images": await agent_setting_int(
                db, "agent.max_reference_images"
            ),
            "max_session_images": await agent_setting_int(
                db, "agent.max_session_images"
            ),
        },
        "eligible_provider_names": list(value.pin.provider_names),
        "credential_capabilities": (
            dict(credential.capabilities_jsonb or {}) if credential else {}
        ),
        **(
            {
                "operation": "continue",
                "continuation_source_run_id": continuation.source_run_id,
            }
            if continuation
            else {}
        ),
    }


@dataclass(frozen=True, slots=True)
class _SubmissionReferences:
    catalog: tuple[_SessionReference, ...]
    turn: tuple[_SessionReference, ...]


def _apply_image_visibility(statement: Any, visible: Any | None) -> Any:
    return statement if visible is None else statement.where(visible)


async def _session_references(
    db: AsyncSession,
    *,
    session: AgentSession,
    user: User,
    body: AgentMessageCreateIn,
) -> list[_SessionReference]:
    catalog_rows = list(
        (
            await db.execute(
                select(AgentSessionImage).where(
                    AgentSessionImage.agent_session_id == session.id,
                    AgentSessionImage.user_id == user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    catalog_by_image = {row.image_id: row for row in catalog_rows}
    used_labels = {row.reference_label for row in catalog_rows}

    async def allocate(
        *,
        image_id: str,
        role: str,
        display_label: str | None,
        source: str,
        active: bool,
        preferred_label: str | None = None,
    ) -> AgentSessionImage | None:
        existing = catalog_by_image.get(image_id)
        if existing is not None:
            if active:
                existing.active = True
                existing.role = role
                existing.display_label = display_label
                existing.source = source
            return existing
        label = preferred_label if preferred_label not in used_labels else None
        if label is None:
            label = next(
                (
                    stable_reference_label(index)
                    for index in range(AGENT_MAX_SESSION_IMAGES)
                    if stable_reference_label(index) not in used_labels
                ),
                None,
            )
        if label is None and active:
            reusable = next((row for row in catalog_rows if not row.active), None)
            if reusable is not None:
                label = reusable.reference_label
                catalog_rows.remove(reusable)
                catalog_by_image.pop(reusable.image_id, None)
                await db.delete(reusable)
                await db.flush()
        if label is None:
            return None
        used_labels.add(label)
        row = AgentSessionImage(
            agent_session_id=session.id,
            user_id=user.id,
            image_id=image_id,
            reference_label=label,
            role=role,
            display_label=display_label,
            source=source,
            active=active,
        )
        db.add(row)
        catalog_rows.append(row)
        catalog_by_image[image_id] = row
        return row

    # Explicit selections have priority over lazy history/output backfill.
    for attachment in body.attachments:
        row = await allocate(
            image_id=attachment.image_id,
            role=attachment.role,
            display_label=attachment.label,
            source="current",
            active=True,
        )
        if row is None:
            raise http_error(
                "agent_session_reference_limit_reached",
                "Agent session reference image limit reached",
                422,
                max_session_images=AGENT_MAX_SESSION_IMAGES,
            )

    historical_statement = (
        select(AgentRunReference)
        .join(AgentRun, AgentRun.id == AgentRunReference.agent_run_id)
        .join(Image, Image.id == AgentRunReference.image_id)
        .where(
            AgentRun.agent_session_id == session.id,
            AgentRun.user_id == user.id,
            AgentRunReference.user_id == user.id,
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        .order_by(AgentRun.created_at, AgentRun.id, AgentRunReference.ordinal)
    )
    visible = await retention_filter(db, user, Image.created_at)
    historical_statement = _apply_image_visibility(historical_statement, visible)
    historical = list((await db.execute(historical_statement)).scalars().all())
    for reference in historical:
        if reference.image_id in catalog_by_image:
            continue
        await allocate(
            image_id=reference.image_id,
            role=reference.role,
            display_label=reference.display_label,
            source="history",
            active=True,
            preferred_label=reference.reference_label,
        )

    assistant_ids = select(AgentRun.assistant_message_id).where(
        AgentRun.agent_session_id == session.id,
        AgentRun.user_id == user.id,
    )
    generated_statement = (
        select(Image)
        .join(Generation, Generation.id == Image.owner_generation_id)
        .where(
            Generation.user_id == user.id,
            Generation.message_id.in_(assistant_ids),
            Generation.status == "succeeded",
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        .order_by(Generation.created_at, Generation.id, Image.id)
    )
    generated_statement = _apply_image_visibility(generated_statement, visible)
    for image in (await db.execute(generated_statement)).scalars():
        if image.id in catalog_by_image:
            continue
        await allocate(
            image_id=image.id,
            role="reference",
            display_label="Agent result",
            source="generated",
            active=False,
        )

    active_rows = sorted(
        (row for row in catalog_rows if row.active),
        key=lambda row: int(row.reference_label.removeprefix("ref_")),
    )
    return [
        _SessionReference(
            image_id=row.image_id,
            reference_label=row.reference_label,
            role=row.role,
            label=row.display_label,
            source=row.source,
        )
        for row in active_rows
    ]


async def _validate_submission_slot(
    db: AsyncSession,
    *,
    session: AgentSession,
    user: User,
    body: AgentMessageCreateIn,
) -> _SubmissionReferences:
    active_run_id = (
        await db.execute(
            select(AgentRun.id).where(
                AgentRun.agent_session_id == session.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active_run_id is not None:
        raise http_error(
            "agent_run_active",
            "this Agent session already has an active run",
            409,
            agent_run_id=active_run_id,
        )
    max_references = await agent_setting_int(db, "agent.max_reference_images")
    if len(body.attachments) > max_references:
        raise http_error(
            "agent_reference_limit_reached",
            "too many Agent reference images",
            422,
            max_reference_images=max_references,
        )
    await validate_reference_images(
        db,
        user=user,
        image_ids=[attachment.image_id for attachment in body.attachments],
        artifact_validator=_validate_reference_artifact,
    )
    references = await _session_references(
        db,
        session=session,
        user=user,
        body=body,
    )
    max_session_images = await agent_setting_int(db, "agent.max_session_images")
    session_image_visibility = await retention_filter(db, user, Image.created_at)
    session_image_slots = await session_image_slot_count(
        db,
        session_id=session.id,
        user_id=user.id,
        snapshotted_image_ids={reference.image_id for reference in references},
        image_visibility_filter=session_image_visibility,
    )
    if session_image_slots > max_session_images:
        raise http_error(
            "agent_session_reference_limit_reached",
            "Agent session reference image limit reached",
            422,
            max_session_images=max_session_images,
            reference_images=session_image_slots,
        )
    by_image = {reference.image_id: reference for reference in references}
    turn = tuple(by_image[attachment.image_id] for attachment in body.attachments)
    return _SubmissionReferences(catalog=tuple(references), turn=turn)


async def _stage_submission(
    db: AsyncSession,
    *,
    user_id: str,
    user: User,
    account_mode: str,
    session: AgentSession,
    conversation: Conversation,
    body: AgentMessageCreateIn,
    request_fingerprint: str,
    request: Any | None,
    pin: _ExecutionPin,
    catalog_references: tuple[_SessionReference, ...],
    references: tuple[_SessionReference, ...],
) -> _StagedSubmission:
    continuation = pin.continuation
    now = datetime.now(timezone.utc)
    references_by_image = {
        reference.image_id: reference for reference in catalog_references
    }
    reference_content = [
        {
            "image_id": attachment.image_id,
            "role": attachment.role,
            "label": attachment.label,
            "reference_label": references_by_image[attachment.image_id].reference_label,
        }
        for attachment in body.attachments
    ]
    run_reference_content = [
        {
            "image_id": reference.image_id,
            "role": reference.role,
            "label": reference.label,
            "source": reference.source,
            "reference_label": reference.reference_label,
        }
        for reference in references
    ]
    user_message = Message(
        conversation_id=conversation.id,
        role=Role.SYSTEM.value if continuation else Role.USER.value,
        content=(
            {
                "source": "agent-continuation",
                "continuation_source_run_id": continuation.source_run_id,
            }
            if continuation
            else {
                "text": body.text,
                "source": "agent",
                "attachments": reference_content,
            }
        ),
        intent="agent",
        status=None,
    )
    db.add(user_message)
    await db.flush()
    run_id = new_uuid7()
    assistant_message = Message(
        id=new_uuid7(),
        conversation_id=conversation.id,
        role=Role.ASSISTANT.value,
        content={
            "text": "",
            "source": "agent",
            "agent_run_id": run_id,
            "tool_calls": [],
            "generation_ids": [],
        },
        parent_message_id=(
            continuation.source_user_message_id if continuation else user_message.id
        ),
        intent="agent",
        status=MessageStatus.PENDING.value,
    )
    db.add(assistant_message)
    await db.flush()
    credential = pin.credential
    run = AgentRun(
        id=run_id,
        agent_session_id=session.id,
        user_id=user_id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        continuation_source_run_id=(
            continuation.source_run_id if continuation else None
        ),
        status=AgentRunStatus.QUEUED.value,
        execution_epoch=0,
        attempt=0,
        last_event_seq=0,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
        request_snapshot_jsonb={},
        account_mode_snapshot=account_mode,
        system_prompt_snapshot=(
            continuation.system_prompt if continuation else pin.system_prompt
        ),
        model=pin.model,
        reasoning_effort=body.reasoning_effort,
        user_api_credential_id=credential.credential_id if credential else None,
        upstream_supplier_id=credential.supplier_id if credential else None,
        text_hold_micro=0,
        billing_jsonb={},
        dispatch_jsonb={},
        usage_jsonb={},
        turn_count=0,
        tool_call_count=0,
    )
    db.add(run)
    await db.flush()
    max_image_tool_calls = await agent_setting_int(db, "agent.max_image_tool_calls")
    max_images_per_run = await agent_setting_int(db, "agent.max_images_per_run")
    run.request_snapshot_jsonb = await _request_snapshot(
        db,
        _SnapshotInput(
            body=body,
            pin=pin,
            catalog_references=catalog_references,
            run_reference_content=tuple(run_reference_content),
            max_image_tool_calls=max_image_tool_calls,
            max_images_per_run=max_images_per_run,
        ),
    )
    for index, reference in enumerate(references):
        db.add(
            AgentRunReference(
                agent_run_id=run.id,
                user_id=user_id,
                image_id=reference.image_id,
                ordinal=index,
                reference_label=reference.reference_label,
                role=reference.role,
                display_label=reference.label,
                metadata_jsonb={"source": reference.source},
            )
        )
    reservation = await reserve_agent_text(
        db,
        run=run,
        user_id=user_id,
        account_mode=account_mode,
        model=pin.model,
        text=body.text,
        reference_count=len(references),
        context_window=pin.context_window,
        provider_max_output_tokens=pin.max_output_tokens,
        max_image_tool_calls=(max_image_tool_calls if body.allow_image else 0),
    )
    run.text_hold_micro = reservation.hold_micro
    run.billing_jsonb = reservation.billing_snapshot
    stage_agent_run_dispatch(db, run)
    queued_event = stage_agent_event(db, run=run, event_name=EV_AGENT_RUN_QUEUED)
    conversation.last_activity_at = now
    conversation.default_params = agent_default_params(
        image_defaults=body.image_defaults,
        allow_image=body.allow_image,
        existing=conversation.default_params,
    )
    session.updated_at = now
    await write_audit(
        db,
        event_type="agent.run.create",
        user_id=user_id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "agent_session_id": session.id,
            "agent_run_id": run.id,
            "explicit_reference_count": len(body.attachments),
            "session_reference_count": len(catalog_references),
            "allow_image": body.allow_image,
            "account_mode": account_mode,
        },
        autocommit=False,
    )
    return _StagedSubmission(user_message, assistant_message, run, queued_event)


async def submit_agent_message(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    body: AgentMessageCreateIn,
    request: Any | None,
    continuation: AgentContinuationSubmission | None = None,
) -> AgentMessageCreateOut:
    request_user_id = str(user.id)
    request_fingerprint = agent_message_request_fingerprint(body)
    prior = await _idempotent_agent_message(
        db,
        session_id=session_id,
        user_id=request_user_id,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if prior is not None:
        return prior
    expected_account_mode = account_mode_from_user(user)
    try:
        snapshot = await lock_active_user_snapshot(
            db,
            request_user_id,
            expected_account_mode,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    user = snapshot.user
    session, conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=request_user_id,
        for_update=True,
    )
    prior = await _idempotent_agent_message(
        db,
        session_id=session.id,
        user_id=request_user_id,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if prior is not None:
        await db.rollback()
        return prior
    submission_references = await _validate_submission_slot(
        db,
        session=session,
        user=user,
        body=body,
    )
    pin = await _resolve_execution_pin(
        db,
        user_id=request_user_id,
        user=user,
        account_mode=snapshot.account_mode,
        conversation=conversation,
        body=body,
        references=submission_references.turn,
    )
    pin = replace(pin, continuation=continuation)
    staged = await _stage_submission(
        db,
        user_id=request_user_id,
        user=user,
        account_mode=snapshot.account_mode,
        session=session,
        conversation=conversation,
        body=body,
        request_fingerprint=request_fingerprint,
        request=request,
        pin=pin,
        catalog_references=submission_references.catalog,
        references=submission_references.turn,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await _idempotent_agent_message(
            db,
            session_id=session_id,
            user_id=request_user_id,
            idempotency_key=body.idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if prior is not None:
            return prior
        raise http_error(
            "agent_run_active",
            "this Agent session already has an active run",
            409,
        )
    await db.refresh(staged.user_message)
    await db.refresh(staged.assistant_message)
    await db.refresh(staged.run)
    await publish_agent_events_best_effort(
        user_id=request_user_id,
        agent_session_id=session.id,
        events=[staged.queued_event],
    )
    return AgentMessageCreateOut(
        user_message=MessageOut.model_validate(staged.user_message),
        assistant_message=MessageOut.model_validate(staged.assistant_message),
        agent_run=await load_agent_run_out(db, staged.run),
    )


__all__ = ["AgentContinuationSubmission", "submit_agent_message"]
