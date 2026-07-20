"""Billing Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..billing_schemas import MoneyOut, WalletOut
from .common import BaseOut

# ---------- Admin / Usage / Share (V1.0 收尾) ----------


class AllowedEmailOut(BaseOut):
    id: str
    email: str
    invited_by_email: str | None
    created_at: datetime


class AdminUserOut(BaseOut):
    id: str
    email: str
    role: str
    account_mode: Literal["wallet", "byok"] = "wallet"
    display_name: str | None
    created_at: datetime
    generations_count: int
    completions_count: int
    messages_count: int


# ---------- Billing / Wallet ----------


class WalletTransactionOut(BaseOut):
    id: str
    kind: str
    amount: MoneyOut
    balance_after: MoneyOut
    hold_after: MoneyOut
    ref_type: str | None = None
    ref_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    created_by_admin: str | None = None


class WalletTransactionListOut(BaseModel):
    items: list[WalletTransactionOut]
    next_cursor: str | None = None


class BillingWindowOut(BaseModel):
    used_micro: int
    limit_micro: int
    resets_at: datetime | None = None


class BillingUsageByKindOut(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    image: int = 0
    reasoning: int = 0


class BillingSnapshotOut(BaseModel):
    balance_micro: int
    billing_rate_multiplier: str
    credential_id: str | None = None
    windows: dict[str, BillingWindowOut]
    by_kind_30d: BillingUsageByKindOut


class AdminBillingUsageOut(BaseModel):
    user_id: str
    balance_micro: int
    billing_rate_multiplier: str
    credential_id: str | None = None
    range_start: datetime
    range_end: datetime
    windows: dict[str, BillingWindowOut]
    by_kind_30d: BillingUsageByKindOut
    total_micro: int = 0
    transaction_count: int = 0


class AdminPricingBulkRatesIn(BaseModel):
    input: str | int | float | None = None
    output: str | int | float | None = None
    cache_read: str | int | float | None = None
    cache_creation: str | int | float | None = None
    cache_creation_5m: str | int | float | None = None
    cache_creation_1h: str | int | float | None = None
    image_output: str | int | float | None = None
    reasoning: str | int | float | None = None
    input_priority: str | int | float | None = None
    output_priority: str | int | float | None = None
    cache_read_priority: str | int | float | None = None
    long_context_threshold: int | None = None
    long_context_input_multiplier: float | None = None
    long_context_output_multiplier: float | None = None


class AdminPricingBulkIn(BaseModel):
    model: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, max_length=32)
    rates: AdminPricingBulkRatesIn
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


PricingUnit = Literal[
    "per_image",
    "per_1k_tokens_in",
    "per_1k_tokens_out",
    "per_1k_tokens_cache_read",
    "per_1k_tokens_cache_creation",
    "per_1k_tokens_cache_creation_5m",
    "per_1k_tokens_cache_creation_1h",
    "per_1k_tokens_image_output",
    "per_1k_tokens_reasoning",
    "per_1k_tokens_input_priority",
    "per_1k_tokens_output_priority",
    "per_1k_tokens_cache_read_priority",
    "long_context_threshold",
    "long_context_input_multiplier",
    "long_context_output_multiplier",
    "per_mtoken",
]


class PricingRuleOut(BaseOut):
    id: str
    scope: Literal["image_size", "chat_model", "video"]
    key: str
    variant: str = "default"
    unit: PricingUnit
    price: MoneyOut
    priority: int = 0
    enabled: bool
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class PricingRulesOut(BaseModel):
    items: list[PricingRuleOut]
    image_size_thresholds: dict[str, int] | None = None
    billing_enabled: bool | None = None
    show_estimate_in_composer: bool | None = None


class PricingRuleUpsertIn(BaseModel):
    scope: Literal["image_size", "chat_model", "video"]
    key: str = Field(min_length=1, max_length=64)
    variant: str = Field(default="default", min_length=1, max_length=32)
    unit: PricingUnit
    price_rmb: str = Field(min_length=1, max_length=32)
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


class PricingRulesUpdateIn(BaseModel):
    items: list[PricingRuleUpsertIn] = Field(min_length=1, max_length=500)
    image_size_thresholds: dict[str, int] | None = None
    force: bool = False


class PricingImportIn(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    rate: float = Field(default=1.0, gt=0, le=100)


class RedemptionIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)


class RedemptionOut(BaseModel):
    amount: MoneyOut
    balance: MoneyOut


class RedemptionUsageOut(BaseOut):
    id: str
    code_id: str
    amount: MoneyOut
    redeemed_at: datetime


class RedemptionUsageListOut(BaseModel):
    items: list[RedemptionUsageOut]
    next_cursor: str | None = None


class AdminRedemptionCodeOut(BaseOut):
    id: str
    code_prefix: str
    amount: MoneyOut
    max_redemptions: int
    redeemed_count: int
    usable_count: int = 0
    status: Literal["active", "revoked", "expired", "exhausted"] = "active"
    batch_id: str | None = None
    note: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AdminRedemptionCodeListOut(BaseModel):
    items: list[AdminRedemptionCodeOut]
    next_cursor: str | None = None


class AdminRedemptionUsageOut(BaseOut):
    id: str
    code_id: str
    user_id: str
    user_email: str | None = None
    amount: MoneyOut
    wallet_tx_id: str
    redeemed_at: datetime
    ip_hash: str | None = None


class AdminRedemptionUsageListOut(BaseModel):
    items: list[AdminRedemptionUsageOut]
    next_cursor: str | None = None


class AdminRedemptionCodeCreateIn(BaseModel):
    amount_rmb: str = Field(min_length=1, max_length=32)
    count: int = Field(default=1, ge=1, le=1000)
    max_redemptions: int = Field(default=1, ge=1, le=1000)
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def _expires_at_must_be_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        from datetime import timezone as _tz

        now = datetime.now(_tz.utc)
        # Require at least 1 minute of validity so the batch isn't dead-on-arrival.
        if value.tzinfo is None:
            value = value.replace(tzinfo=_tz.utc)
        if (value - now).total_seconds() < 60:
            raise ValueError("expires_at must be at least 1 minute in the future")
        return value


class AdminRedemptionCodeCreateOut(BaseModel):
    batch_id: str
    count: int
    amount: MoneyOut
    download_token: str
    plaintext_codes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class AdminWalletOut(BaseModel):
    user_id: str
    email: str
    account_mode: Literal["wallet", "byok"]
    wallet: WalletOut
    last_topup_at: datetime | None = None
    last_charge_at: datetime | None = None


class AdminWalletListOut(BaseModel):
    items: list[AdminWalletOut]
    next_cursor: str | None = None


class AdminWalletDetailOut(AdminWalletOut):
    last_redemption_at: datetime | None = None
    transactions: list[WalletTransactionOut] = Field(default_factory=list)
    redemptions: list[AdminRedemptionUsageOut] = Field(default_factory=list)


class AdminBillingAuditEventOut(BaseOut):
    id: str
    event_type: str
    user_id: str | None = None
    target_user_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdminBillingOverviewOut(BaseModel):
    billing_enabled: bool
    redemption_secret_configured: bool
    bootstrap_completed: bool
    wallet_total_balance: MoneyOut
    active_holds_count: int
    active_holds: MoneyOut
    codes_active: int
    codes_redeemed_24h: int
    codes_redeemed_24h_amount: MoneyOut
    charges_24h: MoneyOut
    thresholds_pricing_aligned: bool
    thresholds_missing_prices: list[str] = Field(default_factory=list)
    recent_audit_events: list[AdminBillingAuditEventOut] = Field(default_factory=list)


class AdminWalletAuditOut(BaseModel):
    ok: bool
    transactions: int
    users: int
    mismatch_count: int
    mismatches: list[str] = Field(default_factory=list)


class AdminOrphanHoldOut(BaseModel):
    tx: WalletTransactionOut
    user_id: str
    age_seconds: int


class AdminBillingBootstrapIn(BaseModel):
    redemption_code_secret: str | None = Field(
        default=None, min_length=16, max_length=2048
    )
    enabled: bool = True
    usd_to_rmb_rate: float = Field(default=1.0, gt=0, le=100)
    low_balance_warn_rmb: str = Field(default="2")
    image_size_thresholds: dict[str, int] = Field(
        default_factory=lambda: {"1k": 1_572_864, "2k": 3_686_400, "4k": 8_294_400}
    )
    image_prices_rmb: dict[str, str] = Field(
        default_factory=lambda: {"1k": "0.2", "2k": "0.4", "4k": "0.8"}
    )


class AdminRedemptionBatchRedownloadOut(BaseModel):
    batch_id: str
    count: int
    download_token: str
    plaintext_codes: list[str] = Field(default_factory=list)
    expires_in_seconds: int = 300


class AdminWalletAdjustIn(BaseModel):
    amount_rmb_signed: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=500)


class AdminSetAccountModeIn(BaseModel):
    mode: Literal["wallet", "byok"]
    on_residual_balance: Literal["freeze", "zero"] = "freeze"


class UsageOut(BaseModel):
    range_start: datetime
    range_end: datetime
    messages_count: int
    generations_count: int
    generations_succeeded: int
    generations_failed: int
    completions_count: int
    completions_succeeded: int
    completions_failed: int
    total_pixels_generated: int
    total_tokens_in: int
    total_tokens_out: int
    storage_bytes: int


class ShareOut(BaseOut):
    id: str
    image_id: str
    image_ids: list[str] = Field(default_factory=list)
    token: str
    url: str
    image_url: str
    show_prompt: bool
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class PublicShareImageOut(BaseModel):
    id: str
    image_url: str
    display_url: str | None = None
    preview_url: str | None = None
    thumb_url: str | None = None
    width: int
    height: int
    mime: str
    prompt: str | None = None


class PublicShareOut(BaseModel):
    token: str
    image_url: str
    images: list[PublicShareImageOut] = Field(default_factory=list)
    width: int
    height: int
    mime: str
    show_prompt: bool
    prompt: str | None
    created_at: datetime
    expires_at: datetime | None


__all__ = [
    "AllowedEmailOut",
    "AdminUserOut",
    "WalletTransactionOut",
    "WalletTransactionListOut",
    "BillingWindowOut",
    "BillingUsageByKindOut",
    "BillingSnapshotOut",
    "AdminBillingUsageOut",
    "AdminPricingBulkRatesIn",
    "AdminPricingBulkIn",
    "PricingUnit",
    "PricingRuleOut",
    "PricingRulesOut",
    "PricingRuleUpsertIn",
    "PricingRulesUpdateIn",
    "PricingImportIn",
    "RedemptionIn",
    "RedemptionOut",
    "RedemptionUsageOut",
    "RedemptionUsageListOut",
    "AdminRedemptionCodeOut",
    "AdminRedemptionCodeListOut",
    "AdminRedemptionUsageOut",
    "AdminRedemptionUsageListOut",
    "AdminRedemptionCodeCreateIn",
    "AdminRedemptionCodeCreateOut",
    "AdminWalletOut",
    "AdminWalletListOut",
    "AdminWalletDetailOut",
    "AdminBillingAuditEventOut",
    "AdminBillingOverviewOut",
    "AdminWalletAuditOut",
    "AdminOrphanHoldOut",
    "AdminBillingBootstrapIn",
    "AdminRedemptionBatchRedownloadOut",
    "AdminWalletAdjustIn",
    "AdminSetAccountModeIn",
    "UsageOut",
    "ShareOut",
    "PublicShareImageOut",
    "PublicShareOut",
]
