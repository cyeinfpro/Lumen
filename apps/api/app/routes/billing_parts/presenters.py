"""Pure billing response presenters and cursor helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from lumen_core.model_entities import (
    AuditLog,
    PricingRule,
    RedemptionCode,
    WalletTransaction,
)
from lumen_core.schema_models import (
    AdminBillingAuditEventOut,
    AdminRedemptionCodeOut,
    PricingRuleOut,
    WalletTransactionOut,
)

from ...services.billing.errors import http_error
from ...services.billing.redemption_values import redemption_status
from ...services.billing.wallet_activity import money_out


def pricing_rule_out(rule: PricingRule) -> PricingRuleOut:
    return PricingRuleOut(
        id=rule.id,
        scope=rule.scope,  # type: ignore[arg-type]
        key=rule.key,
        variant=rule.variant,
        unit=rule.unit,  # type: ignore[arg-type]
        price=money_out(rule.price_micro),
        priority=rule.priority,
        enabled=rule.enabled,
        note=rule.note,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def transaction_out(tx: WalletTransaction) -> WalletTransactionOut:
    return WalletTransactionOut(
        id=tx.id,
        kind=tx.kind,
        amount=money_out(tx.amount_micro),
        balance_after=money_out(tx.balance_after),
        hold_after=money_out(tx.hold_after),
        ref_type=tx.ref_type,
        ref_id=tx.ref_id,
        meta=tx.meta or {},
        created_at=tx.created_at,
        created_by_admin=tx.created_by_admin,
    )


def redemption_code_out(
    code: RedemptionCode, *, now: datetime | None = None
) -> AdminRedemptionCodeOut:
    usable_count = max(0, int(code.max_redemptions) - int(code.redeemed_count))
    return AdminRedemptionCodeOut(
        id=code.id,
        code_prefix=code.code_prefix,
        amount=money_out(code.amount_micro),
        max_redemptions=code.max_redemptions,
        redeemed_count=code.redeemed_count,
        usable_count=usable_count,
        status=redemption_status(code, now=now),  # type: ignore[arg-type]
        batch_id=code.batch_id,
        note=code.note,
        expires_at=code.expires_at,
        revoked_at=code.revoked_at,
        created_by=code.created_by,
        created_at=code.created_at,
        updated_at=code.updated_at,
    )


def audit_out(row: AuditLog) -> AdminBillingAuditEventOut:
    return AdminBillingAuditEventOut(
        id=row.id,
        event_type=row.event_type,
        user_id=row.user_id,
        target_user_id=row.target_user_id,
        details=row.details or {},
        created_at=row.created_at,
    )


def cursor_filter(
    stmt: Any,
    model: Any,
    cursor: str | None,
    *,
    attr: str = "created_at",
) -> Any:
    if not cursor:
        return stmt
    try:
        ts_raw, row_id = cursor.split("|", 1)
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        raise http_error("invalid_cursor", "cursor is invalid", 422)
    timestamp = getattr(model, attr)
    return stmt.where((timestamp < ts) | ((timestamp == ts) & (model.id < row_id)))


def next_cursor(
    rows: Sequence[Any], has_more: bool, attr: str = "created_at"
) -> str | None:
    if not has_more or not rows:
        return None
    last = rows[-1]
    ts = getattr(last, attr)
    return f"{ts.isoformat()}|{last.id}"
