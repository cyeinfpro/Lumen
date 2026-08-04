"""Shared recovery primitives for orphan wallet holds."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.billing_operations import AuditLog, WalletTransaction
from lumen_core.model_entities.media_workflows import WorkflowRun

from ...audit import AuditPersistenceError, hash_email
from ..prompt_parts import idempotency as prompt_idempotency

OrphanHoldRecoveryAction = Literal["release", "settle_default", "manual_review"]


@dataclass(frozen=True, slots=True)
class HoldGroup:
    tx_ids: list[str]
    aggregate_held_micro: int

    @property
    def count(self) -> int:
        return len(self.tx_ids)


@dataclass(frozen=True, slots=True)
class PromptHoldRecoveryEvidence:
    action: OrphanHoldRecoveryAction
    proof: str
    active: bool = False


@dataclass(frozen=True, slots=True)
class _PromptOperationSnapshot:
    run: WorkflowRun | None
    operation: prompt_idempotency.PromptEnhanceOperation | None
    record: dict[str, Any] | None
    evidence: PromptHoldRecoveryEvidence


def recovery_action(ref_type: str | None) -> OrphanHoldRecoveryAction:
    if ref_type in {"generation", "completion", "video_generation"}:
        return "release"
    if ref_type == "prompt_enhance":
        return "settle_default"
    return "manual_review"


def _prompt_operation(
    run: WorkflowRun,
    metadata_key: str,
    record: dict[str, Any],
) -> prompt_idempotency.PromptEnhanceOperation | None:
    client_key_hash = record.get("client_key_hash")
    operation_namespace = record.get("operation_namespace")
    request_fingerprint = record.get("request_fingerprint")
    billing_request_id = record.get("billing_request_id")
    billing = record.get("billing")
    if (
        not isinstance(client_key_hash, str)
        or not client_key_hash
        or not isinstance(operation_namespace, str)
        or not operation_namespace
        or not isinstance(request_fingerprint, str)
        or not request_fingerprint
        or billing_request_id != run.id
        or not isinstance(billing, dict)
        or billing.get("mode") != "wallet"
        or billing.get("request_id") != run.id
        or billing.get("user_id") != run.user_id
        or not isinstance(billing.get("hold_amount_micro"), int)
        or billing["hold_amount_micro"] <= 0
    ):
        return None
    return prompt_idempotency.PromptEnhanceOperation(
        user_id=str(run.user_id),
        idempotency_key=str(run.id),
        client_key_hash=client_key_hash,
        operation_namespace=operation_namespace,
        request_fingerprint=request_fingerprint,
        record_id=str(run.id),
        record_type=str(run.type),
        metadata_key=metadata_key,
    )


def _prompt_evidence(
    record: dict[str, Any],
    *,
    now: datetime,
) -> PromptHoldRecoveryEvidence:
    attempt = prompt_idempotency._attempt_from_record(record)  # noqa: SLF001
    if record.get("state") == "running":
        if attempt is None:
            return PromptHoldRecoveryEvidence(
                "settle_default",
                "prompt_operation:malformed_attempt",
            )
        if attempt.lease_expires_at > now:
            return PromptHoldRecoveryEvidence(
                "manual_review",
                "prompt_operation:active_lease",
                active=True,
            )
    try:
        finalization = prompt_idempotency._finalization_from_record(record)  # noqa: SLF001
    except RuntimeError:
        return PromptHoldRecoveryEvidence(
            "settle_default",
            "prompt_operation:malformed_finalization",
        )
    raw_chunks = record.get("response_chunks")
    if not isinstance(raw_chunks, list) or any(
        not isinstance(chunk, str) for chunk in raw_chunks
    ):
        return PromptHoldRecoveryEvidence(
            "settle_default",
            "prompt_operation:malformed_response",
        )
    chunks = raw_chunks
    cost_possible = bool(
        record.get("dispatch_inflight")
        or record.get("upstream_cost_possible")
        or prompt_idempotency.has_nonempty_text(chunks)
    )
    if cost_possible:
        return PromptHoldRecoveryEvidence(
            "settle_default",
            "prompt_operation:dispatch_or_cost_possible",
        )
    if finalization is not None:
        if finalization.billing_action == "release":
            return PromptHoldRecoveryEvidence(
                "release",
                "prompt_operation:attempt_fenced_release_checkpoint",
            )
        return PromptHoldRecoveryEvidence(
            "settle_default",
            f"prompt_operation:finalization_{finalization.billing_action}",
        )
    if record.get("state") == "running" and attempt is not None:
        return PromptHoldRecoveryEvidence(
            "release",
            "prompt_operation:attempt_fenced_no_dispatch",
        )
    return PromptHoldRecoveryEvidence(
        "settle_default",
        "prompt_operation:terminal_or_unknown",
    )


async def _prompt_operation_snapshot(
    db: AsyncSession,
    hold: WalletTransaction,
    *,
    for_update: bool,
) -> _PromptOperationSnapshot:
    stmt = select(WorkflowRun).where(
        WorkflowRun.id == str(hold.ref_id),
        WorkflowRun.user_id == hold.user_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        return _PromptOperationSnapshot(
            None,
            None,
            None,
            PromptHoldRecoveryEvidence(
                "settle_default",
                "prompt_operation:missing",
            ),
        )
    metadata_key = dict(prompt_idempotency.PROMPT_OPERATION_RECORD_CONFIGS).get(
        str(run.type)
    )
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    record = metadata.get(metadata_key) if metadata_key is not None else None
    if not isinstance(record, dict):
        return _PromptOperationSnapshot(
            run,
            None,
            None,
            PromptHoldRecoveryEvidence(
                "settle_default",
                "prompt_operation:unrecognized",
            ),
        )
    operation = _prompt_operation(run, metadata_key, record)
    if operation is None:
        return _PromptOperationSnapshot(
            run,
            None,
            record,
            PromptHoldRecoveryEvidence(
                "settle_default",
                "prompt_operation:identity_mismatch",
            ),
        )
    return _PromptOperationSnapshot(
        run,
        operation,
        record,
        _prompt_evidence(record, now=datetime.now(timezone.utc)),
    )


async def recovery_action_for_hold(
    db: AsyncSession,
    hold: WalletTransaction,
) -> OrphanHoldRecoveryAction:
    if hold.ref_type != "prompt_enhance":
        return recovery_action(hold.ref_type)
    return (
        await _prompt_operation_snapshot(db, hold, for_update=False)
    ).evidence.action


async def resolve_prompt_hold_recovery(
    db: AsyncSession,
    hold: WalletTransaction,
    *,
    action: Literal["release", "settle_default"],
    http: Any,
) -> str:
    await prompt_idempotency.lock_prompt_operation_record(db, str(hold.ref_id))
    snapshot = await _prompt_operation_snapshot(db, hold, for_update=True)
    evidence = snapshot.evidence
    if evidence.active:
        raise http(
            "HOLD_TASK_ACTIVE",
            "prompt enhancement attempt lease is still active",
            409,
        )
    if evidence.action != action:
        if action == "release":
            raise http(
                "HOLD_RELEASE_NOT_PROVEN_SAFE",
                "prompt operation evidence does not prove upstream cost was absent",
                409,
            )
        raise http(
            "HOLD_SETTLEMENT_NOT_RECOMMENDED",
            "prompt operation evidence proves the hold can be safely released",
            409,
        )
    if (
        snapshot.run is None
        or snapshot.operation is None
        or snapshot.record is None
        or snapshot.record.get("state") != "running"
    ):
        return evidence.proof
    now = datetime.now(timezone.utc)
    record = dict(snapshot.record)
    attempt = prompt_idempotency._new_attempt(  # noqa: SLF001
        record,
        billing_request_id=str(hold.ref_id),
        now=now,
        lease_seconds=prompt_idempotency._LEASE_SECONDS,  # noqa: SLF001
    )
    error_code = (
        "idempotency_orphan_hold_released"
        if action == "release"
        else "idempotency_orphan_hold_settled"
    )
    terminal_chunk = f"data: {json.dumps({'error': error_code})}\n\n"
    prompt_idempotency._set_finalization(  # noqa: SLF001
        record,
        terminal_state="failed",
        terminal_chunk=terminal_chunk,
        billing_action=action,
        billing_capture=None,
        reason=f"admin_orphan_hold_{action}",
    )
    prompt_idempotency._replace_record(  # noqa: SLF001
        snapshot.run,
        snapshot.operation,
        record,
    )
    prompt_idempotency._record_terminal_response(  # noqa: SLF001
        snapshot.run,
        snapshot.operation,
        attempt=attempt,
        chunks=[terminal_chunk],
        terminal_state="failed",
    )
    return evidence.proof


async def load_hold_group(
    db: AsyncSession,
    hold: WalletTransaction,
) -> HoldGroup:
    rows = (
        await db.execute(
            select(
                WalletTransaction.id,
                WalletTransaction.amount_micro,
            )
            .where(
                WalletTransaction.user_id == hold.user_id,
                WalletTransaction.kind == "hold",
                WalletTransaction.ref_type == hold.ref_type,
                WalletTransaction.ref_id == hold.ref_id,
            )
            .order_by(
                WalletTransaction.created_at.asc(),
                WalletTransaction.id.asc(),
            )
        )
    ).all()
    return HoldGroup(
        tx_ids=[str(row.id) for row in rows],
        aggregate_held_micro=max(
            0,
            -sum(int(row.amount_micro or 0) for row in rows),
        ),
    )


def replay_hold_group(
    transaction: WalletTransaction,
    fallback_hold: WalletTransaction,
) -> HoldGroup:
    meta = transaction.meta if isinstance(transaction.meta, Mapping) else {}
    raw_ids = meta.get("hold_tx_ids")
    tx_ids = (
        [str(value) for value in raw_ids if isinstance(value, str) and value]
        if isinstance(raw_ids, list)
        else []
    )
    if not tx_ids:
        tx_ids = [str(meta.get("hold_tx_id") or fallback_hold.id)]
    try:
        aggregate = max(0, int(meta.get("aggregate_held_micro") or 0))
    except (TypeError, ValueError):
        aggregate = 0
    if aggregate <= 0:
        aggregate = max(0, -int(fallback_hold.amount_micro or 0))
    return HoldGroup(tx_ids=tx_ids, aggregate_held_micro=aggregate)


async def ensure_admin_recovery_audit(
    db: AsyncSession,
    *,
    commands: Any,
    http: Any,
    request: Any,
    admin: Any,
    target_user_id: str,
    event_type: str,
    transaction: WalletTransaction,
    transaction_detail_key: str,
    details: dict[str, Any],
    already_committed: bool,
) -> bool:
    existing_audit = (
        await db.execute(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == event_type,
                AuditLog.target_user_id == target_user_id,
                AuditLog.details[transaction_detail_key].as_string()
                == str(transaction.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_audit is not None:
        return False
    try:
        written = await commands.write_audit(
            db,
            event_type=event_type,
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=commands.request_ip_hash(request),
            target_user_id=target_user_id,
            details={
                **details,
                transaction_detail_key: transaction.id,
                "audit_recovery": already_committed,
                "original_created_by_admin": transaction.created_by_admin,
            },
            autocommit=False,
        )
    except AuditPersistenceError:
        await db.rollback()
        message = (
            "wallet recovery already exists, but its missing audit record "
            "could not be repaired"
            if already_committed
            else "wallet recovery was rolled back because its audit record "
            "could not be written"
        )
        raise http("AUDIT_WRITE_FAILED", message, 503) from None
    if written is not True:
        await db.rollback()
        message = (
            "wallet recovery already exists, but its missing audit record "
            "could not be repaired"
            if already_committed
            else "wallet recovery was rolled back because its audit record "
            "could not be written"
        )
        raise http("AUDIT_WRITE_FAILED", message, 503)
    return True
