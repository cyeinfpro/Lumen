"""Run-scoped Agent image tool gateway with durable replay receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NoReturn

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_capability import AgentCapabilityClaims
from lumen_core.agent_dispatch import mark_provider_dispatch_authorized
from lumen_core.agent_events import (
    AGENT_TOOL_CREATE_IMAGE,
    EV_AGENT_TOOL_FAILED,
    EV_AGENT_TOOL_STARTED,
    EV_AGENT_TOOL_SUCCEEDED,
    AgentToolCallStatus,
)
from lumen_core.constants import Intent
from lumen_core.model_entities import (
    AgentCapabilityGrant,
    AgentRun,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    Conversation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import (
    AgentImageDefaultsIn,
    AgentProviderDispatchIn,
    AgentProviderDispatchOut,
    AgentCreateImageNormalized,
    AgentToolCreateImageIn,
    AgentToolCreateImageOut,
    agent_tool_semantic_key,
    normalize_create_image_arguments,
)

from ...audit import write_audit
from ...redis_client import get_redis
from ..active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)
from ..message_submission import (
    create_generation_batch_for_message,
    publish_assistant_task,
)
from ..message_generation_batch import (
    ExistingMessageGenerationCommand,
)
from ..message_submission_prompting import resolve_task_credential_pin
from .common import (
    agent_setting_int,
    http_error,
    publish_agent_events_best_effort,
    stage_agent_event,
    wallet_image_provider_preflight,
)
from .presentation import agent_tool_call_out
from .repository import retention_filter
from .session_images import session_image_slot_count


def _error_code(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"][:64]
    return "agent_tool_preflight_failed"


def _snapshot_dict(run: AgentRun, key: str) -> dict[str, Any]:
    snapshot = (
        run.request_snapshot_jsonb
        if isinstance(run.request_snapshot_jsonb, dict)
        else {}
    )
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


def _snapshot_list(run: AgentRun, key: str) -> list[Any]:
    snapshot = (
        run.request_snapshot_jsonb
        if isinstance(run.request_snapshot_jsonb, dict)
        else {}
    )
    value = snapshot.get(key)
    return value if isinstance(value, list) else []


def _snapshot_limit(
    limits: dict[str, Any],
    key: str,
    fallback: int,
) -> int:
    value = limits.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _tool_replay_out(tool_call: AgentToolCall) -> AgentToolCreateImageOut:
    result = tool_call.result_jsonb if isinstance(tool_call.result_jsonb, dict) else {}
    generation_ids = result.get("generation_ids")
    safe_ids = (
        [value for value in generation_ids if isinstance(value, str)][:4]
        if isinstance(generation_ids, list)
        else []
    )
    if tool_call.status != AgentToolCallStatus.SUCCEEDED.value or not safe_ids:
        raise http_error(
            "agent_tool_receipt_incomplete",
            "Agent tool success receipt is incomplete",
            409,
        )
    mode = (
        tool_call.mode
        if tool_call.mode in {"text_to_image", "image_to_image"}
        else "image_to_image"
        if bool(tool_call.arguments_jsonb.get("reference_labels"))
        else "text_to_image"
    )
    accepted = AgentCreateImageNormalized.model_validate(tool_call.arguments_jsonb)
    return AgentToolCreateImageOut(
        tool_call=agent_tool_call_out(tool_call),
        generation_ids=safe_ids,
        mode=mode,
        accepted=accepted,
        replayed=True,
        pi_tool_call_id=tool_call.pi_tool_call_id,
        ordinal=tool_call.ordinal,
        request_hash=tool_call.request_hash,
    )


def _tool_replay_failure(tool_call: AgentToolCall) -> HTTPException:
    if tool_call.status in {
        AgentToolCallStatus.QUEUED.value,
        AgentToolCallStatus.RUNNING.value,
    }:
        return http_error(
            "agent_tool_in_progress",
            "Agent tool call is still in progress",
            409,
            agent_tool_call_id=tool_call.id,
        )
    receipt = tool_call.result_jsonb if isinstance(tool_call.result_jsonb, dict) else {}
    code = receipt.get("error_code") or tool_call.error_code
    status = receipt.get("http_status")
    if not isinstance(status, int) or isinstance(status, bool):
        status = 504 if tool_call.status == AgentToolCallStatus.TIMED_OUT.value else 409
    if not isinstance(code, str) or not code:
        code = {
            AgentToolCallStatus.CANCELLED.value: "agent_tool_cancelled",
            AgentToolCallStatus.TIMED_OUT.value: "agent_tool_timed_out",
        }.get(tool_call.status, "agent_tool_failed")
    return http_error(code[:64], "Agent tool call previously failed", status)


async def lock_agent_capability_run(
    db: AsyncSession,
    *,
    run_id: str,
    claims: AgentCapabilityClaims,
) -> tuple[AgentRun, AgentCapabilityGrant]:
    if claims.run_id != run_id:
        raise http_error(
            "agent_capability_scope_mismatch", "capability scope mismatch", 403
        )
    try:
        await lock_active_user(db, claims.user_id)
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    run = (
        await db.execute(
            select(AgentRun)
            .join(AgentSession, AgentSession.id == AgentRun.agent_session_id)
            .join(Conversation, Conversation.id == AgentSession.conversation_id)
            .where(
                AgentRun.id == run_id,
                AgentRun.user_id == claims.user_id,
                AgentSession.id == claims.agent_session_id,
                AgentSession.user_id == claims.user_id,
                Conversation.user_id == claims.user_id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update(of=AgentRun)
        )
    ).scalar_one_or_none()
    if run is None:
        raise http_error(
            "agent_capability_scope_mismatch", "capability scope mismatch", 403
        )
    if run.execution_epoch != claims.execution_epoch:
        raise http_error(
            "agent_stale_execution_epoch", "stale Agent execution epoch", 409
        )
    if run.status != "running" or run.cancel_requested_at is not None:
        raise http_error("agent_run_not_active", "Agent run is not active", 409)
    grant = (
        await db.execute(
            select(AgentCapabilityGrant)
            .where(AgentCapabilityGrant.capability_id == claims.capability_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    grant_expires_at = grant.expires_at if grant is not None else None
    if grant_expires_at is not None and grant_expires_at.tzinfo is None:
        grant_expires_at = grant_expires_at.replace(tzinfo=timezone.utc)
    if (
        grant is None
        or grant.nonce != claims.nonce
        or grant.agent_run_id != run.id
        or grant.user_id != run.user_id
        or grant.agent_session_id != run.agent_session_id
        or grant.execution_epoch != run.execution_epoch
        or grant_expires_at is None
        or grant_expires_at <= now
    ):
        raise http_error(
            "agent_capability_redeemed",
            "Agent capability is unavailable",
            401,
        )
    return run, grant


async def _reference_map(
    db: AsyncSession,
    *,
    run: AgentRun,
    claims: AgentCapabilityClaims,
    requested_labels: list[str],
) -> dict[str, AgentRunReference]:
    if not set(requested_labels).issubset(set(claims.allowed_reference_labels)):
        raise http_error(
            "agent_reference_not_allowed",
            "tool requested a reference outside its capability",
            403,
        )
    references = list(
        (
            await db.execute(
                select(AgentRunReference)
                .where(AgentRunReference.agent_run_id == run.id)
                .order_by(AgentRunReference.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    by_label = {reference.reference_label: reference for reference in references}
    if any(label not in by_label for label in requested_labels):
        raise http_error("agent_reference_not_found", "Agent reference not found", 400)
    selected = [by_label[label] for label in requested_labels]
    if selected:
        user = await db.get(User, run.user_id)
        if user is None:
            raise http_error(
                "agent_reference_not_found",
                "Agent reference is no longer available",
                400,
            )
        visible = await retention_filter(db, user, Image.created_at)
        owned_statement = select(Image.id).where(
            Image.id.in_([reference.image_id for reference in selected]),
            Image.user_id == run.user_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == "ready",
        )
        if visible is not None:
            owned_statement = owned_statement.where(visible)
        owned_ids = set((await db.execute(owned_statement)).scalars().all())
        if owned_ids != {reference.image_id for reference in selected}:
            raise http_error(
                "agent_reference_not_found",
                "Agent reference is no longer available",
                400,
            )
    return by_label


@dataclass(frozen=True, slots=True)
class _PreparedTool:
    run: AgentRun
    tool_call: AgentToolCall
    normalized: AgentCreateImageNormalized
    semantic_key: str
    mode: str
    events: list[dict[str, Any]]


async def _prepare_create_image_tool(
    db: AsyncSession,
    *,
    run_id: str,
    claims: AgentCapabilityClaims,
    body: AgentToolCreateImageIn,
) -> _PreparedTool | AgentToolCreateImageOut:
    if AGENT_TOOL_CREATE_IMAGE not in claims.allowed_tools:
        raise http_error("agent_tool_not_allowed", "Agent tool is not allowed", 403)
    if body.execution_epoch != claims.execution_epoch:
        raise http_error(
            "agent_stale_execution_epoch", "stale Agent execution epoch", 409
        )
    run, grant = await lock_agent_capability_run(db, run_id=run_id, claims=claims)
    if AGENT_TOOL_CREATE_IMAGE not in _snapshot_list(run, "allowed_tools"):
        raise http_error("agent_tool_not_allowed", "image generation is disabled", 403)
    defaults = AgentImageDefaultsIn.model_validate(
        _snapshot_dict(run, "image_defaults")
    )
    normalized = normalize_create_image_arguments(body.arguments, defaults)
    request_hash, semantic_key = agent_tool_semantic_key(
        run.id, body.ordinal, normalized
    )
    existing_rows = list(
        (
            await db.execute(
                select(AgentToolCall).where(
                    AgentToolCall.agent_run_id == run.id,
                    or_(
                        AgentToolCall.ordinal == body.ordinal,
                        AgentToolCall.pi_tool_call_id == body.pi_tool_call_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if existing_rows:
        existing = existing_rows[0]
        exact_replay = (
            len(existing_rows) == 1
            and existing.ordinal == body.ordinal
            and existing.pi_tool_call_id == body.pi_tool_call_id
            and existing.name == AGENT_TOOL_CREATE_IMAGE
            and existing.request_hash == request_hash
            and existing.semantic_key == semantic_key
        )
        if exact_replay:
            if existing.status == AgentToolCallStatus.SUCCEEDED.value:
                replay = _tool_replay_out(existing)
                await db.rollback()
                return replay
            replay_failure = _tool_replay_failure(existing)
            await db.rollback()
            raise replay_failure
        raise http_error(
            "agent_tool_ordinal_conflict",
            "tool ordinal was used with different arguments",
            409,
        )
    await _reference_map(
        db,
        run=run,
        claims=claims,
        requested_labels=normalized.reference_labels,
    )
    await _enforce_tool_limits(db, run=run, requested_count=normalized.count)
    if grant.redeemed_count >= grant.max_redemptions:
        raise http_error(
            "agent_tool_limit_reached",
            "Agent capability redemption limit reached",
            409,
        )
    grant.redeemed_count += 1
    mode = "image_to_image" if normalized.reference_labels else "text_to_image"
    tool_call = AgentToolCall(
        agent_run_id=run.id,
        capability_id=claims.capability_id,
        pi_tool_call_id=body.pi_tool_call_id,
        ordinal=body.ordinal,
        execution_epoch=body.execution_epoch,
        name=AGENT_TOOL_CREATE_IMAGE,
        mode=mode,
        status=AgentToolCallStatus.RUNNING.value,
        request_hash=request_hash,
        semantic_key=semantic_key,
        arguments_jsonb=normalized.model_dump(mode="json"),
        result_jsonb={},
        generation_count=0,
        started_at=datetime.now(timezone.utc),
    )
    db.add(tool_call)
    run.tool_call_count += 1
    await db.flush()
    events = [
        stage_agent_event(
            db,
            run=run,
            event_name=EV_AGENT_TOOL_STARTED,
            tool_call_id=tool_call.id,
        )
    ]
    return _PreparedTool(run, tool_call, normalized, semantic_key, mode, events)


async def _enforce_tool_limits(
    db: AsyncSession,
    *,
    run: AgentRun,
    requested_count: int,
) -> None:
    limits = _snapshot_dict(run, "tool_policy") or _snapshot_dict(run, "limits")
    reference_policy = _snapshot_dict(run, "reference_policy") or _snapshot_dict(
        run, "limits"
    )
    max_image_tool_calls = _snapshot_limit(
        limits,
        "max_image_tool_calls",
        await agent_setting_int(db, "agent.max_image_tool_calls"),
    )
    max_images_per_run = _snapshot_limit(
        limits,
        "max_images_per_run",
        await agent_setting_int(db, "agent.max_images_per_run"),
    )
    max_session_references = _snapshot_limit(
        reference_policy,
        "max_session_images",
        await agent_setting_int(db, "agent.max_session_images"),
    )
    image_tool_calls = int(
        (
            await db.execute(
                select(func.count(AgentToolCall.id)).where(
                    AgentToolCall.agent_run_id == run.id,
                    AgentToolCall.name == AGENT_TOOL_CREATE_IMAGE,
                )
            )
        ).scalar_one()
        or 0
    )
    reference_image_ids = set(
        (
            await db.execute(
                select(AgentRunReference.image_id).where(
                    AgentRunReference.agent_run_id == run.id
                )
            )
        ).scalars()
    )
    user = await db.get(User, run.user_id)
    if user is None:
        raise http_error("agent_snapshot_incomplete", "Agent user is missing", 409)
    visible = await retention_filter(db, user, Image.created_at)
    session_image_slots = await session_image_slot_count(
        db,
        session_id=run.agent_session_id,
        user_id=run.user_id,
        snapshotted_image_ids=reference_image_ids,
        image_visibility_filter=visible,
    )
    existing_images = int(
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(AgentToolCall.generation_count), 0)
                ).where(AgentToolCall.agent_run_id == run.id)
            )
        ).scalar_one()
        or 0
    )
    if image_tool_calls >= max_image_tool_calls:
        raise http_error(
            "agent_tool_limit_reached", "Agent tool-call limit reached", 409
        )
    if existing_images + requested_count > max_images_per_run:
        raise http_error("agent_image_limit_reached", "Agent image limit reached", 409)
    if session_image_slots + requested_count > max_session_references:
        raise http_error(
            "agent_session_reference_limit_reached",
            "Agent session reference image limit reached",
            409,
            max_session_images=max_session_references,
        )


async def _create_tool_generation(
    db: AsyncSession,
    *,
    prepared: _PreparedTool,
    claims: AgentCapabilityClaims,
) -> Any:
    run = prepared.run
    normalized = prepared.normalized
    async with db.begin_nested():
        references = await _reference_map(
            db,
            run=run,
            claims=claims,
            requested_labels=normalized.reference_labels,
        )
        attachment_ids = [
            references[label].image_id for label in normalized.reference_labels
        ]
        credential_pin = None
        if run.account_mode_snapshot == "byok":
            credential_pin = await resolve_task_credential_pin(
                db, run.user_id, "image", run.account_mode_snapshot
            )
        else:
            await wallet_image_provider_preflight(db)
        assistant_message = (
            await db.execute(
                select(Message)
                .where(
                    Message.id == run.assistant_message_id,
                    Message.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if assistant_message is None:
            raise http_error(
                "agent_snapshot_incomplete", "Agent assistant message is missing", 409
            )
        return await create_generation_batch_for_message(
            ExistingMessageGenerationCommand(
                db=db,
                user_id=run.user_id,
                account_mode=run.account_mode_snapshot,
                assistant_msg=assistant_message,
                intent=(
                    Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE
                ),
                idempotency_key=prepared.semantic_key,
                image_params=normalized.image_params(),
                attachment_ids=attachment_ids,
                text=normalized.prompt,
                credential_pin=credential_pin,
                credential_pin_resolved=True,
                request_metadata={
                    "source": "agent",
                    "action_source": "agent.create_image",
                    "agent_session_id": run.agent_session_id,
                    "agent_run_id": run.id,
                    "agent_tool_call_id": prepared.tool_call.id,
                    "trace_id": (
                        run.dispatch_jsonb.get("trace_id")
                        if isinstance(run.dispatch_jsonb, dict)
                        and isinstance(run.dispatch_jsonb.get("trace_id"), str)
                        else None
                    ),
                    "reference_labels": list(normalized.reference_labels),
                    "attachment_roles": [
                        {
                            "image_id": references[label].image_id,
                            "role": references[label].role,
                            "label": references[label].display_label,
                            "reference_label": label,
                        }
                        for label in normalized.reference_labels
                    ],
                },
            )
        )


async def _commit_tool_failure(
    db: AsyncSession,
    *,
    prepared: _PreparedTool,
    failure: HTTPException,
) -> NoReturn:
    run, tool_call = prepared.run, prepared.tool_call
    tool_call.status = AgentToolCallStatus.FAILED.value
    tool_call.error_code = _error_code(failure)
    tool_call.result_jsonb = {
        "receipt_version": 1,
        "status": AgentToolCallStatus.FAILED.value,
        "http_status": failure.status_code,
        "error_code": tool_call.error_code,
    }
    tool_call.error_message = None
    tool_call.finished_at = datetime.now(timezone.utc)
    prepared.events.append(
        stage_agent_event(
            db,
            run=run,
            event_name=EV_AGENT_TOOL_FAILED,
            tool_call_id=tool_call.id,
        )
    )
    await write_audit(
        db,
        event_type="agent.tool.failed",
        user_id=run.user_id,
        details={
            "agent_run_id": run.id,
            "agent_tool_call_id": tool_call.id,
            "ordinal": tool_call.ordinal,
            "error_code": tool_call.error_code,
        },
        autocommit=False,
    )
    await db.commit()
    await publish_agent_events_best_effort(
        user_id=run.user_id,
        agent_session_id=run.agent_session_id,
        events=prepared.events,
    )
    raise failure


async def _commit_tool_success(
    db: AsyncSession,
    *,
    prepared: _PreparedTool,
    result: Any,
) -> AgentToolCreateImageOut:
    run, tool_call = prepared.run, prepared.tool_call
    generation_ids = list(result.generation_ids)
    tool_call.status = AgentToolCallStatus.SUCCEEDED.value
    tool_call.result_jsonb = {
        "generation_ids": generation_ids,
        "mode": prepared.mode,
        "accepted": True,
        "accepted_parameters": prepared.normalized.model_dump(mode="json"),
        "pi_tool_call_id": tool_call.pi_tool_call_id,
        "ordinal": tool_call.ordinal,
        "request_hash": tool_call.request_hash,
    }
    tool_call.generation_count = len(generation_ids)
    tool_call.finished_at = datetime.now(timezone.utc)
    assistant_content = (
        dict(result.assistant_msg.content)
        if isinstance(result.assistant_msg.content, dict)
        else {}
    )
    public_tool = {
        "id": tool_call.id,
        "name": AGENT_TOOL_CREATE_IMAGE,
        "label": "Create image",
        "mode": prepared.mode,
        "status": tool_call.status,
        "generation_ids": generation_ids,
        "generation_count": len(generation_ids),
    }
    tool_items = [
        item
        for item in assistant_content.get("tool_calls", [])
        if isinstance(item, dict) and item.get("id") != tool_call.id
    ]
    tool_items.append(public_tool)
    existing_generation_ids = [
        value
        for value in assistant_content.get("generation_ids", [])
        if isinstance(value, str)
    ]
    assistant_content.update(
        {
            "source": "agent",
            "agent_run_id": run.id,
            "tool_calls": tool_items,
            "generation_ids": list(
                dict.fromkeys([*existing_generation_ids, *generation_ids])
            ),
        }
    )
    result.assistant_msg.content = assistant_content
    prepared.events.append(
        stage_agent_event(
            db,
            run=run,
            event_name=EV_AGENT_TOOL_SUCCEEDED,
            tool_call_id=tool_call.id,
            generation_ids=generation_ids,
        )
    )
    await write_audit(
        db,
        event_type="agent.tool.succeeded",
        user_id=run.user_id,
        details={
            "agent_run_id": run.id,
            "agent_tool_call_id": tool_call.id,
            "ordinal": tool_call.ordinal,
            "mode": prepared.mode,
            "generation_count": len(generation_ids),
            "trace_id": (
                run.dispatch_jsonb.get("trace_id")
                if isinstance(run.dispatch_jsonb, dict)
                else None
            ),
        },
        autocommit=False,
    )
    await db.commit()
    await publish_assistant_task(
        db=db,
        redis=get_redis(),
        user_id=run.user_id,
        conv_id=result.assistant_msg.conversation_id,
        assistant_msg_id=result.assistant_msg.id,
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    await publish_agent_events_best_effort(
        user_id=run.user_id,
        agent_session_id=run.agent_session_id,
        events=prepared.events,
    )
    await db.refresh(tool_call)
    return AgentToolCreateImageOut(
        tool_call=agent_tool_call_out(tool_call),
        generation_ids=generation_ids,
        mode=prepared.mode,
        accepted=prepared.normalized,
        replayed=False,
        pi_tool_call_id=tool_call.pi_tool_call_id,
        ordinal=tool_call.ordinal,
        request_hash=tool_call.request_hash,
    )


async def submit_create_image_tool(
    db: AsyncSession,
    *,
    run_id: str,
    claims: AgentCapabilityClaims,
    body: AgentToolCreateImageIn,
) -> AgentToolCreateImageOut:
    prepared = await _prepare_create_image_tool(
        db,
        run_id=run_id,
        claims=claims,
        body=body,
    )
    if isinstance(prepared, AgentToolCreateImageOut):
        return prepared
    try:
        result = await _create_tool_generation(db, prepared=prepared, claims=claims)
    except HTTPException as exc:
        await _commit_tool_failure(db, prepared=prepared, failure=exc)
    return await _commit_tool_success(db, prepared=prepared, result=result)


async def authorize_provider_dispatch(
    db: AsyncSession,
    *,
    run_id: str,
    claims: AgentCapabilityClaims,
    body: AgentProviderDispatchIn,
) -> AgentProviderDispatchOut:
    if claims.allowed_tools or claims.allowed_reference_labels:
        raise http_error(
            "agent_capability_scope_mismatch", "capability scope mismatch", 403
        )
    if body.execution_epoch != claims.execution_epoch:
        raise http_error(
            "agent_stale_execution_epoch", "stale Agent execution epoch", 409
        )
    run, grant = await lock_agent_capability_run(
        db,
        run_id=run_id,
        claims=claims,
    )
    expected_ordinal = grant.redeemed_count + 1
    if body.dispatch_ordinal != expected_ordinal:
        raise http_error(
            "agent_provider_dispatch_conflict",
            "provider dispatch ordinal is unavailable",
            409,
        )
    if grant.redeemed_count >= grant.max_redemptions:
        raise http_error(
            "agent_safety_budget_reached",
            "provider dispatch budget reached",
            409,
        )
    grant.redeemed_count += 1
    dispatch = dict(run.dispatch_jsonb) if isinstance(run.dispatch_jsonb, dict) else {}
    mark_provider_dispatch_authorized(dispatch, body.dispatch_ordinal)
    run.dispatch_jsonb = dispatch
    permit_id = f"{grant.capability_id}:{body.dispatch_ordinal}"
    await db.commit()
    return AgentProviderDispatchOut(
        permit_id=permit_id,
        dispatch_ordinal=body.dispatch_ordinal,
    )


__all__ = [
    "authorize_provider_dispatch",
    "lock_agent_capability_run",
    "submit_create_image_tool",
]
