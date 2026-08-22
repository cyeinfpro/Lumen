"""Shared Agent API settings, provider preflight, billing, and SSE helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.agent_events import agent_channel, agent_event_id
from lumen_core.context_window import estimate_message_tokens
from lumen_core.model_entities import AgentRun, OutboxEvent
from lumen_core.providers_parts.config import parse_provider_json
from lumen_core.runtime_settings import get_spec
from lumen_core.schema_models import AgentEventEnvelope

from ...audit import write_audit
from ...redis_client import get_redis
from ...runtime_settings import get_setting
from ...sse_publish import publish_sse_event
from ...task_billing import user_rate_multiplier_x10000
from ..message_submission_billing import billing_allow_negative, billing_enabled


logger = logging.getLogger(__name__)

AGENT_SETTING_DEFAULTS = MappingProxyType(
    {
        "agent.max_turns": 6,
        "agent.max_tool_calls": 3,
        "agent.max_image_tool_calls": 2,
        "agent.max_images_per_run": 4,
        "agent.max_reference_images": 4,
        "agent.max_output_tokens": 4096,
        "agent.run_timeout_seconds": 180,
        "agent.tool_timeout_seconds": 30,
        "agent.capability_ttl_seconds": 120,
    }
)


@dataclass(frozen=True, slots=True)
class AgentProviderPreflight:
    model: str
    eligible_provider_names: tuple[str, ...]
    context_window: int = 128_000
    max_output_tokens: int = 16_384
    reasoning_supported: bool = True


@dataclass(frozen=True, slots=True)
class AgentTextReservation:
    hold_micro: int
    billing_snapshot: dict[str, Any]


def http_error(
    code: str,
    message: str,
    status_code: int = 400,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(status_code=status_code, detail={"error": error})


async def agent_setting_int(
    db: AsyncSession,
    key: str,
    default: int | None = None,
) -> int:
    fallback = AGENT_SETTING_DEFAULTS.get(key) if default is None else default
    if fallback is None:
        raise KeyError(key)
    spec = get_spec(key)
    if spec is None:
        return int(fallback)
    raw = await get_setting(db, spec)
    if raw in (None, ""):
        return int(fallback)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(fallback)
    if spec.min_value is not None:
        value = max(int(spec.min_value), value)
    if spec.max_value is not None:
        value = min(int(spec.max_value), value)
    return value


async def wallet_chat_provider_preflight(
    db: AsyncSession,
    *,
    require_vision: bool,
) -> AgentProviderPreflight:
    providers_spec = get_spec("providers")
    raw = await get_setting(db, providers_spec) if providers_spec is not None else None
    providers, errors = parse_provider_json(raw)
    model_spec = get_spec("upstream.default_model")
    model = await get_setting(db, model_spec) if model_spec is not None else None
    if not isinstance(model, str) or not model.strip():
        raise http_error(
            "agent_provider_unavailable",
            "Agent chat model is not configured",
            503,
        )
    model = model.strip()
    eligible = [
        provider
        for provider in providers
        if provider.enabled
        and "chat" in provider.purposes
        and provider.responses_supported is not False
        and (not provider.agent_models or model in provider.agent_models)
    ]
    if not eligible:
        raise http_error(
            "agent_provider_unavailable",
            "no Agent chat provider is available",
            503,
            configuration_errors=len(errors),
        )
    if require_vision:
        eligible = [
            provider for provider in eligible if provider.vision_supported is True
        ]
        if not eligible:
            raise http_error(
                "agent_vision_model_unavailable",
                "no configured Agent chat provider has verified image input support",
                412,
            )
    return AgentProviderPreflight(
        model=model,
        eligible_provider_names=tuple(provider.name for provider in eligible),
        context_window=max(provider.agent_context_window for provider in eligible),
        max_output_tokens=max(
            provider.agent_max_output_tokens for provider in eligible
        ),
        reasoning_supported=any(
            provider.agent_reasoning_supported for provider in eligible
        ),
    )


async def wallet_image_provider_preflight(db: AsyncSession) -> tuple[str, ...]:
    providers_spec = get_spec("providers")
    raw = await get_setting(db, providers_spec) if providers_spec is not None else None
    providers, errors = parse_provider_json(raw)
    eligible = tuple(
        provider.name
        for provider in providers
        if provider.enabled and "image" in provider.purposes
    )
    if not eligible:
        raise http_error(
            "agent_image_provider_unavailable",
            "no Agent image provider is available",
            503,
            configuration_errors=len(errors),
        )
    return eligible


def byok_vision_supported(capabilities: dict[str, Any] | None) -> bool:
    if not isinstance(capabilities, dict):
        return False
    if capabilities.get("vision_supported") is True:
        return True
    modalities = capabilities.get("input_modalities")
    return isinstance(modalities, list) and "image" in modalities


async def reserve_agent_text(
    db: AsyncSession,
    *,
    run: AgentRun,
    user_id: str,
    account_mode: str,
    model: str,
    text: str,
    reference_count: int,
    context_window: int = 128_000,
    provider_max_output_tokens: int = 32_000,
) -> AgentTextReservation:
    if account_mode != "wallet" or not await billing_enabled(db):
        return AgentTextReservation(hold_micro=0, billing_snapshot={})

    max_turns = await agent_setting_int(db, "agent.max_turns")
    configured_output_tokens = await agent_setting_int(
        db, "agent.max_output_tokens"
    )
    max_output_tokens = min(
        configured_output_tokens,
        max(1, provider_max_output_tokens),
    )
    bounded_context_window = max(4096, min(2_000_000, context_window))
    input_per_turn = max(1, bounded_context_window - max_output_tokens)
    input_upper = input_per_turn * max_turns
    output_upper = max_output_tokens * max_turns
    try:
        pricing_snapshot = await billing_core.completion_pricing_snapshot(
            db,
            model=model,
        )
        multiplier = int(await user_rate_multiplier_x10000(db, user_id))
        candidates = []
        for input_kind in (
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cache_creation_1h_tokens",
        ):
            for output_kind in ("output_tokens", "reasoning_tokens"):
                values = {
                    "input_tokens": 0,
                    "output_tokens": output_upper,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_creation_1h_tokens": 0,
                    "reasoning_tokens": 0,
                }
                values[input_kind] = input_upper
                if output_kind == "reasoning_tokens":
                    values["reasoning_tokens"] = output_upper
                candidates.append(
                    billing_core.completion_breakdown_from_snapshot(
                        pricing_snapshot,
                        model=model,
                        tokens=billing_core.UsageTokens(**values),
                        rate_multiplier_x10000=multiplier,
                    )
                )
        breakdown = max(
            candidates,
            key=lambda item: int(item.actual_cost_micro or 0),
        )
    except billing_core.BillingError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    hold_micro = (
        0 if multiplier == 0 else max(10_000, int(breakdown.actual_cost_micro or 0))
    )
    snapshot = {
        "pricing_snapshot": pricing_snapshot,
        "rate_multiplier_x10000": multiplier,
        "max_turns": max_turns,
        "max_output_tokens": max_output_tokens,
        "context_window": bounded_context_window,
        "reference_count": reference_count,
        "current_message_tokens": max(
            1,
            estimate_message_tokens(
                "user",
                {
                    "text": text,
                    "attachments": [
                        {"image_id": f"ref-{index}"}
                        for index in range(reference_count)
                    ],
                },
            ),
        ),
        "reserved_input_tokens": input_upper,
        "reserved_output_tokens": output_upper,
        "estimated_micro": hold_micro,
    }
    if hold_micro <= 0:
        return AgentTextReservation(hold_micro=0, billing_snapshot=snapshot)
    try:
        transaction = await billing_core.hold(
            db,
            user_id,
            hold_micro,
            ref_type="agent_run",
            ref_id=run.id,
            idempotency_key=f"hold:{run.id}",
            allow_negative=await billing_allow_negative(db),
            meta=snapshot,
        )
    except billing_core.BillingError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    if transaction is not None:
        await write_audit(
            db,
            event_type="wallet.hold.agent",
            user_id=user_id,
            details={
                "agent_run_id": run.id,
                "amount_micro": hold_micro,
                "balance_after": transaction.balance_after,
                "hold_after": transaction.hold_after,
                "max_turns": max_turns,
                "max_output_tokens": max_output_tokens,
            },
            autocommit=False,
        )
    return AgentTextReservation(hold_micro=hold_micro, billing_snapshot=snapshot)


def stage_agent_event(
    db: AsyncSession,
    *,
    run: AgentRun,
    event_name: str,
    tool_call_id: str | None = None,
    generation_ids: list[str] | None = None,
) -> dict[str, Any]:
    run.last_event_seq = int(run.last_event_seq or 0) + 1
    envelope = AgentEventEnvelope(
        agent_session_id=run.agent_session_id,
        agent_run_id=run.id,
        assistant_message_id=run.assistant_message_id,
        execution_epoch=run.execution_epoch,
        event_seq=run.last_event_seq,
        event_name=event_name,
        tool_call_id=tool_call_id,
        generation_ids=list(generation_ids or []),
    ).model_dump(mode="json")
    envelope["event_id"] = agent_event_id(
        run.id,
        run.execution_epoch,
        run.last_event_seq,
    )
    db.add(
        OutboxEvent(
            kind="sse",
            payload={
                "user_id": run.user_id,
                "channel": agent_channel(run.agent_session_id),
                "event_name": event_name,
                "data": envelope,
            },
            published_at=None,
        )
    )
    return envelope


def stage_agent_run_dispatch(db: AsyncSession, run: AgentRun) -> OutboxEvent:
    row = OutboxEvent(
        kind="agent_run",
        payload={
            "kind": "agent_run",
            "task_id": run.id,
            "user_id": run.user_id,
            "agent_session_id": run.agent_session_id,
            "execution_epoch": run.execution_epoch,
        },
        published_at=None,
    )
    db.add(row)
    return row


async def publish_agent_events_best_effort(
    *,
    user_id: str,
    agent_session_id: str,
    events: list[dict[str, Any]],
) -> None:
    if not events:
        return
    redis = get_redis()
    for event in events:
        try:
            await publish_sse_event(
                redis,
                user_id=user_id,
                channel=agent_channel(agent_session_id),
                event_name=str(event["event_name"]),
                data=event,
            )
        except Exception:
            logger.warning(
                "agent SSE fast path failed user=%s session=%s run=%s",
                user_id,
                agent_session_id,
                event.get("agent_run_id"),
                exc_info=True,
            )


async def release_queued_agent_hold(
    db: AsyncSession,
    *,
    run: AgentRun,
    reason: str,
) -> bool:
    if run.account_mode_snapshot != "wallet" or int(run.text_hold_micro or 0) <= 0:
        return False
    try:
        transaction = await billing_core.release(
            db,
            run.user_id,
            ref_type="agent_run",
            ref_id=run.id,
            idempotency_key=f"release:{run.id}:{reason}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    return transaction is not None


__all__ = [
    "AGENT_SETTING_DEFAULTS",
    "AgentProviderPreflight",
    "AgentTextReservation",
    "agent_setting_int",
    "byok_vision_supported",
    "http_error",
    "publish_agent_events_best_effort",
    "release_queued_agent_hold",
    "reserve_agent_text",
    "stage_agent_event",
    "stage_agent_run_dispatch",
    "wallet_chat_provider_preflight",
    "wallet_image_provider_preflight",
]
