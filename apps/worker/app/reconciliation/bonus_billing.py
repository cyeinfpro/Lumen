"""Repair successful bonus generations whose wallet settlement is missing."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, func, or_, select

from lumen_core.constants import GenerationStatus
from lumen_core.models import Generation, Image, WalletTransaction

from ..billing_parts.common import (
    BILLING_OBLIGATION_STATE_KEY,
    BILLING_OBLIGATION_TERMINAL_REASON_KEY,
    BILLING_OBLIGATION_UNSETTLEABLE,
    billing_obligation_is_unsettleable,
    mark_billing_obligation_unsettleable,
)
from .contracts import ReconcileContext, ReconcileResult
from .metrics import reconciliation_rows_total

BONUS_BILLING_POLICIES = (
    "dual_race_loser_settled_separately",
    "batch_extra_settled_separately",
)
BONUS_BILLING_OBLIGATION_KEY = "bonus_billing_obligation"
BONUS_BILLING_WIDTH_KEY = "bonus_billing_width"
BONUS_BILLING_HEIGHT_KEY = "bonus_billing_height"
BONUS_ARTIFACT_STATE_KEY = "bonus_artifact_state"
BONUS_ARTIFACT_PENDING = "pending"
BONUS_ARTIFACT_COMMITTED = "committed"
BONUS_ARTIFACT_RECONCILED = "reconciled_without_artifact"
BONUS_ARTIFACT_RECONCILED_AT_KEY = "bonus_artifact_reconciled_at"


def _completed_settlement_condition(
    *,
    user_id: Any,
    ref_id: Any,
) -> Any:
    settlement_actual_micro = WalletTransaction.meta["actual_micro"].as_integer()
    settlement_rate_multiplier = WalletTransaction.meta[
        "rate_multiplier_x10000"
    ].as_integer()
    return and_(
        WalletTransaction.user_id == user_id,
        WalletTransaction.ref_type == "generation",
        WalletTransaction.ref_id == ref_id,
        WalletTransaction.kind == "settle",
        or_(
            func.coalesce(settlement_actual_micro, 1) > 0,
            and_(
                settlement_actual_micro == 0,
                settlement_rate_multiplier == 0,
            ),
        ),
    )


def _obligation_dimensions(request: dict[str, Any]) -> tuple[int, int] | None:
    if request.get(BONUS_BILLING_OBLIGATION_KEY) is not True:
        return None
    return _positive_dimensions(
        request.get(BONUS_BILLING_WIDTH_KEY),
        request.get(BONUS_BILLING_HEIGHT_KEY),
    )


def _positive_dimensions(width_value: Any, height_value: Any) -> tuple[int, int] | None:
    try:
        width = int(width_value)
        height = int(height_value)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _reconcile_artifact_state(
    generation: Any,
    request: dict[str, Any],
    *,
    has_image: bool,
    reconciled_at: Any,
) -> bool:
    if request.get(BONUS_BILLING_OBLIGATION_KEY) is not True:
        return False
    target_state = BONUS_ARTIFACT_COMMITTED if has_image else BONUS_ARTIFACT_RECONCILED
    if request.get(BONUS_ARTIFACT_STATE_KEY) == target_state:
        return False
    updated = dict(request)
    updated[BONUS_ARTIFACT_STATE_KEY] = target_state
    if has_image:
        updated.pop(BONUS_ARTIFACT_RECONCILED_AT_KEY, None)
    else:
        updated[BONUS_ARTIFACT_RECONCILED_AT_KEY] = reconciled_at.isoformat()
    generation.upstream_request = updated
    return True


def _request_payload(generation: Any) -> dict[str, Any]:
    request = getattr(generation, "upstream_request", None)
    return request if isinstance(request, dict) else {}


def _terminalize_invalid_dimensions(
    *,
    context: ReconcileContext,
    generation: Any,
    request: dict[str, Any],
    has_image: bool,
    result: ReconcileResult,
) -> bool:
    if request.get(BONUS_BILLING_OBLIGATION_KEY) is not True:
        return False
    state_changed = mark_billing_obligation_unsettleable(
        generation,
        reason="invalid_bonus_billing_dimensions",
        at=context.now,
    )
    artifact_changed = _reconcile_artifact_state(
        generation,
        _request_payload(generation),
        has_image=has_image,
        reconciled_at=context.now,
    )
    if state_changed or artifact_changed:
        result.touched += 1
    reconciliation_rows_total.labels(
        domain="bonus_billing",
        action="unsettleable",
    ).inc()
    context.logger.error(
        "bonus billing terminalized invalid obligation generation_id=%s",
        generation.id,
    )
    return True


def _record_missing_settlement(
    *,
    context: ReconcileContext,
    generation: Any,
    has_image: bool,
    result: ReconcileResult,
) -> None:
    request = _request_payload(generation)
    if not billing_obligation_is_unsettleable(generation):
        reconciliation_rows_total.labels(
            domain="bonus_billing",
            action="settle_missing_ledger",
        ).inc()
        context.logger.error(
            "bonus billing settlement missing ledger generation_id=%s",
            generation.id,
        )
        return
    _reconcile_artifact_state(
        generation,
        request,
        has_image=has_image,
        reconciled_at=context.now,
    )
    result.touched += 1
    reconciliation_rows_total.labels(
        domain="bonus_billing",
        action="unsettleable",
    ).inc()
    context.logger.error(
        "bonus billing reached explicit unsettleable terminal "
        "generation_id=%s reason=%s",
        generation.id,
        request.get(BILLING_OBLIGATION_TERMINAL_REASON_KEY),
    )


class BonusBillingReconciler:
    name = "bonus_billing"

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        completed_settlement = exists(
            select(WalletTransaction.id).where(
                _completed_settlement_condition(
                    user_id=Generation.user_id,
                    ref_id=Generation.id,
                )
            )
        )
        has_image = exists(
            select(Image.id).where(Image.owner_generation_id == Generation.id)
        )
        has_obligation = func.coalesce(
            Generation.upstream_request[BONUS_BILLING_OBLIGATION_KEY].as_boolean(),
            False,
        ).is_(True)
        artifact_pending = (
            Generation.upstream_request[BONUS_ARTIFACT_STATE_KEY].as_string()
            == BONUS_ARTIFACT_PENDING
        )
        billing_unsettleable = (
            func.coalesce(
                Generation.upstream_request[BILLING_OBLIGATION_STATE_KEY].as_string(),
                "",
            )
            == BILLING_OBLIGATION_UNSETTLEABLE
        )
        rows = list(
            (
                await context.session.execute(
                    select(Generation)
                    .where(
                        Generation.status == GenerationStatus.SUCCEEDED.value,
                        Generation.upstream_request["billing_policy"]
                        .as_string()
                        .in_(BONUS_BILLING_POLICIES),
                        func.coalesce(
                            Generation.upstream_request["billing_free"].as_boolean(),
                            False,
                        ).is_(False),
                        ~billing_unsettleable,
                        or_(
                            and_(
                                ~completed_settlement,
                                or_(has_image, has_obligation),
                            ),
                            and_(has_obligation, artifact_pending),
                        ),
                    )
                    .order_by(Generation.finished_at.asc(), Generation.id.asc())
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        result = ReconcileResult()
        for generation in rows:
            request = _request_payload(generation)
            if (
                str(generation.status) != GenerationStatus.SUCCEEDED.value
                or request.get("billing_policy") not in BONUS_BILLING_POLICIES
                or request.get("billing_free") is True
            ):
                continue
            image = (
                await context.session.execute(
                    select(Image)
                    .where(Image.owner_generation_id == generation.id)
                    .order_by(Image.created_at.asc(), Image.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            dimensions = (
                _positive_dimensions(image.width, image.height)
                if image is not None
                else _obligation_dimensions(request)
            )
            if dimensions is None:
                if _terminalize_invalid_dimensions(
                    context=context,
                    generation=generation,
                    request=request,
                    has_image=image is not None,
                    result=result,
                ):
                    continue
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="missing_image",
                ).inc()
                context.logger.error(
                    "bonus billing skipped generation without image generation_id=%s",
                    generation.id,
                )
                continue
            try:
                # A savepoint keeps a malformed bonus row from rolling back
                # repairs already made by the other reconcilers in this run.
                async with context.session.begin_nested():
                    await context.billing.settle_generation(
                        context.session,
                        generation,
                        width=dimensions[0],
                        height=dimensions[1],
                        image_count=1,
                    )
            except Exception:  # noqa: BLE001
                reconciliation_rows_total.labels(
                    domain=self.name,
                    action="settle_failed",
                ).inc()
                context.logger.error(
                    "bonus billing settlement failed generation_id=%s",
                    generation.id,
                    exc_info=True,
                )
                continue
            settlement_id = (
                await context.session.execute(
                    select(WalletTransaction.id)
                    .where(
                        _completed_settlement_condition(
                            user_id=getattr(generation, "user_id", ""),
                            ref_id=generation.id,
                        )
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if settlement_id is None:
                _record_missing_settlement(
                    context=context,
                    generation=generation,
                    has_image=image is not None,
                    result=result,
                )
                continue
            _reconcile_artifact_state(
                generation,
                _request_payload(generation),
                has_image=image is not None,
                reconciled_at=context.now,
            )
            result.touched += 1
            reconciliation_rows_total.labels(
                domain=self.name,
                action="settled",
            ).inc()
        return result


BONUS_BILLING_RECONCILER = BonusBillingReconciler()

__all__ = [
    "BILLING_OBLIGATION_STATE_KEY",
    "BILLING_OBLIGATION_UNSETTLEABLE",
    "BONUS_ARTIFACT_COMMITTED",
    "BONUS_ARTIFACT_PENDING",
    "BONUS_ARTIFACT_RECONCILED",
    "BONUS_ARTIFACT_STATE_KEY",
    "BONUS_BILLING_POLICIES",
    "BONUS_BILLING_RECONCILER",
    "BonusBillingReconciler",
]
