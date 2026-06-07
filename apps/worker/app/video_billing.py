"""Worker-side video billing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import AuditLog, VideoGeneration, WalletTransaction
from lumen_core.video_billing import (
    VideoBillingError,
    settle_video_cost,
    video_billing_model,
    video_pricing_variant,
)

from . import billing as worker_billing


@dataclass(frozen=True)
class VideoBillingResolution:
    decision: str
    actual_micro: int
    actual_tokens: int | None
    released: bool
    tx: WalletTransaction | None


def _poll_attr(poll_result: Any, name: str, default: Any = None) -> Any:
    if isinstance(poll_result, dict):
        return poll_result.get(name, default)
    return getattr(poll_result, name, default)


def _generation_reference_media(generation: VideoGeneration) -> list[Any]:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    raw = request.get("reference_media")
    return raw if isinstance(raw, list) else []


def _generation_pricing_variant(generation: VideoGeneration) -> str:
    return video_pricing_variant(
        generation.action,
        _generation_reference_media(generation),
        resolution=generation.resolution,
    )


def _generation_upstream_model(generation: VideoGeneration) -> str | None:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    raw = request.get("upstream_model")
    return raw if isinstance(raw, str) else None


def _generation_billing_model(generation: VideoGeneration) -> str:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    raw = request.get("billing_model")
    model = raw if isinstance(raw, str) and raw.strip() else generation.model
    return video_billing_model(model, _generation_upstream_model(generation))


async def resolve_video_billing(
    session: AsyncSession,
    generation: VideoGeneration,
    *,
    poll_result: Any,
    reason: str,
) -> VideoBillingResolution:
    held = await worker_billing.held_amount_for_ref(
        session,
        generation.user_id,
        "video_generation",
        generation.id,
    )
    usage_tokens_raw = _poll_attr(poll_result, "usage_total_tokens")
    usage_tokens: int | None = None
    if usage_tokens_raw is not None:
        try:
            parsed = int(usage_tokens_raw)
            usage_tokens = parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            usage_tokens = None
    upstream_billable = _poll_attr(poll_result, "upstream_billable")
    status = str(_poll_attr(poll_result, "status", "") or "")
    pricing_variant = _generation_pricing_variant(generation)
    billing_model = _generation_billing_model(generation)

    if status == "succeeded" and upstream_billable is False:
        return await _release_video_hold(
            session,
            generation,
            reason=reason,
            decision="upstream_not_billable_release",
            actual_tokens=usage_tokens,
            pricing_variant=pricing_variant,
        )

    if status == "succeeded" and usage_tokens is not None:
        try:
            actual_micro = await settle_video_cost(
                session,
                model=billing_model,
                action=generation.action,
                actual_total_tokens=usage_tokens,
                resolution=generation.resolution,
                pricing_variant=pricing_variant,
            )
            decision = "actual_usage_settle"
        except VideoBillingError:
            actual_micro = max(held, int(generation.est_cost_micro or 0))
            decision = "pricing_missing_default_charge"
    elif status == "succeeded" and upstream_billable is True:
        actual_micro = max(held, int(generation.est_cost_micro or 0))
        decision = "missing_usage_default_charge"
    elif status == "succeeded":
        return await _release_video_hold(
            session,
            generation,
            reason=reason,
            decision="missing_usage_release",
            actual_tokens=usage_tokens,
            pricing_variant=pricing_variant,
        )
    elif upstream_billable is False:
        return await _release_video_hold(
            session,
            generation,
            reason=reason,
            decision="upstream_not_billable_release",
            actual_tokens=usage_tokens,
            pricing_variant=pricing_variant,
        )
    elif upstream_billable is not True:
        return await _release_video_hold(
            session,
            generation,
            reason=reason,
            decision="terminal_not_billable_release",
            actual_tokens=usage_tokens,
            pricing_variant=pricing_variant,
        )
    elif usage_tokens is not None:
        try:
            actual_micro = await settle_video_cost(
                session,
                model=billing_model,
                action=generation.action,
                actual_total_tokens=usage_tokens,
                resolution=generation.resolution,
                pricing_variant=pricing_variant,
            )
            decision = "failure_usage_settle"
        except VideoBillingError:
            actual_micro = max(held, int(generation.est_cost_micro or 0))
            decision = "failure_pricing_missing_default_charge"
    elif upstream_billable is True:
        actual_micro = max(held, int(generation.est_cost_micro or 0))
        decision = "failure_billable_default_charge"
    else:
        actual_micro = max(held, int(generation.est_cost_micro or 0))
        decision = "unknown_default_charge"

    tx = await billing_core.settle(
        session,
        generation.user_id,
        ref_type="video_generation",
        ref_id=generation.id,
        actual_micro=actual_micro,
        idempotency_key=f"video_generation:settle:{generation.id}",
        allow_negative=await worker_billing.allow_negative_balance(),
        meta=_billing_meta(
            generation,
            decision=decision,
            reason=reason,
            actual_tokens=usage_tokens,
            actual_micro=actual_micro,
            pricing_variant=pricing_variant,
        ),
    )
    if tx is not None:
        worker_billing._record_balance_cache_refresh(  # noqa: SLF001
            session,
            user_id=generation.user_id,
            balance_after=tx.balance_after,
        )
        session.add(
            AuditLog(
                user_id=generation.user_id,
                event_type="wallet.settle.video",
                details={
                    "video_generation_id": generation.id,
                    "decision": decision,
                    "reason": reason,
                    "actual_tokens": usage_tokens,
                    "actual_micro": actual_micro,
                    "amount_micro": tx.amount_micro,
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                    "provider_name": generation.provider_name,
                    "provider_task_id": generation.provider_task_id,
                    "pricing_variant": pricing_variant,
                },
            )
        )
    return VideoBillingResolution(
        decision=decision,
        actual_micro=actual_micro,
        actual_tokens=usage_tokens,
        released=False,
        tx=tx,
    )


async def _release_video_hold(
    session: AsyncSession,
    generation: VideoGeneration,
    *,
    reason: str,
    decision: str,
    actual_tokens: int | None,
    pricing_variant: str,
) -> VideoBillingResolution:
    tx = await billing_core.release(
        session,
        generation.user_id,
        ref_type="video_generation",
        ref_id=generation.id,
        idempotency_key=f"video_generation:release:{generation.id}",
        meta=_billing_meta(
            generation,
            decision=decision,
            reason=reason,
            actual_tokens=actual_tokens,
            pricing_variant=pricing_variant,
        ),
    )
    if tx is not None:
        worker_billing._record_balance_cache_refresh(  # noqa: SLF001
            session,
            user_id=generation.user_id,
            balance_after=tx.balance_after,
        )
        session.add(
            AuditLog(
                user_id=generation.user_id,
                event_type="wallet.release.video",
                details={
                    "video_generation_id": generation.id,
                    "reason": reason,
                    "decision": decision,
                    "actual_tokens": actual_tokens,
                    "amount_micro": tx.amount_micro,
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                    "provider_name": generation.provider_name,
                    "provider_task_id": generation.provider_task_id,
                    "pricing_variant": pricing_variant,
                },
            )
        )
    return VideoBillingResolution(
        decision=decision,
        actual_micro=0,
        actual_tokens=actual_tokens,
        released=True,
        tx=tx,
    )


def _billing_meta(
    generation: VideoGeneration,
    *,
    decision: str,
    reason: str,
    actual_tokens: int | None,
    actual_micro: int | None = None,
    pricing_variant: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "model": generation.model,
        "billing_model": _generation_billing_model(generation),
        "action": generation.action,
        "resolution": generation.resolution,
        "duration_s": generation.duration_s,
        "estimated_tokens": generation.est_token_upper,
        "provider_name": generation.provider_name,
        "provider_task_id": generation.provider_task_id,
        "pricing_variant": pricing_variant or _generation_pricing_variant(generation),
        "billing_decision": decision,
        "reason": reason,
    }
    if actual_tokens is not None:
        meta["actual_tokens"] = actual_tokens
    if actual_micro is not None:
        meta["actual_micro"] = actual_micro
    return meta


__all__ = ["VideoBillingResolution", "resolve_video_billing"]
