"""Orchestration for loading and compressing completion conversation context.

The data-access primitives stay in :mod:`context_loading`; this module owns the
policy decisions around summaries, sticky messages, token budgets, and safe
fallbacks.  It intentionally receives a late-bound hooks object so the worker
facade retains its existing monkeypatch surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, cast

from lumen_core.context_window import (
    MESSAGE_OVERHEAD_TOKENS,
    estimate_summary_tokens,
    format_sticky_input_text,
    is_summary_usable,
)
from lumen_core.models import Conversation, Message

from .context import (
    PackedContext,
    _estimated_summary_source,
    _fallback_pack,
    _pack_with_existing_summary,
    _packed_with_input,
)
from .history import (
    _SummaryBoundary,
    _message_created_at,
    _summary_age_seconds,
    _summary_compressed_at,
    _summary_covers_boundary,
    _sticky_text_from_message,
    _instructions_with_summary_guardrail,
)


@dataclass(frozen=True)
class _CompressionSettings:
    input_budget: int
    summary_model: str
    trigger_tokens: int
    target_tokens: int
    min_recent_messages: int
    min_interval_seconds: int
    compression_enabled: bool


@dataclass(frozen=True)
class _RecentSelection:
    first_user: Message | None
    current_user: Message | None
    recent_desc: list[Message]
    summary_rows: list[Message]
    sticky_message: Message | None
    sticky_tokens: int
    used_tokens: int


def _empty_context(system_prompt: str | None) -> PackedContext:
    return PackedContext(
        input_list=[],
        estimated_tokens=0,
        summary_used=False,
        summary_created=False,
        summary_up_to_message_id=None,
        sticky_used=False,
        included_messages_count=0,
        truncated_without_summary=False,
        fallback_reason=None,
        _system_prompt=system_prompt,
    )


async def _with_input(
    session: Any,
    packed: PackedContext,
    hooks: Any,
) -> PackedContext:
    return _packed_with_input(
        await hooks.build_input_from_packed_context(session, packed),
        packed,
    )


async def _existing_summary_context(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    system_prompt: str | None,
    retention_filter: Any | None,
    summary: dict[str, Any],
    settings: _CompressionSettings,
    hooks: Any,
) -> PackedContext | None:
    rows_after_summary = await hooks.load_rows_desc_after_summary(
        session,
        conversation_id=conversation_id,
        target=target,
        summary=summary,
        retention_filter=retention_filter,
    )
    first_user = await hooks.pick_first_user_from_summary(session, summary)
    current_user = await hooks.pick_current_user_with_lookup(
        session,
        rows_after_summary,
        target,
        summary,
    )
    packed = _pack_with_existing_summary(
        system_prompt=system_prompt,
        all_rows_desc=rows_after_summary,
        summary=summary,
        summary_model=settings.summary_model,
        input_budget=settings.input_budget,
        current_user=current_user,
        first_user=first_user,
    )
    packed = _reestimate_existing_summary_tokens(
        packed,
        system_prompt=system_prompt,
        hooks=hooks,
    )
    if (
        not settings.compression_enabled
        or packed.estimated_tokens < settings.trigger_tokens
    ):
        return await _with_input(session, packed, hooks)
    return packed


async def _load_uncompressed_context(
    session: Any,
    *,
    system_prompt: str | None,
    all_rows_desc: list[Message],
    input_budget: int,
    hooks: Any,
) -> PackedContext:
    rows_desc: list[Message] = []
    used_tokens = hooks.estimate_system_prompt_tokens(system_prompt)
    truncated = False
    for message in all_rows_desc:
        estimated = hooks.count_message_tokens(message.role, message.content)
        if estimated <= 0:
            continue
        if used_tokens + estimated > input_budget:
            truncated = True
            break
        rows_desc.append(message)
        used_tokens += estimated
    return _fallback_pack(
        system_prompt=system_prompt,
        rows_desc=rows_desc,
        used_tokens=used_tokens,
        truncated=truncated,
    )


async def _load_circuit_fallback(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    system_prompt: str | None,
    retention_filter: Any | None,
    input_budget: int,
    summary_model: str,
    hooks: Any,
) -> PackedContext:
    rows_desc, used_tokens, truncated = await hooks.load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=input_budget,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
    )
    return _fallback_pack(
        system_prompt=system_prompt,
        rows_desc=rows_desc,
        used_tokens=used_tokens,
        truncated=truncated,
        compression_enabled=True,
        fallback_reason="circuit_open",
        force_include_message=hooks.pick_current_user(rows_desc, target),
        compressor_model=summary_model,
    )


async def _load_budget_fallback(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    system_prompt: str | None,
    retention_filter: Any | None,
    input_budget: int,
    current_user: Message | None,
    summary_model: str,
    reason: str,
    hooks: Any,
) -> PackedContext:
    rows_desc, used_tokens, truncated = await hooks.load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=input_budget,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
    )
    return _fallback_pack(
        system_prompt=system_prompt,
        rows_desc=rows_desc,
        used_tokens=used_tokens,
        truncated=truncated,
        compression_enabled=True,
        fallback_reason=reason,
        force_include_message=current_user,
        compressor_model=summary_model,
    )


async def _resolve_settings(
    *,
    chat_model: str | None,
    hooks: Any,
) -> _CompressionSettings:
    input_budget = (
        hooks.get_input_budget(chat_model) if chat_model else hooks.input_token_budget
    )
    summary_model = await hooks.resolve_summary_model()
    compression_enabled = bool(
        await hooks.resolve_int_setting(
            "context.compression_enabled",
            hooks.compression_enabled_default,
        )
    )
    trigger_percent = await hooks.resolve_int_setting(
        "context.compression_trigger_percent",
        hooks.compression_trigger_percent_default,
    )
    target_tokens = await hooks.resolve_int_setting(
        "context.summary_target_tokens",
        hooks.summary_target_tokens_default,
    )
    min_recent_messages = max(
        1,
        await hooks.resolve_int_setting(
            "context.summary_min_recent_messages",
            hooks.summary_min_recent_messages_default,
        ),
    )
    min_interval_seconds = await hooks.resolve_int_setting(
        "context.summary_min_interval_seconds",
        hooks.summary_min_interval_seconds_default,
    )
    return _CompressionSettings(
        input_budget=input_budget,
        summary_model=summary_model,
        trigger_tokens=input_budget * trigger_percent // 100,
        target_tokens=target_tokens,
        min_recent_messages=min_recent_messages,
        min_interval_seconds=min_interval_seconds,
        compression_enabled=compression_enabled,
    )


def _reestimate_existing_summary_tokens(
    packed: PackedContext,
    *,
    system_prompt: str | None,
    hooks: Any,
) -> PackedContext:
    instructions = _instructions_with_summary_guardrail(system_prompt, enabled=True)
    used_tokens = hooks.estimate_system_prompt_tokens(instructions)
    if packed.sticky_used and packed._sticky_message is not None:
        used_tokens += MESSAGE_OVERHEAD_TOKENS + hooks.count_tokens(
            format_sticky_input_text(_sticky_text_from_message(packed._sticky_message))
        )
    if packed.summary_used:
        used_tokens += MESSAGE_OVERHEAD_TOKENS + packed.summary_tokens
    used_tokens += sum(
        hooks.count_message_tokens(message.role, message.content)
        for message in packed._recent_rows
    )
    return replace(packed, estimated_tokens=used_tokens)


def _forced_recent_messages(
    all_rows_desc: list[Message],
    *,
    min_recent_messages: int,
    current_user: Message | None,
    hooks: Any,
) -> list[Message]:
    forced_recent_desc: list[Message] = []
    for message in all_rows_desc:
        if hooks.count_message_tokens(message.role, message.content) <= 0:
            continue
        if len(forced_recent_desc) < min_recent_messages:
            forced_recent_desc.append(message)
    if current_user is not None and all(
        message.id != current_user.id for message in forced_recent_desc
    ):
        forced_recent_desc.insert(0, current_user)
    return forced_recent_desc


def _append_budgeted_recent_messages(
    all_rows_desc: list[Message],
    *,
    forced_recent_desc: list[Message],
    sticky_message: Message | None,
    input_budget: int,
    used_tokens: int,
    hooks: Any,
) -> tuple[list[Message], int]:
    forced_ids = {message.id for message in forced_recent_desc}
    recent_desc = list(forced_recent_desc)
    for message in forced_recent_desc:
        used_tokens += hooks.count_message_tokens(message.role, message.content)
    for message in all_rows_desc:
        if message.id in forced_ids:
            continue
        if sticky_message is not None and message.id == sticky_message.id:
            continue
        estimated = hooks.count_message_tokens(message.role, message.content)
        if estimated <= 0:
            continue
        if used_tokens + estimated > input_budget:
            break
        recent_desc.append(message)
        forced_ids.add(message.id)
        used_tokens += estimated
    return recent_desc, used_tokens


def _select_recent_messages(
    *,
    all_rows_desc: list[Message],
    system_prompt: str | None,
    input_budget: int,
    min_recent_messages: int,
    first_user: Message | None,
    current_user: Message | None,
    target_tokens: int,
    hooks: Any,
) -> _RecentSelection:
    forced_recent_desc = _forced_recent_messages(
        all_rows_desc,
        min_recent_messages=min_recent_messages,
        current_user=current_user,
        hooks=hooks,
    )

    forced_ids = {message.id for message in forced_recent_desc}
    first_user_in_recent = first_user is not None and first_user.id in forced_ids
    sticky_message = (
        first_user if first_user is not None and not first_user_in_recent else None
    )
    sticky_tokens = 0
    if sticky_message is not None:
        sticky_tokens = MESSAGE_OVERHEAD_TOKENS + hooks.count_tokens(
            format_sticky_input_text(_sticky_text_from_message(sticky_message))
        )

    used_tokens = (
        hooks.estimate_system_prompt_tokens(
            _instructions_with_summary_guardrail(system_prompt, enabled=True)
        )
        + sticky_tokens
        + target_tokens
        + MESSAGE_OVERHEAD_TOKENS
    )
    recent_desc, used_tokens = _append_budgeted_recent_messages(
        all_rows_desc,
        forced_recent_desc=forced_recent_desc,
        sticky_message=sticky_message,
        input_budget=input_budget,
        used_tokens=used_tokens,
        hooks=hooks,
    )

    recent_ids = {message.id for message in recent_desc}
    summary_rows = [
        message
        for message in all_rows_desc
        if hooks.count_message_tokens(message.role, message.content) > 0
        and message.id not in recent_ids
        and (sticky_message is None or message.id != sticky_message.id)
    ]
    return _RecentSelection(
        first_user=first_user,
        current_user=current_user,
        recent_desc=recent_desc,
        summary_rows=summary_rows,
        sticky_message=sticky_message,
        sticky_tokens=sticky_tokens,
        used_tokens=used_tokens,
    )


def _summary_recently_refreshed(
    summary: dict[str, Any] | None,
    min_interval_seconds: int,
) -> bool:
    if not is_summary_usable(summary) or min_interval_seconds <= 0:
        return False
    compressed_at = _summary_compressed_at(summary)
    if compressed_at is None:
        return False
    now = datetime.now(compressed_at.tzinfo or timezone.utc)
    return (now - compressed_at).total_seconds() < min_interval_seconds


def _summary_boundary(
    *,
    conversation_id: str,
    boundary_message: Message,
    selection: _RecentSelection,
    sticky_message: Message | None,
) -> _SummaryBoundary:
    return _SummaryBoundary(
        conversation_id=conversation_id,
        up_to_message_id=boundary_message.id,
        up_to_created_at=_message_created_at(boundary_message),
        first_user_message_id=(
            selection.first_user.id if selection.first_user is not None else None
        ),
        recent_message_ids=[message.id for message in reversed(selection.recent_desc)],
        summary_message_ids=[
            message.id for message in reversed(selection.summary_rows)
        ],
        source_message_count=len(selection.summary_rows),
        source_token_estimate=_estimated_summary_source(
            selection.summary_rows,
            skip_message_id=(sticky_message.id if sticky_message is not None else None),
        ),
    )


async def _refresh_summary(
    session: Any,
    *,
    conversation: Conversation | None,
    conversation_id: str,
    boundary_message: Message,
    redis: Any | None,
    settings: _CompressionSettings,
    selection: _RecentSelection,
    summary: dict[str, Any] | None,
    hooks: Any,
) -> tuple[dict[str, Any] | None, bool]:
    if conversation is None or _summary_covers_boundary(summary, boundary_message):
        return summary, False
    boundary = _summary_boundary(
        conversation_id=conversation_id,
        boundary_message=boundary_message,
        selection=selection,
        sticky_message=selection.sticky_message,
    )
    try:
        new_summary = await hooks.ensure_context_summary(
            session,
            conversation,
            boundary,
            target_tokens=settings.target_tokens,
            model=settings.summary_model,
            redis=redis,
        )
    except Exception as exc:  # noqa: BLE001
        hooks.logger.warning(
            "context summary generation failed conversation=%s err=%s",
            conversation_id,
            exc,
        )
        return summary, False
    if is_summary_usable(new_summary):
        return cast(dict[str, Any], new_summary), True
    return summary, False


def _summary_context(
    *,
    system_prompt: str | None,
    summary: dict[str, Any],
    boundary_message: Message,
    selection: _RecentSelection,
    summary_created: bool,
    summary_model: str,
    hooks: Any,
) -> PackedContext:
    summary_text = str(summary.get("text") or "")
    summary_token_count = estimate_summary_tokens(summary)
    recent_rows = tuple(reversed(selection.recent_desc))
    estimated_tokens = (
        hooks.estimate_system_prompt_tokens(
            _instructions_with_summary_guardrail(system_prompt, enabled=True)
        )
        + (selection.sticky_tokens if selection.sticky_message is not None else 0)
        + MESSAGE_OVERHEAD_TOKENS
        + summary_token_count
        + sum(
            hooks.count_message_tokens(message.role, message.content)
            for message in recent_rows
        )
    )
    return PackedContext(
        input_list=[],
        estimated_tokens=estimated_tokens,
        summary_used=True,
        summary_created=summary_created,
        summary_up_to_message_id=str(
            summary.get("up_to_message_id") or boundary_message.id
        ),
        sticky_used=selection.sticky_message is not None,
        included_messages_count=len(recent_rows)
        + (1 if selection.sticky_message is not None else 0),
        truncated_without_summary=False,
        fallback_reason=None,
        compression_enabled=True,
        recent_messages_count=len(recent_rows),
        summary_tokens=summary_token_count,
        summary_age_seconds=_summary_age_seconds(summary),
        compressor_model=summary_model,
        image_caption_count=int(summary.get("image_caption_count") or 0),
        _system_prompt=system_prompt,
        _sticky_message=selection.sticky_message,
        _summary_text=summary_text,
        _recent_rows=recent_rows,
    )


async def _pack_compressed_context(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    system_prompt: str | None,
    redis: Any | None,
    conversation: Conversation | None,
    summary: dict[str, Any] | None,
    retention_filter: Any | None,
    all_rows_desc: list[Message],
    total_used_tokens: int,
    current_user: Message | None,
    selection: _RecentSelection,
    existing_summary_packed: PackedContext | None,
    settings: _CompressionSettings,
    hooks: Any,
) -> PackedContext:
    if not selection.summary_rows:
        packed = existing_summary_packed or _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=settings.summary_model,
        )
        return await _with_input(session, packed, hooks)

    boundary_message = max(
        selection.summary_rows,
        key=lambda message: (_message_created_at(message), message.id),
    )
    summary_is_recent = _summary_recently_refreshed(
        summary,
        settings.min_interval_seconds,
    )
    if summary_is_recent and not _summary_covers_boundary(
        summary,
        boundary_message,
    ):
        packed = await _load_budget_fallback(
            session,
            conversation_id=conversation_id,
            target=target,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
            input_budget=settings.input_budget,
            current_user=current_user,
            summary_model=settings.summary_model,
            reason="rate_limited",
            hooks=hooks,
        )
        return await _with_input(session, packed, hooks)

    summary, summary_created = await _refresh_summary(
        session,
        conversation=conversation,
        conversation_id=conversation_id,
        boundary_message=boundary_message,
        redis=redis,
        settings=settings,
        selection=selection,
        summary=summary,
        hooks=hooks,
    )
    if not _summary_covers_boundary(summary, boundary_message):
        packed = await _load_budget_fallback(
            session,
            conversation_id=conversation_id,
            target=target,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
            input_budget=settings.input_budget,
            current_user=current_user,
            summary_model=settings.summary_model,
            reason="summary_failed",
            hooks=hooks,
        )
        return await _with_input(session, packed, hooks)

    packed = _summary_context(
        system_prompt=system_prompt,
        summary=cast(dict[str, Any], summary),
        boundary_message=boundary_message,
        selection=selection,
        summary_created=summary_created,
        summary_model=settings.summary_model,
        hooks=hooks,
    )
    return await _with_input(session, packed, hooks)


async def pack_recent_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    redis: Any | None = None,
    chat_model: str | None = None,
    account_mode: str | None = None,
    hooks: Any,
) -> PackedContext:
    target = await session.get(Message, up_to_message_id)
    if target is None:
        return _empty_context(system_prompt)
    settings = await _resolve_settings(
        chat_model=chat_model,
        hooks=hooks,
    )

    conversation = await session.get(Conversation, conversation_id)
    summary = (
        getattr(conversation, "summary_jsonb", None)
        if conversation is not None
        else None
    )
    retention_filter = await hooks.message_retention_filter_for_account(account_mode)
    if retention_filter is not None:
        summary = None

    existing_summary_packed = None
    if is_summary_usable(summary):
        existing_summary_packed = await _existing_summary_context(
            session,
            conversation_id=conversation_id,
            target=target,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
            summary=cast(dict[str, Any], summary),
            settings=settings,
            hooks=hooks,
        )
        if existing_summary_packed is not None and (
            existing_summary_packed.input_list
            or not settings.compression_enabled
            or existing_summary_packed.estimated_tokens < settings.trigger_tokens
        ):
            return existing_summary_packed

    all_rows_desc, total_used_tokens, total_truncated = await hooks.load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=None,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
    )
    first_user = hooks.pick_first_user(all_rows_desc)
    current_user = await hooks.pick_current_user_with_lookup(
        session,
        all_rows_desc,
        target,
    )

    if not settings.compression_enabled:
        packed = await _load_uncompressed_context(
            session,
            system_prompt=system_prompt,
            all_rows_desc=all_rows_desc,
            input_budget=settings.input_budget,
            hooks=hooks,
        )
        return await _with_input(session, packed, hooks)

    if await hooks.context_circuit_open(redis):
        packed = await _load_circuit_fallback(
            session,
            conversation_id=conversation_id,
            target=target,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
            input_budget=settings.input_budget,
            summary_model=settings.summary_model,
            hooks=hooks,
        )
        return await _with_input(session, packed, hooks)

    if (
        not is_summary_usable(summary)
        and total_used_tokens < settings.trigger_tokens
        and not total_truncated
    ):
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=settings.summary_model,
        )
        return await _with_input(session, packed, hooks)

    selection = _select_recent_messages(
        all_rows_desc=all_rows_desc,
        system_prompt=system_prompt,
        input_budget=settings.input_budget,
        min_recent_messages=settings.min_recent_messages,
        first_user=first_user,
        current_user=current_user,
        target_tokens=settings.target_tokens,
        hooks=hooks,
    )
    return await _pack_compressed_context(
        session,
        conversation_id=conversation_id,
        target=target,
        system_prompt=system_prompt,
        redis=redis,
        conversation=conversation,
        summary=summary,
        retention_filter=retention_filter,
        all_rows_desc=all_rows_desc,
        total_used_tokens=total_used_tokens,
        current_user=current_user,
        selection=selection,
        existing_summary_packed=existing_summary_packed,
        settings=settings,
        hooks=hooks,
    )


__all__ = ["pack_recent_history"]
