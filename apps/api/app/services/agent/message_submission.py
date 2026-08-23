"""Atomic Agent user-message, placeholder, run, hold, and Outbox creation."""

from __future__ import annotations

import asyncio
import io
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image as PILImage
from fastapi import HTTPException

from lumen_core.agent_events import (
    AGENT_RUN_ACTIVE_STATUSES,
    AGENT_TOOL_CREATE_IMAGE,
    EV_AGENT_RUN_QUEUED,
    AgentRunStatus,
)
from lumen_core.agent_model_profiles import default_agent_context_window
from lumen_core.constants import MessageStatus, Role
from lumen_core.context_window import estimate_text_tokens
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
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
from .. import storage_files
from ..active_user import (
    ActiveUserFenceError,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ..message_submission_prompting import (
    TaskCredentialPin,
    resolve_system_prompt_for_message,
    resolve_task_credential_pin,
)
from .common import (
    agent_setting_int,
    byok_vision_supported,
    http_error,
    publish_agent_events_best_effort,
    reserve_agent_text,
    stage_agent_event,
    stage_agent_run_dispatch,
    wallet_chat_provider_preflight,
)
from .repository import (
    get_owned_agent_session,
    load_agent_run_out,
    retention_filter,
)
from .session_images import session_image_slot_count
from .presentation import agent_default_params


_REFERENCE_MIME_FORMATS = MappingProxyType(
    {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }
)
_REFERENCE_MAX_BYTES = 32 * 1024 * 1024
_REFERENCE_MAX_PIXELS = 50_000_000
_REFERENCE_CONTEXT_TOKENS = 2048
_AGENT_CONTEXT_SAFETY_TOKENS = 8192


def _read_reference_bytes(image: Image) -> bytes:
    path = storage_files.resolve_storage_path(
        settings.storage_root,
        image.storage_key,
        error_factory=lambda code, message, status: http_error(
            "invalid_attachment", message, status, reason=code
        ),
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise http_error(
                "invalid_attachment",
                "reference image artifact is not a regular file",
                422,
            )
        if metadata.st_size > _REFERENCE_MAX_BYTES:
            raise http_error("invalid_attachment", "reference image is too large", 422)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(_REFERENCE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _REFERENCE_MAX_BYTES:
        raise http_error("invalid_attachment", "reference image is too large", 422)
    return raw


async def _validate_reference_artifact(image: Image) -> None:
    if image.artifact_status != "ready":
        raise http_error(
            "agent_reference_not_ready",
            "reference image is not ready",
            409,
        )
    if (
        image.mime not in _REFERENCE_MIME_FORMATS
        or image.size_bytes < 1
        or image.size_bytes > _REFERENCE_MAX_BYTES
        or image.width < 1
        or image.height < 1
        or image.width * image.height > _REFERENCE_MAX_PIXELS
    ):
        raise http_error(
            "invalid_attachment",
            "reference image metadata is invalid",
            422,
        )
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_read_reference_bytes, image),
            timeout=10,
        )
        with PILImage.open(io.BytesIO(raw)) as decoded:
            if decoded.format != _REFERENCE_MIME_FORMATS[image.mime]:
                raise ValueError("reference image format does not match its MIME type")
            if decoded.width * decoded.height > _REFERENCE_MAX_PIXELS:
                raise ValueError("reference image exceeds the pixel limit")
            decoded.verify()
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(
            "invalid_attachment",
            "reference image could not be decoded",
            422,
        ) from exc


async def _validate_reference_images(
    db: AsyncSession,
    *,
    user: User,
    image_ids: list[str],
) -> None:
    if not image_ids:
        return
    statement = select(Image).where(
        Image.id.in_(image_ids),
        Image.user_id == user.id,
        Image.deleted_at.is_(None),
    )
    visible = await retention_filter(db, user, Image.created_at)
    if visible is not None:
        statement = statement.where(visible)
    rows = list((await db.execute(statement)).scalars().all())
    if {image.id for image in rows} != set(image_ids):
        raise http_error(
            "invalid_attachment",
            "one or more reference images are not owned or were deleted",
            400,
        )
    by_id = {image.id: image for image in rows}
    for image_id in image_ids:
        await _validate_reference_artifact(by_id[image_id])


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
class _ExecutionPin:
    model: str
    provider_names: tuple[str, ...]
    credential: TaskCredentialPin | None
    system_prompt: str | None
    context_window: int
    max_output_tokens: int
    reasoning_supported: bool


def _capability_int(
    capabilities: dict[str, Any] | None,
    key: str,
    default: int,
    maximum: int,
) -> int:
    raw = capabilities.get(key) if isinstance(capabilities, dict) else None
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


@dataclass(frozen=True, slots=True)
class _StagedSubmission:
    user_message: Message
    assistant_message: Message
    run: AgentRun
    queued_event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SessionReference:
    image_id: str
    reference_label: str
    role: str
    label: str | None
    source: str


def _next_reference_label(used_labels: set[str]) -> str:
    for index in range(AGENT_MAX_SESSION_IMAGES):
        label = stable_reference_label(index)
        if label not in used_labels:
            used_labels.add(label)
            return label
    raise http_error(
        "agent_session_reference_limit_reached",
        "Agent session reference image limit reached",
        422,
        max_session_images=AGENT_MAX_SESSION_IMAGES,
    )


def _append_session_reference(
    references: list[_SessionReference],
    index_by_image: dict[str, int],
    used_labels: set[str],
    *,
    image_id: str,
    role: str,
    label: str | None,
    source: str,
    preferred_reference_label: str | None = None,
) -> None:
    if image_id in index_by_image:
        return
    reference_label = (
        preferred_reference_label
        if preferred_reference_label and preferred_reference_label not in used_labels
        else _next_reference_label(used_labels)
    )
    used_labels.add(reference_label)
    index_by_image[image_id] = len(references)
    references.append(
        _SessionReference(
            image_id=image_id,
            reference_label=reference_label,
            role=role,
            label=label,
            source=source,
        )
    )


async def _session_references(
    db: AsyncSession,
    *,
    session: AgentSession,
    user: User,
    body: AgentMessageCreateIn,
) -> list[_SessionReference]:
    references: list[_SessionReference] = []
    index_by_image: dict[str, int] = {}
    used_labels: set[str] = set()

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
        .order_by(
            AgentRun.created_at.asc(),
            AgentRun.id.asc(),
            AgentRunReference.ordinal.asc(),
        )
    )
    historical_visible = await retention_filter(db, user, Image.created_at)
    if historical_visible is not None:
        historical_statement = historical_statement.where(historical_visible)
    historical_rows = list((await db.execute(historical_statement)).scalars().all())

    prior_assistant_ids = select(AgentRun.assistant_message_id).where(
        AgentRun.agent_session_id == session.id,
        AgentRun.user_id == user.id,
    )
    generated_statement = (
        select(Image)
        .join(Generation, Generation.id == Image.owner_generation_id)
        .where(
            Generation.user_id == user.id,
            Generation.message_id.in_(prior_assistant_ids),
            Generation.status == "succeeded",
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        .order_by(Generation.created_at.asc(), Generation.id.asc(), Image.id.asc())
    )
    generated_visible = await retention_filter(db, user, Image.created_at)
    if generated_visible is not None:
        generated_statement = generated_statement.where(generated_visible)
    generated_rows = list((await db.execute(generated_statement)).scalars().all())

    for reference in historical_rows:
        _append_session_reference(
            references,
            index_by_image,
            used_labels,
            image_id=reference.image_id,
            role=reference.role,
            label=reference.display_label,
            source="history",
            preferred_reference_label=reference.reference_label,
        )
    for image in generated_rows:
        _append_session_reference(
            references,
            index_by_image,
            used_labels,
            image_id=image.id,
            role="reference",
            label="Agent result",
            source="generated",
        )
    for attachment in body.attachments:
        existing_index = index_by_image.get(attachment.image_id)
        if existing_index is None:
            _append_session_reference(
                references,
                index_by_image,
                used_labels,
                image_id=attachment.image_id,
                role=attachment.role,
                label=attachment.label,
                source="current",
            )
            continue
        references[existing_index] = replace(
            references[existing_index],
            role=attachment.role,
            label=attachment.label,
            source="current",
        )
    return references


async def _validate_submission_slot(
    db: AsyncSession,
    *,
    session: AgentSession,
    user: User,
    body: AgentMessageCreateIn,
) -> list[_SessionReference]:
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
    await _validate_reference_images(
        db,
        user=user,
        image_ids=[attachment.image_id for attachment in body.attachments],
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
    return references


async def _resolve_execution_pin(
    db: AsyncSession,
    *,
    user_id: str,
    user: User,
    account_mode: str,
    conversation: Conversation,
    body: AgentMessageCreateIn,
    reference_count: int,
) -> _ExecutionPin:
    credential: TaskCredentialPin | None = None
    provider_names: tuple[str, ...] = ()
    references_required = reference_count > 0
    reasoning_requested = (body.reasoning_effort or "max") != "none"
    system_prompt = await resolve_system_prompt_for_message(
        db,
        user_id=user_id,
        default_system_prompt_id=user.default_system_prompt_id,
        conv=conversation,
        explicit_prompt=None,
    )
    configured_output_tokens = await agent_setting_int(
        db,
        "agent.max_output_tokens",
    )
    minimum_context_window = (
        estimate_text_tokens(system_prompt or "")
        + estimate_text_tokens(body.text)
        + reference_count * _REFERENCE_CONTEXT_TOKENS
        + configured_output_tokens
        + _AGENT_CONTEXT_SAFETY_TOKENS
    )
    if account_mode == "byok":
        credential = await resolve_task_credential_pin(
            db, user_id, "chat", account_mode
        )
        if references_required and not byok_vision_supported(
            credential.capabilities_jsonb
        ):
            raise http_error(
                "agent_vision_model_unavailable",
                "the active API key has no verified image input capability",
                412,
            )
        model = credential.default_chat_model
        context_window = _capability_int(
            credential.capabilities_jsonb,
            "agent_context_window",
            default_agent_context_window(model),
            2_000_000,
        )
        max_output_tokens = _capability_int(
            credential.capabilities_jsonb,
            "agent_max_output_tokens",
            16_384,
            128_000,
        )
        reasoning_supported = (
            not isinstance(credential.capabilities_jsonb, dict)
            or credential.capabilities_jsonb.get("agent_reasoning_supported")
            is not False
        )
        if reasoning_requested and not reasoning_supported:
            raise http_error(
                "agent_reasoning_model_unavailable",
                "the active API key has no verified reasoning capability",
                412,
            )
        if context_window < minimum_context_window:
            raise http_error(
                "agent_context_window_exceeded",
                "the active API key cannot carry the complete session context",
                412,
                minimum_context_window=minimum_context_window,
            )
    else:
        provider = await wallet_chat_provider_preflight(
            db,
            require_vision=references_required,
            require_reasoning=reasoning_requested,
            minimum_context_window=minimum_context_window,
        )
        model = provider.model
        provider_names = provider.eligible_provider_names
        context_window = provider.context_window
        max_output_tokens = provider.max_output_tokens
        reasoning_supported = provider.reasoning_supported
    return _ExecutionPin(
        model,
        provider_names,
        credential,
        system_prompt,
        context_window,
        max_output_tokens,
        reasoning_supported,
    )


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
    references: list[_SessionReference],
) -> _StagedSubmission:
    now = datetime.now(timezone.utc)
    references_by_image = {
        reference.image_id: reference for reference in references
    }
    reference_content = [
        {
            "image_id": attachment.image_id,
            "role": attachment.role,
            "label": attachment.label,
            "reference_label": references_by_image[
                attachment.image_id
            ].reference_label,
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
        role=Role.USER.value,
        content={
            "text": body.text,
            "source": "agent",
            "attachments": reference_content,
        },
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
        parent_message_id=user_message.id,
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
        status=AgentRunStatus.QUEUED.value,
        execution_epoch=0,
        attempt=0,
        last_event_seq=0,
        idempotency_key=body.idempotency_key,
        request_fingerprint=request_fingerprint,
        request_snapshot_jsonb={},
        account_mode_snapshot=account_mode,
        system_prompt_snapshot=pin.system_prompt,
        model=pin.model,
        reasoning_effort=body.reasoning_effort or "max",
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
    setting_keys = (
        "agent.max_turns",
        "agent.max_tool_calls",
        "agent.max_image_tool_calls",
        "agent.max_images_per_run",
        "agent.max_reference_images",
        "agent.max_session_images",
        "agent.max_output_tokens",
        "agent.run_timeout_seconds",
        "agent.tool_timeout_seconds",
        "agent.capability_ttl_seconds",
    )
    run.request_snapshot_jsonb = {
        "image_defaults": body.image_defaults.model_dump(mode="json"),
        "allow_image": body.allow_image,
        "allowed_tools": [AGENT_TOOL_CREATE_IMAGE] if body.allow_image else [],
        "references": [
            {
                "reference_label": item["reference_label"],
                "role": item["role"],
                "display_label": item["label"],
            }
            for item in run_reference_content
        ],
        "limits": {
            key.removeprefix("agent."): await agent_setting_int(db, key)
            for key in setting_keys
        },
        "eligible_provider_names": list(pin.provider_names),
        "credential_capabilities": (
            dict(credential.capabilities_jsonb or {}) if credential else {}
        ),
    }
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
            "session_reference_count": len(references),
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
    references = await _validate_submission_slot(
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
        reference_count=len(references),
    )
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
        references=references,
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


__all__ = ["submit_agent_message"]
