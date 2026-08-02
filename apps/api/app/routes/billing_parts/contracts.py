"""Typed dependency contracts consumed by billing route modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...ratelimit import RateLimiter


SyncOperation = Callable[..., Any]
AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class BillingQueries:
    """Read-only and validation operations needed by billing HTTP routes."""

    http: SyncOperation
    billing_http: SyncOperation
    money: SyncOperation
    escape_like_pattern: SyncOperation
    generate_redemption_secret: SyncOperation
    get_spec: SyncOperation
    setting_raw: AsyncOperation
    get_setting: AsyncOperation
    low_balance_threshold: AsyncOperation
    allow_negative_balance: AsyncOperation
    image_thresholds: AsyncOperation
    billing_enabled_setting: AsyncOperation
    bootstrap_completed_setting: AsyncOperation
    wallet_out: AsyncOperation
    tx_out: SyncOperation
    redemption_code_out: SyncOperation
    audit_out: SyncOperation
    cursor_filter: SyncOperation
    next_cursor: SyncOperation
    cached_redemption_out: AsyncOperation
    redemption_out_for_usage: AsyncOperation
    redemption_secret: AsyncOperation
    redemption_secrets: AsyncOperation
    load_redemption_plaintext_batch: AsyncOperation
    redemption_batch_for_idempotency: AsyncOperation
    replay_redemption_batch: AsyncOperation
    billing_audit_predicate: SyncOperation
    wallet_audit_ledger: SyncOperation
    threshold_price_alignment: AsyncOperation
    billing_snapshot_parts: AsyncOperation
    usage_total: SyncOperation
    pricing_rule_out: SyncOperation
    rmb_to_micro_or_422: SyncOperation
    validate_enabled_pricing_value: SyncOperation
    validate_thresholds_have_prices: AsyncOperation
    bulk_numeric_micro: SyncOperation
    bulk_multiplier_x10000: SyncOperation
    parse_price_rows: SyncOperation
    openai_price_micro: SyncOperation
    redemption_request_hash: SyncOperation
    redemption_idempotency_key: SyncOperation
    redemption_usage_id: SyncOperation
    redemption_batch_request_hash: SyncOperation
    redemption_batch_idempotency_key: SyncOperation
    redemption_batch_lock_identity: SyncOperation
    integrity_constraint_name: SyncOperation
    require_redemption_download_batch: SyncOperation


@dataclass(frozen=True, slots=True)
class BillingCommands:
    """State-changing operations needed by billing HTTP routes."""

    require_wallet_user: SyncOperation
    require_bootstrap_completed: AsyncOperation
    require_redemption_operational: AsyncOperation
    lock_redemption_idempotency_key: AsyncOperation
    lock_redemption_batch_idempotency_key: AsyncOperation
    cache_redemption_out: AsyncOperation
    store_redemption_plaintext_batch: AsyncOperation
    invalidate_balance_cache: AsyncOperation
    align_pricing_group_priorities: AsyncOperation
    invalidate_pricing_cache: AsyncOperation
    lock_redemption_secret_rotation: AsyncOperation
    update_settings: AsyncOperation
    write_audit: AsyncOperation
    request_ip_hash: SyncOperation
    remember_previous_redemption_secret: AsyncOperation
    get_redis: SyncOperation


@dataclass(frozen=True, slots=True)
class BillingServices:
    """Complete, explicit dependency bundle for billing route composition."""

    queries: BillingQueries
    commands: BillingCommands
    redemption_limiter: RateLimiter
    max_admin_adjust_micro: int
    max_admin_negative_balance_micro: int
    charge_kinds: tuple[str, ...]
    bulk_rate_units: Mapping[str, str]
    redemption_download_ttl_seconds: int
    download_token_prefix: str
    plaintext_batch_prefix: str
    redemption_replay_constraints: frozenset[str] | set[str] | tuple[str, ...]
    redemption_already_used_constraint: str
    redemption_batch_idempotency_constraint: str
