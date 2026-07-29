# ruff: noqa: F401
"""Billing router composition and historical compatibility exports."""

from __future__ import annotations

from fastapi import APIRouter

from lumen_core import billing as billing_core
from lumen_core.models import (
    AuditLog,
    PricingRule,
    RedemptionBatch,
    RedemptionCode,
    RedemptionCodeUsage,
    User,
    UserApiCredential,
    UserWallet,
    WalletTransaction,
    new_uuid7,
)
from lumen_core.schemas import (
    AdminBillingAuditEventOut,
    AdminBillingBootstrapIn,
    AdminBillingOverviewOut,
    AdminBillingUsageOut,
    AdminOrphanHoldOut,
    AdminPricingBulkIn,
    AdminRedemptionBatchRedownloadOut,
    AdminRedemptionCodeCreateIn,
    AdminRedemptionCodeCreateOut,
    AdminRedemptionCodeListOut,
    AdminRedemptionCodeOut,
    AdminRedemptionUsageListOut,
    AdminRedemptionUsageOut,
    AdminSetAccountModeIn,
    AdminWalletAdjustIn,
    AdminWalletAuditOut,
    AdminWalletDetailOut,
    AdminWalletListOut,
    AdminWalletOut,
    BillingSnapshotOut,
    BillingUsageByKindOut,
    BillingWindowOut,
    PricingImportIn,
    PricingRuleOut,
    PricingRulesOut,
    PricingRulesUpdateIn,
    RedemptionIn,
    RedemptionOut,
    RedemptionUsageListOut,
    RedemptionUsageOut,
    WalletOut,
    WalletTransactionListOut,
    WalletTransactionOut,
)

from ..audit import request_ip_hash, write_audit
from ..redis_client import get_redis
from ..runtime_settings import get_setting, update_settings
from ..services.billing_cache import BillingCacheService
from ..services.billing.errors import http_error as _http
from ..services.billing.pricing_values import (
    ZERO_PRICE_ALLOWED_UNITS as _ZERO_PRICE_ALLOWED_UNITS,
)
from ..services.billing.usage import meta_int as _meta_int
from ..services.pricing_cache import (
    invalidate_pricing_cache as _invalidate_pricing_cache,
)
from ..services.redemption_secret import (
    previous_redemption_secret,
    remember_previous_redemption_secret,
)
from .billing_parts import overview as _billing_overview_routes
from .billing_parts import pricing as _billing_pricing_routes
from .billing_parts import redemptions as _billing_redemption_routes
from .billing_parts import wallets as _billing_wallet_routes
from .billing_parts.composition import (
    build_billing_services,
    replace_billing_commands,
    replace_billing_queries,
    replace_billing_services,
)
from .billing_parts.contracts import BillingCommands, BillingQueries, BillingServices
from .billing_parts.services import (
    MAX_ADMIN_ADJUST_MICRO,
    MAX_ADMIN_NEGATIVE_BALANCE_MICRO,
    REDEMPTION_LIMITER,
    BILLING_AUDIT_EVENT_PREFIXES as _BILLING_AUDIT_EVENT_PREFIXES,
    BILLING_WINDOWS as _BILLING_WINDOWS,
    BULK_RATE_UNITS as _BULK_RATE_UNITS,
    CHARGE_KINDS as _CHARGE_KINDS,
    DOWNLOAD_TOKEN_PREFIX as _DOWNLOAD_TOKEN_PREFIX,
    PLAINTEXT_BATCH_PREFIX as _PLAINTEXT_BATCH_PREFIX,
    REDEMPTION_ALREADY_USED_CONSTRAINT as _REDEMPTION_ALREADY_USED_CONSTRAINT,
    REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT as _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT,
    REDEMPTION_DOWNLOAD_TTL_SECONDS as _REDEMPTION_DOWNLOAD_TTL_SECONDS,
    REDEMPTION_IDEMPOTENCY_NAMESPACE as _REDEMPTION_IDEMPOTENCY_NAMESPACE,
    REDEMPTION_IDEMPOTENCY_TTL_SECONDS as _REDEMPTION_IDEMPOTENCY_TTL_SECONDS,
    REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE as _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE,
    REDEMPTION_KNOWN_CONSTRAINTS as _REDEMPTION_KNOWN_CONSTRAINTS,
    REDEMPTION_REPLAY_CONSTRAINTS as _REDEMPTION_REPLAY_CONSTRAINTS,
    active_credential_window_config as _active_credential_window_config,
    align_pricing_group_priorities as _align_pricing_group_priorities,
    allow_negative_balance as _allow_negative_balance,
    audit_out as _audit_out,
    billing_audit_predicate as _billing_audit_predicate,
    billing_balance_micro as _billing_balance_micro,
    billing_cache as _billing_cache,
    billing_enabled_setting as _billing_enabled_setting,
    billing_http as _billing_http,
    billing_rows_for_range as _billing_rows_for_range,
    billing_snapshot_parts as _billing_snapshot_parts,
    bootstrap_completed_setting as _bootstrap_completed_setting,
    bulk_multiplier_x10000 as _bulk_multiplier_x10000,
    bulk_numeric_micro as _bulk_numeric_micro,
    cache_redemption_out as _cache_redemption_out,
    cached_redemption_out as _cached_redemption_out,
    client_idempotency_key as _client_idempotency_key,
    credential_windows as _credential_windows,
    cursor_filter as _cursor_filter,
    escape_like_pattern as _escape_like_pattern,
    generate_redemption_secret as _generate_redemption_secret,
    image_thresholds as _image_thresholds,
    integrity_constraint_name as _integrity_constraint_name,
    invalidate_balance_cache as _invalidate_balance_cache,
    load_redemption_plaintext_batch as _load_redemption_plaintext_batch,
    lock_redemption_batch_idempotency_key as _lock_redemption_batch_idempotency_key,
    lock_redemption_idempotency_key as _lock_redemption_idempotency_key,
    low_balance_threshold as _low_balance_threshold,
    money as _money,
    next_cursor as _next_cursor,
    openai_price_micro as _openai_price_micro,
    parse_price_rows as _parse_price_rows,
    pricing_group_priorities as _pricing_group_priorities,
    pricing_rule_out as _pricing_rule_out,
    redemption_batch_for_idempotency as _redemption_batch_for_idempotency,
    redemption_batch_idempotency_key as _redemption_batch_idempotency_key,
    redemption_batch_lock_identity as _redemption_batch_lock_identity,
    redemption_batch_payload_matches as _redemption_batch_payload_matches,
    redemption_batch_request_hash as _redemption_batch_request_hash,
    redemption_code_out as _redemption_code_out,
    redemption_csv_batch_id as _redemption_csv_batch_id,
    redemption_csv_payload as _redemption_csv_payload,
    redemption_idempotency_cache_key as _redemption_idempotency_cache_key,
    redemption_idempotency_key as _redemption_idempotency_key,
    redemption_out_for_usage as _redemption_out_for_usage,
    redemption_plaintext_payload as _redemption_plaintext_payload,
    redemption_request_hash as _redemption_request_hash,
    redemption_secret as _redemption_secret,
    redemption_secrets as _redemption_secrets,
    redemption_status as _redemption_status,
    redemption_usage_id as _redemption_usage_id,
    replay_redemption_batch as _replay_redemption_batch,
    require_bootstrap_completed as _require_bootstrap_completed,
    require_redemption_download_batch as _require_redemption_download_batch,
    require_redemption_operational as _require_redemption_operational,
    require_wallet_user as _require_wallet_user,
    rmb_to_micro_or_422 as _rmb_to_micro_or_422,
    scaled_meta_cost as _scaled_meta_cost,
    setting_raw as _setting_raw,
    store_redemption_plaintext_batch as _store_redemption_plaintext_batch,
    threshold_price_alignment as _threshold_price_alignment,
    tx_out as _tx_out,
    usage_by_kind as _usage_by_kind,
    usage_total as _usage_total,
    validate_enabled_pricing_value as _validate_enabled_pricing_value,
    validate_thresholds_have_prices as _validate_thresholds_have_prices,
    wallet_activity_24h as _wallet_activity_24h,
    wallet_activity_window_end as _wallet_activity_window_end,
    wallet_audit_ledger as _wallet_audit_ledger,
    wallet_out as _wallet_out,
    configure_billing_cache,
)


router = APIRouter(tags=["billing"])
router.include_router(_billing_wallet_routes.router)
router.include_router(_billing_overview_routes.router)
router.include_router(_billing_redemption_routes.router)
router.include_router(_billing_pricing_routes.router)

get_my_wallet = _billing_wallet_routes.get_my_wallet
get_my_billing_snapshot = _billing_wallet_routes.get_my_billing_snapshot
list_my_wallet_transactions = _billing_wallet_routes.list_my_wallet_transactions
admin_list_wallets = _billing_wallet_routes.admin_list_wallets
admin_adjust_wallet = _billing_wallet_routes.admin_adjust_wallet
admin_get_wallet_detail = _billing_wallet_routes.admin_get_wallet_detail
admin_set_account_mode = _billing_wallet_routes.admin_set_account_mode
admin_list_wallet_transactions = _billing_wallet_routes.admin_list_wallet_transactions

admin_billing_audit = _billing_overview_routes.admin_billing_audit
admin_billing_overview = _billing_overview_routes.admin_billing_overview
admin_billing_usage = _billing_overview_routes.admin_billing_usage
admin_wallet_audit = _billing_overview_routes.admin_wallet_audit
admin_list_orphan_holds = _billing_overview_routes.admin_list_orphan_holds
admin_release_orphan_hold = _billing_overview_routes.admin_release_orphan_hold
admin_billing_bootstrap = _billing_overview_routes.admin_billing_bootstrap
admin_rotate_redemption_secret = _billing_overview_routes.admin_rotate_redemption_secret

redeem_code = _billing_redemption_routes.redeem_code
list_my_redemptions = _billing_redemption_routes.list_my_redemptions
admin_list_redemption_codes = _billing_redemption_routes.admin_list_redemption_codes
admin_list_redemption_code_usage = (
    _billing_redemption_routes.admin_list_redemption_code_usage
)
admin_create_redemption_codes = _billing_redemption_routes.admin_create_redemption_codes
admin_download_redemption_batch_csv = (
    _billing_redemption_routes.admin_download_redemption_batch_csv
)
admin_download_redemption_batch_txt = (
    _billing_redemption_routes.admin_download_redemption_batch_txt
)
admin_redownload_redemption_batch = (
    _billing_redemption_routes.admin_redownload_redemption_batch
)
admin_revoke_redemption_code = _billing_redemption_routes.admin_revoke_redemption_code
admin_revoke_redemption_batch = _billing_redemption_routes.admin_revoke_redemption_batch

get_my_pricing = _billing_pricing_routes.get_my_pricing
admin_list_pricing = _billing_pricing_routes.admin_list_pricing
admin_list_billing_pricing = _billing_pricing_routes.admin_list_billing_pricing
admin_update_pricing = _billing_pricing_routes.admin_update_pricing
admin_bulk_pricing = _billing_pricing_routes.admin_bulk_pricing
admin_import_openai_pricing = _billing_pricing_routes.admin_import_openai_pricing
