"""Worker-side video billing decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import AuditLog, VideoGeneration, WalletTransaction
from lumen_core.upstream_billing import (
    LocalBillingAction,
    UpstreamCostKnowledge,
    decide_upstream_billing,
)
from lumen_core.video_billing import (
    VideoBillingError,
    settle_video_cost,
    video_billing_model,
    video_pricing_variant,
)

from . import billing as worker_billing


logger = logging.getLogger(__name__)
_DEFAULTABLE_VIDEO_BILLING_ERRORS = frozenset({"video_pricing_missing"})


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


def _default_video_charge_micro(generation: VideoGeneration, held: int) -> int:
    return max(int(held), int(generation.est_cost_micro or 0))


def _no_upstream_cost_receipts(poll_result: Any, reason: str) -> tuple[str | None, ...]:
    """收集「上游未扣费」的本地收据原因，交给 core 决策表判定。

    白名单本身住在 `lumen_core.upstream_billing`，这里只负责把 poll 结果里的
    原因字段翻出来，避免各调用点各自维护一份白名单副本。
    """
    raw = _poll_attr(poll_result, "raw")
    raw_reason = raw.get("reason") if isinstance(raw, dict) else None
    return (reason, str(raw_reason) if raw_reason is not None else None)


def _usage_total_tokens(poll_result: Any) -> int | None:
    raw = _poll_attr(poll_result, "usage_total_tokens")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _default_decision_name(*, succeeded: bool, upstream_billable: bool | None) -> str:
    """SETTLE_DEFAULT 分支的决策名——只影响可观测性，不影响金额。"""
    if succeeded:
        return "missing_usage_default_charge"
    if upstream_billable is False:
        # 上游声称没扣费但拿不出本地收据：按纯转嫁默认结算，同时用决策名
        # 把「标志不可信」这件事留在审计里。
        return "upstream_not_billable_untrusted_default_charge"
    if upstream_billable is True:
        return "failure_billable_default_charge"
    return "unknown_default_charge"


async def _usage_charge_micro(
    session: AsyncSession,
    generation: VideoGeneration,
    *,
    held: int,
    usage_tokens: int,
    pricing_variant: str,
    billing_model: str,
) -> int:
    """Return the amount to settle, always passing upstream cost through.

    ``settle_video_cost`` raises ``video_cost_exceeds_estimate`` when the real
    cost is far above the hold. That is *not* a pricing failure — the cost is
    known, it is simply large — so we still charge it in full per the
    pure-pass-through rule, and only raise an operator alert. Falling back to
    ``max(held, est)`` here would make the platform silently eat the excess,
    which is exactly what 新-1 is about.
    """
    try:
        return await settle_video_cost(
            session,
            model=billing_model,
            action=generation.action,
            actual_total_tokens=usage_tokens,
            resolution=generation.resolution,
            pricing_variant=pricing_variant,
            estimated_micro=_default_video_charge_micro(generation, held),
        )
    except VideoBillingError as exc:
        if exc.code != "video_cost_exceeds_estimate" or exc.actual_micro is None:
            raise
        logger.error(
            "video_billing.cost_exceeds_estimate generation_id=%s user_id=%s "
            "held_micro=%s est_micro=%s actual_micro=%s — charging in full "
            "(pure pass-through); review upstream usage",
            generation.id,
            generation.user_id,
            held,
            generation.est_cost_micro,
            exc.actual_micro,
        )
        return int(exc.actual_micro)


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
    usage_tokens = _usage_total_tokens(poll_result)
    upstream_billable = _poll_attr(poll_result, "upstream_billable")
    status = str(_poll_attr(poll_result, "status", "") or "")
    succeeded = status == "succeeded"
    pricing_variant = _generation_pricing_variant(generation)
    billing_model = _generation_billing_model(generation)

    # 决策表在 packages/core：调用点只提供证据（上游标志 + 本地收据 + 是否
    # 拿到真实用量），动作由表统一裁定。上游已经把任务跑成功了，本身就是
    # 「上游已扣费」的证据，所以 succeeded 时把缺失的标志补成 True。
    billable_evidence = upstream_billable
    if succeeded and billable_evidence is None:
        billable_evidence = True
    decided = decide_upstream_billing(
        upstream_billable=billable_evidence,
        actual_cost_known=usage_tokens is not None,
        receipt_reasons=_no_upstream_cost_receipts(poll_result, reason),
    )
    if decided.action is LocalBillingAction.RELEASE:
        return await _release_video_hold(
            session,
            generation,
            reason=reason,
            decision="upstream_not_billable_release",
            actual_tokens=usage_tokens,
            pricing_variant=pricing_variant,
        )

    if decided.action is LocalBillingAction.SETTLE_ACTUAL and usage_tokens is not None:
        try:
            actual_micro = await _usage_charge_micro(
                session,
                generation,
                held=held,
                usage_tokens=usage_tokens,
                pricing_variant=pricing_variant,
                billing_model=billing_model,
            )
            decision = "actual_usage_settle" if succeeded else "failure_usage_settle"
        except VideoBillingError as exc:
            if exc.code not in _DEFAULTABLE_VIDEO_BILLING_ERRORS:
                raise
            # 定价规则缺失 → 算不出真实成本，但上游仍可能已扣费，
            # 因此退回默认金额结算而不是 release。
            actual_micro = _default_video_charge_micro(generation, held)
            decision = (
                "pricing_missing_default_charge"
                if succeeded
                else "failure_pricing_missing_default_charge"
            )
    else:
        actual_micro = _default_video_charge_micro(generation, held)
        decision = _default_decision_name(
            succeeded=succeeded,
            upstream_billable=upstream_billable,
        )

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
            knowledge=decided.knowledge,
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
            knowledge=UpstreamCostKnowledge.PROVEN_ABSENT,
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
    knowledge: UpstreamCostKnowledge | None = None,
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
    if knowledge is not None:
        # 把决策表的输入（可知性）一并落库，对账时可以回放这次判断。
        meta["upstream_cost_knowledge"] = str(knowledge)
    if actual_tokens is not None:
        meta["actual_tokens"] = actual_tokens
    if actual_micro is not None:
        meta["actual_micro"] = actual_micro
    return meta


__all__ = ["VideoBillingResolution", "resolve_video_billing"]
