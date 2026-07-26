"""Repair successful bonus generations whose wallet settlement is missing."""

from __future__ import annotations

from sqlalchemy import exists, select

from lumen_core.constants import GenerationStatus
from lumen_core.models import Generation, Image, WalletTransaction

from .contracts import ReconcileContext, ReconcileResult
from .metrics import reconciliation_rows_total

BONUS_BILLING_POLICIES = (
    "dual_race_loser_settled_separately",
    "batch_extra_settled_separately",
)


class BonusBillingReconciler:
    name = "bonus_billing"

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        consumed = exists(
            select(WalletTransaction.id).where(
                WalletTransaction.user_id == Generation.user_id,
                WalletTransaction.ref_type == "generation",
                WalletTransaction.ref_id == Generation.id,
                WalletTransaction.kind.in_(("settle", "release")),
            )
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
                        ~consumed,
                    )
                    .order_by(Generation.finished_at.asc(), Generation.id.asc())
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        result = ReconcileResult()
        for generation in rows:
            request = (
                generation.upstream_request
                if isinstance(generation.upstream_request, dict)
                else {}
            )
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
            if image is None:
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
                        width=int(image.width),
                        height=int(image.height),
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
            result.touched += 1
            reconciliation_rows_total.labels(
                domain=self.name,
                action="settled",
            ).inc()
        return result


BONUS_BILLING_RECONCILER = BonusBillingReconciler()

__all__ = [
    "BONUS_BILLING_POLICIES",
    "BONUS_BILLING_RECONCILER",
    "BonusBillingReconciler",
]
