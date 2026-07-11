"""Deterministic redemption request and payload helpers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from sqlalchemy.exc import IntegrityError

from lumen_core import billing as billing_core
from lumen_core.models import RedemptionBatch, RedemptionCode
from lumen_core.schemas import AdminRedemptionCodeCreateIn

from .errors import _http


_DOWNLOAD_TOKEN_PREFIX = "billing:redemption_csv:"
_PLAINTEXT_BATCH_PREFIX = "billing:redemption_plaintext:"
_REDEMPTION_DOWNLOAD_TTL_SECONDS = 300
_REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT = "uq_redemption_batch_creator_idemp"
_REDEMPTION_IDEMPOTENCY_NAMESPACE = "billing:redemption:idempotency"
_REDEMPTION_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
_REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE = uuid.UUID(
    "cf14d7e7-73ca-4b91-89fa-d4ab765034c9"
)
_REDEMPTION_ALREADY_USED_CONSTRAINT = "uq_redeem_code_user"
_REDEMPTION_REPLAY_CONSTRAINTS = frozenset(
    (
        _REDEMPTION_ALREADY_USED_CONSTRAINT,
        "redemption_codes_usage_pkey",
        "uq_wallet_tx_idemp",
    )
)
_REDEMPTION_KNOWN_CONSTRAINTS = _REDEMPTION_REPLAY_CONSTRAINTS | {
    _REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT
}


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    for source in (diag, orig):
        value = getattr(source, "constraint_name", None)
        if isinstance(value, str) and value:
            return value.lower()
    msg = f"{exc!s} {diag!s}".lower()
    for name in _REDEMPTION_KNOWN_CONSTRAINTS:
        if name in msg:
            return name
    return None


def _redemption_request_hash(normalized_code: str) -> str:
    return hashlib.sha256(
        f"redemption-code:{normalized_code}".encode("utf-8")
    ).hexdigest()


def _client_idempotency_key(request: Request | None) -> str | None:
    headers = getattr(request, "headers", None)
    raw = headers.get("Idempotency-Key") if headers is not None else None
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must not be blank",
            422,
        )
    if len(key) > 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must be 1-128 printable ASCII characters",
            422,
        )
    return key


def _redemption_idempotency_key(
    request: Request,
    *,
    user_id: str,
    normalized_code: str,
) -> str:
    key = _client_idempotency_key(request)
    if key is None:
        digest = hashlib.sha256(
            f"{user_id}:{normalized_code}".encode("utf-8")
        ).hexdigest()[:32]
        return f"derived:{digest}"
    return f"client:{key}"


def _redemption_batch_request_hash(
    body: AdminRedemptionCodeCreateIn,
    *,
    amount_micro: int,
) -> str:
    expires_at = body.expires_at
    if expires_at is not None:
        expires_at = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None
            else expires_at.astimezone(timezone.utc)
        )
    payload = {
        "amount_micro": int(amount_micro),
        "count": int(body.count),
        "max_redemptions": int(body.max_redemptions),
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "note": body.note,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _redemption_batch_idempotency_key(
    request: Request | None,
    *,
    admin_id: str,
    request_hash: str,
    now: datetime | None = None,
) -> str:
    key = _client_idempotency_key(request)
    if key is not None:
        return f"client:{key}"
    current = now or datetime.now(timezone.utc)
    bucket = int(current.timestamp()) // _REDEMPTION_DOWNLOAD_TTL_SECONDS
    digest = hashlib.sha256(
        f"{admin_id}:{request_hash}:{bucket}".encode("utf-8")
    ).hexdigest()[:32]
    return f"derived:{bucket}:{digest}"


def _redemption_batch_lock_identity(
    idempotency_key: str,
    request_hash: str,
) -> str:
    if idempotency_key.startswith("derived:"):
        return f"derived-request:{request_hash}"
    return idempotency_key


def _redemption_usage_id(user_id: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            _REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE,
            f"{user_id}:{idempotency_key}",
        )
    )


def _redemption_idempotency_cache_key(user_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{user_id}:{idempotency_key}".encode("utf-8")).hexdigest()


def _redemption_status(code: RedemptionCode, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    expires_at = code.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if code.revoked_at is not None:
        return "revoked"
    if expires_at is not None and expires_at <= current:
        return "expired"
    if code.redeemed_count >= code.max_redemptions:
        return "exhausted"
    return "active"


def _redemption_plaintext_payload(
    *, batch_id: str, amount_micro: int, codes: list[str], expires_at: datetime | None
) -> str:
    return json.dumps(
        {
            "batch_id": batch_id,
            "amount_rmb": billing_core.micro_to_rmb_str(amount_micro),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "codes": codes,
        },
        ensure_ascii=False,
    )


def _redemption_csv_payload(
    *, batch_id: str, amount_micro: int, codes: list[str], expires_at: datetime | None
) -> str:
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["code", "amount_rmb", "batch_id", "expires_at"])
    for code in codes:
        writer.writerow(
            [
                code,
                billing_core.micro_to_rmb_str(amount_micro),
                batch_id,
                expires_at.isoformat() if expires_at else "",
            ]
        )
    return csv_buf.getvalue()


def _redemption_csv_batch_id(csv_text: str) -> str | None:
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        value = row.get("batch_id")
        return str(value) if value else None
    return None


def _require_redemption_download_batch(csv_text: str, batch_id: str) -> None:
    if _redemption_csv_batch_id(csv_text) != batch_id:
        raise _http(
            "download_token_batch_mismatch", "download token does not match batch", 404
        )


def _redemption_batch_payload_matches(
    batch: RedemptionBatch,
    payload: dict[str, Any],
) -> bool:
    codes = payload.get("codes")
    if not isinstance(codes, list) or len(codes) != int(batch.code_count):
        return False
    try:
        amount_micro = billing_core.rmb_to_micro(str(payload.get("amount_rmb") or "0"))
    except billing_core.BillingError:
        return False
    if amount_micro != int(batch.amount_micro):
        return False
    expires_raw = payload.get("expires_at")
    if expires_raw:
        try:
            payload_expires = datetime.fromisoformat(str(expires_raw))
        except ValueError:
            return False
        payload_expires = (
            payload_expires.replace(tzinfo=timezone.utc)
            if payload_expires.tzinfo is None
            else payload_expires.astimezone(timezone.utc)
        )
    else:
        payload_expires = None
    batch_expires = batch.expires_at
    if batch_expires is not None:
        batch_expires = (
            batch_expires.replace(tzinfo=timezone.utc)
            if batch_expires.tzinfo is None
            else batch_expires.astimezone(timezone.utc)
        )
    return payload_expires == batch_expires
