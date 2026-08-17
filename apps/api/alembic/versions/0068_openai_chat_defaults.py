"""Seed OpenAI chat pricing and set GPT-5.6 Sol defaults.

Revision ID: 0068_openai_chat_defaults
Revises: 0067_seedance_25_1080p
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from alembic import op
import sqlalchemy as sa

revision: str = "0068_openai_chat_defaults"
down_revision: str | None = "0067_seedance_25_1080p"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFAULT_RATE = Decimal("1.0")
_MICRO_RMB = Decimal("1000000")
_DEFAULT_CHAT_MODEL = "gpt-5.6-sol"
_OLD_DEFAULT_MODELS = ("gpt-5.4", "gpt-5.5")
_PRICE_NOTE_PREFIX = "OpenAI official standard"
_PRICE_SOURCE_DATE = "2026-08-17"
_SETTING_ID = "00000000-0000-7000-8068-000000000001"
_OPENAI_STANDARD_CHAT_PRICES = (
    ("gpt-5.6-sol", "5.00", "30.00"),
    ("gpt-5.6-terra", "2.00", "12.00"),
    ("gpt-5.6-luna", "0.20", "1.20"),
    ("gpt-5.6", "5.00", "30.00"),
    ("gpt-5.5", "5.00", "30.00"),
    ("gpt-5.5-pro", "30.00", "180.00"),
    ("gpt-5.4", "2.50", "15.00"),
    ("gpt-5.4-mini", "0.75", "4.50"),
    ("gpt-5.4-nano", "0.20", "1.25"),
    ("gpt-5.4-pro", "30.00", "180.00"),
    ("gpt-5.2", "1.75", "14.00"),
    ("gpt-5.2-pro", "21.00", "168.00"),
    ("gpt-5.1", "1.25", "10.00"),
    ("gpt-5", "1.25", "10.00"),
    ("gpt-5-mini", "0.25", "2.00"),
    ("gpt-5-nano", "0.05", "0.40"),
    ("gpt-5-pro", "15.00", "120.00"),
    ("gpt-4.1", "2.00", "8.00"),
    ("gpt-4.1-mini", "0.40", "1.60"),
    ("gpt-4.1-nano", "0.10", "0.40"),
    ("gpt-4o", "2.50", "10.00"),
    ("gpt-4o-2024-05-13", "5.00", "15.00"),
    ("gpt-4o-mini", "0.15", "0.60"),
    ("o1", "15.00", "60.00"),
    ("o1-pro", "150.00", "600.00"),
    ("o3-pro", "20.00", "80.00"),
    ("o3", "2.00", "8.00"),
    ("o4-mini", "1.10", "4.40"),
    ("o3-mini", "1.10", "4.40"),
    ("gpt-4-turbo-2024-04-09", "10.00", "30.00"),
    ("gpt-4-0613", "30.00", "60.00"),
    ("gpt-3.5-turbo", "0.50", "1.50"),
    ("gpt-3.5-turbo-0125", "0.50", "1.50"),
    ("gpt-3.5-turbo-1106", "1.00", "2.00"),
    ("gpt-3.5-turbo-instruct", "1.50", "2.00"),
    ("davinci-002", "2.00", "2.00"),
    ("babbage-002", "0.40", "0.40"),
)


def _usd_to_rmb_rate() -> Decimal:
    raw = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT value FROM system_settings "
                "WHERE key = 'billing.usd_to_rmb_rate'"
            )
        )
        .scalar_one_or_none()
    )
    try:
        rate = Decimal(str(raw or _DEFAULT_RATE))
    except InvalidOperation:
        return _DEFAULT_RATE
    if not rate.is_finite() or rate <= 0:
        return _DEFAULT_RATE
    return rate


def _price_micro(usd_per_1m: str, rate: Decimal) -> int:
    value = Decimal(usd_per_1m) * rate * _MICRO_RMB / Decimal("1000")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _pricing_row_id(index: int, unit_index: int) -> str:
    return f"00000000-0000-7000-8068-{index * 2 + unit_index + 10:012d}"


def _seed_pricing() -> None:
    bind = op.get_bind()
    rate = _usd_to_rmb_rate()
    for index, (model, input_usd, output_usd) in enumerate(
        _OPENAI_STANDARD_CHAT_PRICES
    ):
        for unit_index, (unit, price_usd, direction) in enumerate(
            (
                ("per_1k_tokens_in", input_usd, "input"),
                ("per_1k_tokens_out", output_usd, "output"),
            )
        ):
            note = (
                f"{_PRICE_NOTE_PREFIX} {direction} USD/1M={price_usd} "
                f"rate={rate} as of {_PRICE_SOURCE_DATE}"
            )
            existing = bind.execute(
                sa.text(
                    """
                    SELECT id, note
                    FROM pricing_rules
                    WHERE scope = 'chat_model'
                      AND key = :key
                      AND variant = 'default'
                      AND unit = :unit
                    """
                ),
                {"key": model, "unit": unit},
            ).mappings().one_or_none()
            values = {
                "key": model,
                "unit": unit,
                "price_micro": _price_micro(price_usd, rate),
                "note": note,
            }
            if existing is None:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO pricing_rules
                          (id, scope, key, variant, unit, price_micro,
                           priority, enabled, note)
                        VALUES
                          (:id, 'chat_model', :key, 'default', :unit,
                           :price_micro, 0, true, :note)
                        """
                    ),
                    {
                        **values,
                        "id": _pricing_row_id(index, unit_index),
                    },
                )
                continue
            existing_note = str(existing["note"] or "")
            if existing_note.startswith("OpenAI "):
                bind.execute(
                    sa.text(
                        """
                        UPDATE pricing_rules
                        SET price_micro = :price_micro,
                            enabled = true,
                            note = :note,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                        """
                    ),
                    {
                        **values,
                        "id": existing["id"],
                    },
                )


def _set_default_model() -> None:
    bind = op.get_bind()
    current = bind.execute(
        sa.text(
            "SELECT value FROM system_settings WHERE key = 'upstream.default_model'"
        )
    ).scalar_one_or_none()
    if current is None:
        bind.execute(
            sa.text(
                """
                INSERT INTO system_settings (id, key, value)
                VALUES (:id, 'upstream.default_model', :value)
                """
            ),
            {"id": _SETTING_ID, "value": _DEFAULT_CHAT_MODEL},
        )
    elif str(current).strip() in (*_OLD_DEFAULT_MODELS, ""):
        bind.execute(
            sa.text(
                """
                UPDATE system_settings
                SET value = :value,
                    updated_at = CURRENT_TIMESTAMP
                WHERE key = 'upstream.default_model'
                """
            ),
            {"value": _DEFAULT_CHAT_MODEL},
        )

    bind.execute(
        sa.text(
            """
            UPDATE api_supplier_templates
            SET default_chat_model = :value,
                updated_at = CURRENT_TIMESTAMP
            WHERE default_chat_model IN ('gpt-5.4', 'gpt-5.5')
            """
        ),
        {"value": _DEFAULT_CHAT_MODEL},
    )


def upgrade() -> None:
    op.alter_column(
        "completions",
        "model",
        server_default=_DEFAULT_CHAT_MODEL,
        existing_type=sa.String(length=64),
    )
    op.alter_column(
        "generations",
        "model",
        server_default=_DEFAULT_CHAT_MODEL,
        existing_type=sa.String(length=64),
    )
    op.alter_column(
        "api_supplier_templates",
        "default_chat_model",
        server_default=_DEFAULT_CHAT_MODEL,
        existing_type=sa.String(length=64),
    )
    _set_default_model()
    _seed_pricing()


def downgrade() -> None:
    op.alter_column(
        "api_supplier_templates",
        "default_chat_model",
        server_default="gpt-5.4",
        existing_type=sa.String(length=64),
    )
    op.alter_column(
        "generations",
        "model",
        server_default="gpt-5.5",
        existing_type=sa.String(length=64),
    )
    op.alter_column(
        "completions",
        "model",
        server_default="gpt-5.5",
        existing_type=sa.String(length=64),
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE system_settings
            SET value = 'gpt-5.5',
                updated_at = CURRENT_TIMESTAMP
            WHERE key = 'upstream.default_model'
              AND value = :current
            """
        ),
        {"current": _DEFAULT_CHAT_MODEL},
    )
    bind.execute(
        sa.text(
            """
            UPDATE api_supplier_templates
            SET default_chat_model = 'gpt-5.4',
                updated_at = CURRENT_TIMESTAMP
            WHERE default_chat_model = :current
            """
        ),
        {"current": _DEFAULT_CHAT_MODEL},
    )
    # Pricing is retained on downgrade because it may have been edited or used
    # for billing after this migration was applied.
