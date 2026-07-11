from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.routes import billing
from app.services.billing import errors, pricing_values, redemption_values, usage
from lumen_core.schemas import AdminRedemptionCodeCreateIn, BillingUsageByKindOut


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_route_preserves_billing_value_helper_compatibility_names() -> None:
    names_by_module = {
        errors: ("_http",),
        pricing_values: (
            "_ZERO_PRICE_ALLOWED_UNITS",
            "_bulk_multiplier_x10000",
            "_bulk_numeric_micro",
            "_openai_price_micro",
            "_parse_price_rows",
            "_pricing_group_priorities",
            "_rmb_to_micro_or_422",
            "_validate_enabled_pricing_value",
        ),
        redemption_values: (
            "_DOWNLOAD_TOKEN_PREFIX",
            "_PLAINTEXT_BATCH_PREFIX",
            "_REDEMPTION_ALREADY_USED_CONSTRAINT",
            "_REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT",
            "_REDEMPTION_DOWNLOAD_TTL_SECONDS",
            "_REDEMPTION_IDEMPOTENCY_NAMESPACE",
            "_REDEMPTION_IDEMPOTENCY_TTL_SECONDS",
            "_REDEMPTION_IDEMPOTENCY_UUID_NAMESPACE",
            "_REDEMPTION_KNOWN_CONSTRAINTS",
            "_REDEMPTION_REPLAY_CONSTRAINTS",
            "_client_idempotency_key",
            "_integrity_constraint_name",
            "_redemption_batch_idempotency_key",
            "_redemption_batch_lock_identity",
            "_redemption_batch_payload_matches",
            "_redemption_batch_request_hash",
            "_redemption_csv_batch_id",
            "_redemption_csv_payload",
            "_redemption_idempotency_cache_key",
            "_redemption_idempotency_key",
            "_redemption_plaintext_payload",
            "_redemption_request_hash",
            "_redemption_status",
            "_redemption_usage_id",
            "_require_redemption_download_batch",
        ),
        usage: (
            "_CHARGE_KINDS",
            "_meta_int",
            "_scaled_meta_cost",
            "_usage_by_kind",
            "_usage_total",
        ),
    }

    for module, names in names_by_module.items():
        for name in names:
            assert getattr(billing, name) is getattr(module, name)


def test_http_value_preserves_error_envelope_and_details() -> None:
    exc = errors._http(  # noqa: SLF001
        "invalid_amount",
        "amount is invalid",
        422,
        field="amount_rmb",
    )

    assert exc.status_code == 422
    assert exc.detail == {
        "error": {
            "code": "invalid_amount",
            "message": "amount is invalid",
            "details": {"field": "amount_rmb"},
        }
    }


def test_redemption_request_and_idempotency_values_are_stable() -> None:
    request_hash = redemption_values._redemption_request_hash(  # noqa: SLF001
        "ABCD-1234"
    )
    derived = redemption_values._redemption_idempotency_key(  # noqa: SLF001
        _request(),
        user_id="user-1",
        normalized_code="ABCD-1234",
    )
    client = redemption_values._redemption_idempotency_key(  # noqa: SLF001
        _request([(b"idempotency-key", b"redeem-1")]),
        user_id="user-1",
        normalized_code="ABCD-1234",
    )

    assert len(request_hash) == 64
    assert request_hash == redemption_values._redemption_request_hash(  # noqa: SLF001
        "ABCD-1234"
    )
    assert derived == redemption_values._redemption_idempotency_key(  # noqa: SLF001
        _request(),
        user_id="user-1",
        normalized_code="ABCD-1234",
    )
    assert derived.startswith("derived:")
    assert client == "client:redeem-1"
    assert redemption_values._redemption_usage_id(  # noqa: SLF001
        "user-1", client
    ) == redemption_values._redemption_usage_id(  # noqa: SLF001
        "user-1", client
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"  ",
        b"x" * 129,
        b"contains space",
        b"\xff",
    ],
)
def test_client_idempotency_key_rejects_invalid_headers(raw: bytes) -> None:
    with pytest.raises(HTTPException) as exc_info:
        redemption_values._client_idempotency_key(  # noqa: SLF001
            _request([(b"idempotency-key", raw)])
        )

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    error = detail["error"]
    assert isinstance(error, dict)
    assert error["code"] == "idempotency_key_invalid"


def test_redemption_batch_hash_normalizes_expiry_timezone() -> None:
    naive = AdminRedemptionCodeCreateIn(
        amount_rmb="10",
        count=2,
        max_redemptions=3,
        expires_at=datetime(2026, 7, 12, 12),
        note="batch",
    )
    aware = AdminRedemptionCodeCreateIn(
        amount_rmb="10",
        count=2,
        max_redemptions=3,
        expires_at=datetime(2026, 7, 12, 12, tzinfo=timezone.utc),
        note="batch",
    )

    assert redemption_values._redemption_batch_request_hash(  # noqa: SLF001
        naive, amount_micro=10_000_000
    ) == redemption_values._redemption_batch_request_hash(  # noqa: SLF001
        aware, amount_micro=10_000_000
    )


def test_redemption_batch_idempotency_bucket_and_lock_identity() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    first = redemption_values._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now,
    )
    retry = redemption_values._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now + timedelta(seconds=299),
    )
    later = redemption_values._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now + timedelta(seconds=300),
    )

    assert retry == first
    assert later != first
    assert redemption_values._redemption_batch_lock_identity(  # noqa: SLF001
        first, "request-hash"
    ) == redemption_values._redemption_batch_lock_identity(  # noqa: SLF001
        later, "request-hash"
    )
    assert (
        redemption_values._redemption_batch_idempotency_key(  # noqa: SLF001
            _request([(b"idempotency-key", b"batch-1")]),
            admin_id="admin-1",
            request_hash="request-hash",
            now=now,
        )
        == "client:batch-1"
    )


def test_redemption_status_preserves_precedence_and_expiry_boundary() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    revoked = SimpleNamespace(
        revoked_at=now,
        expires_at=now - timedelta(days=1),
        redeemed_count=1,
        max_redemptions=1,
    )
    expired = SimpleNamespace(
        revoked_at=None,
        expires_at=now,
        redeemed_count=0,
        max_redemptions=1,
    )
    exhausted = SimpleNamespace(
        revoked_at=None,
        expires_at=None,
        redeemed_count=1,
        max_redemptions=1,
    )

    assert redemption_values._redemption_status(revoked, now=now) == "revoked"  # type: ignore[arg-type]  # noqa: SLF001
    assert redemption_values._redemption_status(expired, now=now) == "expired"  # type: ignore[arg-type]  # noqa: SLF001
    assert redemption_values._redemption_status(exhausted, now=now) == "exhausted"  # type: ignore[arg-type]  # noqa: SLF001


def test_redemption_plaintext_csv_and_batch_validation_round_trip() -> None:
    expires_at = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    codes = ["LMN-AAAA-BBBB-CCCC-DDDD", "LMN-EEEE-FFFF-GGGG-HHHH"]
    plaintext = redemption_values._redemption_plaintext_payload(  # noqa: SLF001
        batch_id="batch-1",
        amount_micro=10_000_000,
        codes=codes,
        expires_at=expires_at,
    )
    csv_text = redemption_values._redemption_csv_payload(  # noqa: SLF001
        batch_id="batch-1",
        amount_micro=10_000_000,
        codes=codes,
        expires_at=expires_at,
    )

    assert json.loads(plaintext) == {
        "batch_id": "batch-1",
        "amount_rmb": "10",
        "expires_at": expires_at.isoformat(),
        "codes": codes,
    }
    assert list(csv.DictReader(io.StringIO(csv_text))) == [
        {
            "code": codes[0],
            "amount_rmb": "10",
            "batch_id": "batch-1",
            "expires_at": expires_at.isoformat(),
        },
        {
            "code": codes[1],
            "amount_rmb": "10",
            "batch_id": "batch-1",
            "expires_at": expires_at.isoformat(),
        },
    ]
    assert redemption_values._redemption_csv_batch_id(csv_text) == "batch-1"  # noqa: SLF001
    redemption_values._require_redemption_download_batch(  # noqa: SLF001
        csv_text, "batch-1"
    )

    with pytest.raises(HTTPException) as exc_info:
        redemption_values._require_redemption_download_batch(  # noqa: SLF001
            csv_text, "batch-2"
        )
    assert exc_info.value.status_code == 404


def test_redemption_batch_payload_matches_persisted_values() -> None:
    expires_at = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    batch: Any = SimpleNamespace(
        code_count=2,
        amount_micro=10_000_000,
        expires_at=expires_at,
    )
    payload = {
        "amount_rmb": "10",
        "expires_at": "2026-07-12T12:00:00",
        "codes": ["one", "two"],
    }

    assert redemption_values._redemption_batch_payload_matches(  # noqa: SLF001
        batch, payload
    )
    assert not redemption_values._redemption_batch_payload_matches(  # noqa: SLF001
        batch, {**payload, "amount_rmb": "11"}
    )
    assert not redemption_values._redemption_batch_payload_matches(  # noqa: SLF001
        batch, {**payload, "codes": ["one"]}
    )


def test_integrity_constraint_name_uses_driver_diagnostics_and_message_fallback() -> (
    None
):
    class DiagnosticError(Exception):
        def __init__(self) -> None:
            super().__init__("duplicate")
            self.diag = SimpleNamespace(constraint_name="UQ_REDEEM_CODE_USER")

    diagnostic = DiagnosticError()
    diag_error = IntegrityError("statement", {}, diagnostic)
    fallback_error = IntegrityError(
        "statement",
        {},
        RuntimeError("duplicate uq_redemption_batch_creator_idemp"),
    )

    assert (
        redemption_values._integrity_constraint_name(diag_error)  # noqa: SLF001
        == "uq_redeem_code_user"
    )
    assert (
        redemption_values._integrity_constraint_name(fallback_error)  # noqa: SLF001
        == "uq_redemption_batch_creator_idemp"
    )


def test_price_rows_parse_json_and_simple_yaml_values() -> None:
    assert pricing_values._parse_price_rows(  # noqa: SLF001
        '{"models":[{"model":"gpt-a","input_usd_per_1m":1},"ignored"]}'
    ) == [{"model": "gpt-a", "input_usd_per_1m": 1}]
    assert pricing_values._parse_price_rows(  # noqa: SLF001
        """
        # pricing
        - model: gpt-a
          input_usd_per_1m: 1.25
        - model: gpt-b
          output_usd_per_1m: "2.5"
        """
    ) == [
        {"model": "gpt-a", "input_usd_per_1m": 1.25},
        {"model": "gpt-b", "output_usd_per_1m": 2.5},
    ]


def test_pricing_conversions_and_enabled_value_validation() -> None:
    assert pricing_values._openai_price_micro("0.0005", 1.0) == 1  # noqa: SLF001
    assert pricing_values._bulk_numeric_micro("", field="rate") is None  # noqa: SLF001
    assert (
        pricing_values._bulk_numeric_micro("1.25", field="rate")  # noqa: SLF001
        == 1_250_000
    )
    assert (
        pricing_values._bulk_multiplier_x10000(2.25, field="rate")  # noqa: SLF001
        == 22_500
    )
    pricing_values._validate_enabled_pricing_value(  # noqa: SLF001
        unit="long_context_threshold",
        price_micro=0,
        enabled=True,
        field="threshold",
    )

    with pytest.raises(HTTPException) as exc_info:
        pricing_values._validate_enabled_pricing_value(  # noqa: SLF001
            unit="per_1k_tokens_in",
            price_micro=0,
            enabled=True,
            field="price",
        )
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    error = detail["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_amount"


def test_pricing_group_priorities_validate_each_rule_group() -> None:
    values = [
        {
            "scope": "chat_model",
            "key": "gpt-*",
            "variant": "default",
            "priority": 10,
        },
        {
            "scope": "chat_model",
            "key": "gpt-*",
            "variant": "default",
            "priority": 10,
        },
    ]

    assert pricing_values._pricing_group_priorities(values) == {  # noqa: SLF001
        ("chat_model", "gpt-*", "default"): 10
    }

    with pytest.raises(HTTPException) as exc_info:
        pricing_values._pricing_group_priorities(  # noqa: SLF001
            [values[0], {**values[1], "priority": 20}]
        )
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    error = detail["error"]
    assert isinstance(error, dict)
    assert error["code"] == "pricing_priority_mismatch"
    assert error["details"] == {
        "scope": "chat_model",
        "key": "gpt-*",
        "variant": "default",
    }


def test_usage_metadata_classification_and_total_values() -> None:
    rows: list[Any] = [
        SimpleNamespace(
            kind="charge",
            amount_micro=-25_000,
            ref_type="completion",
            meta={
                "cost_breakdown": {
                    "input_cost_micro": 10_000,
                    "output_cost_micro": 20_000,
                    "cache_read_cost_micro": 5_000,
                    "cache_creation_cost_micro": 3_000,
                    "image_output_cost_micro": 2_000,
                    "reasoning_cost_micro": 1_000,
                    "rate_multiplier_x10000": 5000,
                }
            },
        ),
        SimpleNamespace(
            kind="settle",
            amount_micro=-40_000,
            ref_type="video_generation",
            meta={"actual_micro": 40_000},
        ),
        SimpleNamespace(
            kind="settle",
            amount_micro=-30_000,
            ref_type="prompt_enhance",
            meta={"actual_micro": 30_000},
        ),
    ]

    result = usage._usage_by_kind(rows)  # noqa: SLF001

    assert usage._meta_int({"value": "-1"}, "value") == 0  # noqa: SLF001
    assert usage._meta_int({"value": "bad"}, "value") == 0  # noqa: SLF001
    assert (
        usage._scaled_meta_cost(  # noqa: SLF001
            {"cost": 5_000, "rate_multiplier_x10000": 15_000},
            "cost",
        )
        == 7_500
    )
    assert result == BillingUsageByKindOut(
        input=5_000,
        output=40_000,
        cache_read=2_500,
        cache_creation=1_500,
        image=41_000,
        reasoning=500,
    )
    assert usage._usage_total(result) == 90_500  # noqa: SLF001
