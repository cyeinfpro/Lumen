"""Provider, checkpoint, and context admission planning for Agent submissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_image_tokens import (
    agent_preview_dimensions,
    estimate_agent_image_tokens,
)
from lumen_core.agent_history import (
    AGENT_PI_CONTEXT_OVERHEAD_TOKENS,
    agent_tool_history_result_text,
    estimate_agent_runtime_history_tokens,
    plan_agent_runtime_context,
)
from lumen_core.agent_history_selection import (
    AGENT_HISTORY_MAX_ENTRIES,
    AGENT_HISTORY_SCAN_LIMIT,
    select_agent_history_tail,
    semantic_agent_message,
)
from lumen_core.agent_model_profiles import default_agent_context_window
from lumen_core.agent_wire_budget import (
    DEFAULT_AGENT_RUNTIME_MAX_REQUEST_BYTES,
    encoded_json_bytes,
    estimate_agent_runtime_request_bytes,
)
from lumen_core.context_window import estimate_text_tokens
from lumen_core.message_content import public_message_content
from lumen_core.model_entities import (
    AgentRun,
    AgentSession,
    AgentToolCall,
    Conversation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import AgentMessageCreateIn

from ...config import settings
from ..message_submission_prompting import (
    TaskCredentialPin,
    resolve_system_prompt_for_message,
    resolve_task_credential_pin,
)
from .common import (
    byok_vision_supported,
    http_error,
    wallet_chat_provider_preflight,
)


@dataclass(frozen=True, slots=True)
class ContinuationPlan:
    source_run_id: str
    source_user_message_id: str
    system_prompt: str | None


@dataclass(frozen=True, slots=True)
class ExecutionPin:
    model: str
    provider_names: tuple[str, ...]
    credential: TaskCredentialPin | None
    system_prompt: str | None
    context_window: int
    max_output_tokens: int
    reasoning_supported: bool
    context_plan: str = "direct"
    estimated_input_tokens: int = 0
    history_truncated: bool = False
    history_first_retained_message_id: str | None = None
    history_removed_entries: int = 0
    history_removed_tokens: int = 0
    estimated_runtime_request_bytes: int = 0
    continuation: ContinuationPlan | None = None


@dataclass(frozen=True, slots=True)
class SubmissionReference:
    image_id: str
    reference_label: str
    role: str
    label: str | None
    source: str


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


async def _history_tool_tokens_by_message(
    db: AsyncSession,
    history_rows: list[Message],
) -> dict[str, int]:
    assistant_ids = [row.id for row in history_rows if row.role == "assistant"]
    if not assistant_ids:
        return {}
    runs = list(
        (
            await db.execute(
                select(AgentRun).where(AgentRun.assistant_message_id.in_(assistant_ids))
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return {}
    tools = list(
        (
            await db.execute(
                select(AgentToolCall)
                .where(AgentToolCall.agent_run_id.in_([run.id for run in runs]))
                .order_by(AgentToolCall.agent_run_id, AgentToolCall.ordinal)
            )
        )
        .scalars()
        .all()
    )
    tools_by_run: dict[str, list[AgentToolCall]] = {}
    for tool in tools:
        tools_by_run.setdefault(tool.agent_run_id, []).append(tool)
    token_counts: dict[str, int] = {}
    for run in runs:
        projected = tools_by_run.get(run.id, [])[:8]
        if not projected:
            continue
        arguments = [
            dict(list(tool.arguments_jsonb.items())[:32])
            if isinstance(tool.arguments_jsonb, dict)
            else {}
            for tool in projected
        ]
        results = [
            agent_tool_history_result_text(
                status=tool.status,
                mode=tool.mode,
                generation_ids=(
                    tool.result_jsonb.get("generation_ids", [])
                    if isinstance(tool.result_jsonb, dict)
                    else []
                ),
                error_code=tool.error_code,
                name=tool.name,
                result_text=(
                    tool.result_jsonb.get("history_text")
                    if isinstance(tool.result_jsonb, dict)
                    and isinstance(tool.result_jsonb.get("history_text"), str)
                    else None
                ),
            )
            for tool in projected
        ]
        token_counts[run.assistant_message_id] = estimate_agent_runtime_history_tokens(
            text="Agent image tool request",
            tool_arguments=arguments,
            tool_result_texts=results,
        )
    return token_counts


async def _history_tool_tokens(
    db: AsyncSession,
    history_rows: list[Message],
) -> int:
    return sum((await _history_tool_tokens_by_message(db, history_rows)).values())


@dataclass(frozen=True, slots=True)
class _HistoryContextEstimate:
    total_tokens: int
    largest_entry_tokens: int


async def _history_context_estimate(
    db: AsyncSession,
    history_rows: list[Message],
    *,
    checkpoint_tokens: int,
) -> _HistoryContextEstimate:
    tokens_by_message: dict[str, int] = {}
    images_by_message: dict[str, list[str]] = {}
    for message in history_rows:
        content = public_message_content(
            message.content if isinstance(message.content, dict) else {}
        )
        tokens_by_message[message.id] = estimate_text_tokens(
            str(content.get("text") or "")
        )
        attachments = content.get("attachments")
        if isinstance(attachments, list):
            images_by_message[message.id] = [
                image_id
                for attachment in attachments
                if isinstance(attachment, dict)
                and isinstance((image_id := attachment.get("image_id")), str)
            ]
    for message_id, tokens in (
        await _history_tool_tokens_by_message(db, history_rows)
    ).items():
        tokens_by_message[message_id] = tokens_by_message.get(message_id, 0) + tokens
    history_image_ids = {
        image_id for image_ids in images_by_message.values() for image_id in image_ids
    }
    if history_image_ids:
        history_images = list(
            (await db.execute(select(Image).where(Image.id.in_(history_image_ids))))
            .scalars()
            .all()
        )
        image_tokens = {
            image.id: estimate_agent_image_tokens(
                "unknown", *agent_preview_dimensions(image.width, image.height)
            ).upper
            for image in history_images
        }
        for message_id, image_ids in images_by_message.items():
            tokens_by_message[message_id] = tokens_by_message.get(message_id, 0) + sum(
                image_tokens.get(image_id, 0) for image_id in image_ids
            )
    return _HistoryContextEstimate(
        total_tokens=checkpoint_tokens + sum(tokens_by_message.values()),
        largest_entry_tokens=max([checkpoint_tokens, *tokens_by_message.values()]),
    )


async def _checkpoint_boundary(
    db: AsyncSession,
    *,
    conversation: Conversation,
    user_id: str,
) -> tuple[tuple[datetime, str] | None, int]:
    session = (
        await db.execute(
            select(AgentSession).where(
                AgentSession.conversation_id == conversation.id,
                AgentSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if session is None or not session.active_pi_compaction_run_id:
        return None, 0
    checkpoint_run = await db.get(AgentRun, session.active_pi_compaction_run_id)
    dispatch = (
        checkpoint_run.dispatch_jsonb
        if checkpoint_run is not None
        and isinstance(checkpoint_run.dispatch_jsonb, dict)
        else {}
    )
    checkpoint = dispatch.get("pi_compaction")
    if not isinstance(checkpoint, dict) or checkpoint.get("status") != "ready":
        return None, 0
    schema = checkpoint.get("schema_version")
    safe_placement = schema == 2 or (
        schema == 1
        and checkpoint.get("placement_contract") == "runtime-pre-prompt-only-v1"
    )
    pointer_matches = (
        session.active_pi_compaction_schema_version == schema
        and session.active_pi_compaction_event_seq == checkpoint.get("source_event_seq")
    )
    boundary_id = checkpoint.get("first_kept_message_id")
    if not safe_placement or not pointer_matches or not isinstance(boundary_id, str):
        return None, 0
    boundary = (
        await db.execute(
            select(Message.created_at, Message.id).where(
                Message.id == boundary_id,
                Message.conversation_id == conversation.id,
                Message.deleted_at.is_(None),
            )
        )
    ).first()
    if boundary is None:
        return None, 0
    summary = checkpoint.get("summary")
    summary_tokens = estimate_text_tokens(summary) if isinstance(summary, str) else 0
    return (boundary.created_at, boundary.id), summary_tokens


@dataclass(frozen=True, slots=True)
class _SubmissionContextEstimate:
    fixed_input_tokens: int
    history_tokens: int
    largest_history_entry_tokens: int
    estimated_input_tokens: int
    estimated_runtime_request_bytes: int
    history_truncated: bool
    history_first_retained_message_id: str | None
    history_removed_entries: int
    history_removed_tokens: int


async def _submission_context_estimate(
    db: AsyncSession,
    *,
    user_id: str,
    conversation: Conversation,
    prompt: str,
    system_prompt: str | None,
    references: tuple[SubmissionReference, ...],
    workspace_files: tuple[dict[str, Any], ...] = (),
) -> _SubmissionContextEstimate:
    history_boundary, checkpoint_tokens = await _checkpoint_boundary(
        db, conversation=conversation, user_id=user_id
    )
    history_statement = (
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.deleted_at.is_(None),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(AGENT_HISTORY_SCAN_LIMIT + 1)
    )
    if history_boundary is not None:
        history_statement = history_statement.where(
            or_(
                Message.created_at > history_boundary[0],
                and_(
                    Message.created_at == history_boundary[0],
                    Message.id >= history_boundary[1],
                ),
            )
        )
    history_desc = list((await db.execute(history_statement)).scalars().all())
    scan_truncated = len(history_desc) > AGENT_HISTORY_SCAN_LIMIT
    history_candidates = list(reversed(history_desc[:AGENT_HISTORY_SCAN_LIMIT]))
    selection = select_agent_history_tail(
        history_candidates,
        item_id=lambda item: item.id,
        role=lambda item: item.role,
        semantic=lambda item: semantic_agent_message(
            role=item.role,
            content=item.content,
            status=item.status,
        ),
        token_estimate=lambda item: estimate_text_tokens(
            str(item.content.get("text") or "")
            if isinstance(item.content, dict)
            else ""
        ),
        max_entries=AGENT_HISTORY_MAX_ENTRIES,
    )
    history_rows = list(selection.items)
    history_estimate = await _history_context_estimate(
        db,
        history_rows,
        checkpoint_tokens=checkpoint_tokens,
    )
    image_rows = list(
        (
            await db.execute(
                select(Image).where(
                    Image.id.in_([reference.image_id for reference in references])
                )
            )
        )
        .scalars()
        .all()
    )
    reference_tokens = sum(
        estimate_agent_image_tokens(
            "unknown", *agent_preview_dimensions(image.width, image.height)
        ).upper
        for image in image_rows
    )
    fixed_input_tokens = (
        estimate_text_tokens(system_prompt or "")
        + estimate_text_tokens(prompt)
        + reference_tokens
        + AGENT_PI_CONTEXT_OVERHEAD_TOKENS
    )
    history_contents = [
        public_message_content(item.content if isinstance(item.content, dict) else {})
        for item in history_rows
    ]
    historical_reference_count = sum(
        len(content.get("attachments", []))
        for content in history_contents
        if isinstance(content.get("attachments"), list)
    )
    wire_budget = estimate_agent_runtime_request_bytes(
        system_prompt=system_prompt or "",
        current_prompt=prompt,
        history_texts=(str(content.get("text") or "") for content in history_contents),
        history_structured_bytes=sum(
            encoded_json_bytes(content) for content in history_contents
        ),
        current_reference_count=len(references),
        historical_reference_count=historical_reference_count,
        workspace_files_bytes=sum(encoded_json_bytes(item) for item in workspace_files),
        maximum_bytes=int(
            getattr(
                settings,
                "agent_runtime_max_request_bytes",
                DEFAULT_AGENT_RUNTIME_MAX_REQUEST_BYTES,
            )
        ),
    )
    if not wire_budget.admitted:
        raise http_error(
            "agent_runtime_request_too_large",
            "Agent request exceeds the Runtime transport limit",
            413,
            estimated_bytes=wire_budget.estimated_bytes,
            maximum_bytes=wire_budget.maximum_bytes,
        )
    return _SubmissionContextEstimate(
        fixed_input_tokens=fixed_input_tokens,
        history_tokens=history_estimate.total_tokens,
        largest_history_entry_tokens=history_estimate.largest_entry_tokens,
        estimated_input_tokens=fixed_input_tokens + history_estimate.total_tokens,
        estimated_runtime_request_bytes=wire_budget.estimated_bytes,
        history_truncated=scan_truncated or selection.truncated,
        history_first_retained_message_id=selection.first_retained_id,
        history_removed_entries=selection.removed_entries + int(scan_truncated),
        history_removed_tokens=selection.removed_tokens,
    )


async def resolve_execution_pin(
    db: AsyncSession,
    *,
    user_id: str,
    user: User,
    account_mode: str,
    conversation: Conversation,
    body: AgentMessageCreateIn,
    references: tuple[SubmissionReference, ...],
) -> ExecutionPin:
    credential: TaskCredentialPin | None = None
    provider_names: tuple[str, ...] = ()
    references_required = bool(references)
    reasoning_requested = body.reasoning_effort not in {None, "none"}
    system_prompt = await resolve_system_prompt_for_message(
        db,
        user_id=user_id,
        default_system_prompt_id=user.default_system_prompt_id,
        conv=conversation,
        explicit_prompt=None,
    )
    context = await _submission_context_estimate(
        db,
        user_id=user_id,
        conversation=conversation,
        prompt=body.text,
        system_prompt=system_prompt,
        references=references,
        workspace_files=tuple(item.model_dump(mode="json") for item in body.files),
    )
    context_plan = "direct"
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
        if body.model and body.model != credential.default_chat_model:
            raise http_error(
                "agent_model_unavailable",
                "requested Agent model is unavailable for the active API key",
                412,
            )
        model = body.model or credential.default_chat_model
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
        plan = plan_agent_runtime_context(
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            fixed_input_tokens=context.fixed_input_tokens,
            history_tokens=context.history_tokens,
            largest_history_entry_tokens=context.largest_history_entry_tokens,
        )
        context_plan = plan.mode
        if plan.mode == "impossible":
            raise http_error(
                "agent_context_window_exceeded",
                "the active API key cannot compact and carry the session context",
                412,
                minimum_context_window=(
                    context.estimated_input_tokens + max_output_tokens
                ),
                direct_input_limit=plan.direct_input_limit,
                compaction_source_limit=plan.compaction_source_limit,
            )
    else:
        provider = await wallet_chat_provider_preflight(
            db,
            require_vision=references_required,
            require_reasoning=reasoning_requested,
            fixed_input_tokens=context.fixed_input_tokens,
            history_context_tokens=context.history_tokens,
            largest_history_entry_tokens=context.largest_history_entry_tokens,
            requested_model=body.model,
        )
        model = provider.model
        provider_names = provider.eligible_provider_names
        context_window = provider.context_window
        max_output_tokens = provider.max_output_tokens
        reasoning_supported = provider.reasoning_supported
        context_plan = provider.context_plan
    return ExecutionPin(
        model,
        provider_names,
        credential,
        system_prompt,
        context_window,
        max_output_tokens,
        reasoning_supported,
        context_plan,
        context.estimated_input_tokens,
        context.history_truncated,
        context.history_first_retained_message_id,
        context.history_removed_entries,
        context.history_removed_tokens,
        context.estimated_runtime_request_bytes,
    )


__all__ = [
    "ContinuationPlan",
    "ExecutionPin",
    "SubmissionReference",
    "resolve_execution_pin",
]
