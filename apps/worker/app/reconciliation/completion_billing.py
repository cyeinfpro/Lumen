"""Reconcile terminal completions whose wallet reservation remains pending."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from lumen_core.constants import CompletionStatus
from lumen_core.model_entities.tasks import Completion

from ..billing_parts.helpers import (
    COMPLETION_BILLING_PENDING,
    COMPLETION_BILLING_STATE_KEY,
    completion_billing_pending,
)
from .contracts import ReconcileContext, ReconcileResult
from .metrics import reconciliation_rows_total

RECON_BATCH_LIMIT = 100
_TERMINAL_STATUSES = (
    CompletionStatus.SUCCEEDED.value,
    CompletionStatus.FAILED.value,
    CompletionStatus.CANCELED.value,
)


class CompletionBillingReconciler:
    name = "completion_billing"

    @staticmethod
    def _eligible(completion: Completion) -> bool:
        return str(
            completion.status
        ) in _TERMINAL_STATUSES and completion_billing_pending(completion)

    def _candidate_query(self) -> Any:
        billing_state = Completion.upstream_request[
            COMPLETION_BILLING_STATE_KEY
        ].as_string()
        return (
            select(Completion)
            .where(
                Completion.status.in_(_TERMINAL_STATUSES),
                billing_state == COMPLETION_BILLING_PENDING,
            )
            .order_by(Completion.updated_at.asc(), Completion.id.asc())
            .limit(RECON_BATCH_LIMIT)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        rows = list((await context.session.execute(self._candidate_query())).scalars())
        result = ReconcileResult()
        for completion in rows:
            if not self._eligible(completion):
                continue
            resolved = await context.billing.reconcile_completion_billing(
                context.session,
                completion,
            )
            result.touched += 1
            reconciliation_rows_total.labels(
                domain=self.name,
                action="resolved" if resolved else "pending",
            ).inc()
        return result


COMPLETION_BILLING_RECONCILER = CompletionBillingReconciler()


__all__ = (
    "COMPLETION_BILLING_RECONCILER",
    "CompletionBillingReconciler",
)
