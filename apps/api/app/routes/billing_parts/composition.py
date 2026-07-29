"""Explicit billing route dependency composition."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...runtime_settings import get_setting, update_settings
from ...services.pricing_cache import invalidate_pricing_cache
from ...services.redemption_secret import remember_previous_redemption_secret
from ...audit import request_ip_hash, write_audit
from ...redis_client import get_redis
from .contracts import BillingCommands, BillingQueries, BillingServices
from . import services
from lumen_core.runtime_settings import get_spec


def build_billing_services() -> BillingServices:
    """Build a typed dependency bundle without module-level mutable runtime state."""

    return BillingServices(
        queries=BillingQueries(
            http=services._http,
            billing_http=services._billing_http,
            money=services._money,
            escape_like_pattern=services._escape_like_pattern,
            generate_redemption_secret=services._generate_redemption_secret,
            get_spec=get_spec,
            setting_raw=services._setting_raw,
            get_setting=get_setting,
            low_balance_threshold=services._low_balance_threshold,
            allow_negative_balance=services._allow_negative_balance,
            image_thresholds=services._image_thresholds,
            billing_enabled_setting=services._billing_enabled_setting,
            bootstrap_completed_setting=services._bootstrap_completed_setting,
            wallet_out=services._wallet_out,
            tx_out=services._tx_out,
            redemption_code_out=services._redemption_code_out,
            audit_out=services._audit_out,
            cursor_filter=services._cursor_filter,
            next_cursor=services._next_cursor,
            cached_redemption_out=services._cached_redemption_out,
            redemption_out_for_usage=services._redemption_out_for_usage,
            redemption_secret=services._redemption_secret,
            redemption_secrets=services._redemption_secrets,
            load_redemption_plaintext_batch=services._load_redemption_plaintext_batch,
            redemption_batch_for_idempotency=services._redemption_batch_for_idempotency,
            replay_redemption_batch=services._replay_redemption_batch,
            billing_audit_predicate=services._billing_audit_predicate,
            wallet_audit_ledger=services._wallet_audit_ledger,
            threshold_price_alignment=services._threshold_price_alignment,
            billing_snapshot_parts=services._billing_snapshot_parts,
            usage_total=services._usage_total,
            pricing_rule_out=services._pricing_rule_out,
            rmb_to_micro_or_422=services._rmb_to_micro_or_422,
            validate_enabled_pricing_value=services._validate_enabled_pricing_value,
            validate_thresholds_have_prices=services._validate_thresholds_have_prices,
            bulk_numeric_micro=services._bulk_numeric_micro,
            bulk_multiplier_x10000=services._bulk_multiplier_x10000,
            parse_price_rows=services._parse_price_rows,
            openai_price_micro=services._openai_price_micro,
            redemption_request_hash=services._redemption_request_hash,
            redemption_idempotency_key=services._redemption_idempotency_key,
            redemption_usage_id=services._redemption_usage_id,
            redemption_batch_request_hash=services._redemption_batch_request_hash,
            redemption_batch_idempotency_key=services._redemption_batch_idempotency_key,
            redemption_batch_lock_identity=services._redemption_batch_lock_identity,
            integrity_constraint_name=services._integrity_constraint_name,
            require_redemption_download_batch=services._require_redemption_download_batch,
        ),
        commands=BillingCommands(
            require_wallet_user=services._require_wallet_user,
            require_bootstrap_completed=services._require_bootstrap_completed,
            require_redemption_operational=services._require_redemption_operational,
            lock_redemption_idempotency_key=services._lock_redemption_idempotency_key,
            lock_redemption_batch_idempotency_key=(
                services._lock_redemption_batch_idempotency_key
            ),
            cache_redemption_out=services._cache_redemption_out,
            store_redemption_plaintext_batch=services._store_redemption_plaintext_batch,
            invalidate_balance_cache=services._invalidate_balance_cache,
            align_pricing_group_priorities=services._align_pricing_group_priorities,
            invalidate_pricing_cache=invalidate_pricing_cache,
            update_settings=update_settings,
            write_audit=write_audit,
            request_ip_hash=request_ip_hash,
            remember_previous_redemption_secret=remember_previous_redemption_secret,
            get_redis=get_redis,
        ),
        redemption_limiter=services.REDEMPTION_LIMITER,
        max_admin_adjust_micro=services.MAX_ADMIN_ADJUST_MICRO,
        max_admin_negative_balance_micro=services.MAX_ADMIN_NEGATIVE_BALANCE_MICRO,
        charge_kinds=tuple(services._CHARGE_KINDS),
        bulk_rate_units=services._BULK_RATE_UNITS,
        redemption_download_ttl_seconds=services._REDEMPTION_DOWNLOAD_TTL_SECONDS,
        download_token_prefix=services._DOWNLOAD_TOKEN_PREFIX,
        plaintext_batch_prefix=services._PLAINTEXT_BATCH_PREFIX,
        redemption_replay_constraints=services._REDEMPTION_REPLAY_CONSTRAINTS,
        redemption_already_used_constraint=(
            services._REDEMPTION_ALREADY_USED_CONSTRAINT
        ),
        redemption_batch_idempotency_constraint=(
            services._REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT
        ),
    )


def replace_billing_queries(
    billing_services: BillingServices,
    **changes: Any,
) -> BillingServices:
    """Return a typed test bundle with selected query operations replaced."""

    return replace(
        billing_services,
        queries=replace(billing_services.queries, **changes),
    )


def replace_billing_commands(
    billing_services: BillingServices,
    **changes: Any,
) -> BillingServices:
    """Return a typed test bundle with selected command operations replaced."""

    return replace(
        billing_services,
        commands=replace(billing_services.commands, **changes),
    )


def replace_billing_services(
    billing_services: BillingServices,
    **changes: Any,
) -> BillingServices:
    """Return a typed test bundle with selected top-level services replaced."""

    return replace(billing_services, **changes)
