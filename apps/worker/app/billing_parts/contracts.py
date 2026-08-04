"""Explicit dependency contracts for worker billing domains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class CommonDependencies:
    billing_core: Any
    async_session_type: type
    get_billing_cache: Callable[[], Any]
    audit: Callable[..., Any]
    billing_idempotency_replay_total: Any


@dataclass(frozen=True, slots=True)
class GenerationDependencies:
    billing_core: Any
    wallet_billing_applies: Callable[..., Any]
    billing_enabled: Callable[[], Any]
    existing_wallet_tx: Callable[..., Any]
    add_replay_audit: Callable[..., Any]
    generation_billing_ref_id: Callable[[Any], str]
    generation_billing_retry_count: Callable[[Any], int]
    generation_billing_tier: Callable[[Any], str | None]
    generation_snapshot_cost: Callable[..., tuple[int, str] | None]
    generation_rate_multiplier_x10000: Callable[..., Any]
    apply_rate_multiplier_micro: Callable[[int, int], int]
    thresholds: Callable[[], Any]
    held_amount_for_ref: Callable[..., Any]
    existing_ref_consumption_tx: Callable[..., Any]
    allow_negative_balance: Callable[[], Any]
    record_balance_cache_refresh: Callable[..., None]
    audit: Callable[..., Any]
    generation_settle_provider: Callable[[Any], str | None]
    wallet_overdrawn_total: Any


@dataclass(frozen=True, slots=True)
class UnknownUpstreamDependencies:
    billing_core: Any
    wallet_billing_applies: Callable[..., Any]
    billing_enabled: Callable[[], Any]
    existing_wallet_tx: Callable[..., Any]
    add_replay_audit: Callable[..., Any]
    held_amount_for_ref: Callable[..., Any]
    existing_ref_consumption_tx: Callable[..., Any]
    allow_negative_balance: Callable[[], Any]
    record_balance_cache_refresh: Callable[..., None]
    audit: Callable[..., Any]
    wallet_overdrawn_total: Any


@dataclass(frozen=True, slots=True)
class UnknownUpstreamSettlement:
    ref_type: str
    ref_id: str
    billing_obligation: bool
    no_hold_scope: str
    no_hold_extra: dict[str, Any]
    settle_meta: dict[str, Any]
    settle_event: str
    settle_audit_extra: dict[str, Any]
    overdraw_extra: dict[str, Any]
    reason: str
    knowledge: str


@dataclass(frozen=True, slots=True)
class CompletionPricingDependencies:
    billing_core: Any
    async_session_type: type
    task_pricing_snapshot: Callable[[Any], dict[str, Any] | None]
    existing_fingerprint_tx: Callable[..., Any]
    add_replay_audit: Callable[..., Any]
    held_amount_for_ref: Callable[..., Any]
    existing_ref_consumption_tx: Callable[..., Any]
    completion_cost_breakdown: Callable[..., Any]
    completion_rate_multiplier_x10000: Callable[..., Any]
    completion_service_tier: Callable[[Any], str]
    completion_billing_ref_id: Callable[[Any], str]
    window_rate_limit_enabled: Callable[[], Any]
    get_billing_cache: Callable[[], Any]
    audit: Callable[..., Any]
    billing_rate_limit_block_total: Any


@dataclass(frozen=True, slots=True)
class CompletionBillingRuntimeSnapshot:
    billing_enabled: bool
    cache_aware: bool
    allow_negative: bool
    window_rate_limit: bool


@dataclass(frozen=True, slots=True)
class CompletionDependencies:
    billing_core: Any
    async_session_type: type
    wallet_billing_applies: Callable[..., Any]
    billing_enabled: Callable[[], Any]
    cache_aware_enabled: Callable[[], Any]
    existing_wallet_tx: Callable[..., Any]
    add_replay_audit: Callable[..., Any]
    completion_billing_ref_id: Callable[[Any], str]
    completion_billing_retry_count: Callable[[Any], int]
    completion_usage: Callable[..., Any]
    completion_rate_multiplier_x10000: Callable[..., Any]
    completion_service_tier: Callable[[Any], str]
    resolve_completion_breakdown: Callable[..., Any]
    completion_request_fingerprint: Callable[..., Any]
    audit_completion_window_limit: Callable[..., Any]
    allow_negative_balance: Callable[[], Any]
    record_completion_settlement: Callable[..., Any]
    record_balance_cache_refresh: Callable[..., None]
    record_window_cache_increment: Callable[..., None]
    ensure_billing_window_usage_event: Callable[..., Any]
    get_billing_cache: Callable[[], Any]
    audit: Callable[..., Any]
    billing_cost_micro_total: Any
    billing_pricing_source_total: Any
    wallet_charge_lost_total: Any
    wallet_overdrawn_total: Any
