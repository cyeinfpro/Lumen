"""Add HappyHorse video pricing and hold estimates.

Revision ID: 0032_happyhorse_defaults
Revises: 0031_video_duration_3s
Create Date: 2026-06-07
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0032_happyhorse_defaults"
down_revision: str | None = "0031_video_duration_3s"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "happyhorse-1.0"
_SETTING_ID = "00000000-0000-7000-8000-000000000064"
_SETTING_KEY = "video.token_hold_estimates"
_TOKENS_PER_SECOND = 1_000_000
_DURATIONS = tuple(range(3, 16))

# Alibaba Cloud Model Studio HappyHorse official original prices are USD/sec:
# 720P = $0.14/s, 1080P = $0.24/s. Lumen pricing_rules are RMB micro-units,
# so these defaults use the repo's fallback 7.2 USD/CNY rate.
_PRICE_ROWS = (
    (
        "00000000-0000-7000-8000-000000000071",
        "t2v_720p",
        1_008_000,
    ),
    (
        "00000000-0000-7000-8000-000000000072",
        "t2v_1080p",
        1_728_000,
    ),
    (
        "00000000-0000-7000-8000-000000000073",
        "i2v_720p",
        1_008_000,
    ),
    (
        "00000000-0000-7000-8000-000000000074",
        "i2v_1080p",
        1_728_000,
    ),
    (
        "00000000-0000-7000-8000-000000000075",
        "reference_image_720p",
        1_008_000,
    ),
    (
        "00000000-0000-7000-8000-000000000076",
        "reference_image_1080p",
        1_728_000,
    ),
)
_NOTE = "HappyHorse official original price, USD/CNY=7.2, per output second"


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _duration_entries() -> dict[str, int]:
    return {
        f"{resolution}:{duration_s}": duration_s * _TOKENS_PER_SECOND
        for resolution in ("720p", "1080p")
        for duration_s in _DURATIONS
    }


def _merge_estimates(raw: str | None) -> str:
    value = _load(raw)
    model_map = value.get(_MODEL)
    if not isinstance(model_map, dict):
        model_map = {}
        value[_MODEL] = model_map
    entries = _duration_entries()
    for action in ("t2v", "i2v", "reference_image"):
        action_map = model_map.get(action)
        if not isinstance(action_map, dict):
            action_map = {}
            model_map[action] = action_map
        for key, estimate in entries.items():
            action_map.setdefault(key, estimate)
    return _dump(value)


def _remove_estimates(raw: str | None) -> str:
    value = _load(raw)
    value.pop(_MODEL, None)
    return _dump(value)


def _current_setting() -> str | None:
    raw = (
        op.get_bind()
        .execute(
            sa.text("SELECT value FROM system_settings WHERE key = :key"),
            {"key": _SETTING_KEY},
        )
        .scalar_one_or_none()
    )
    return raw if isinstance(raw, str) else None


def _upsert_setting(value: str) -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO system_settings (id, key, value)
            VALUES (:id, :key, :value)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            """
        ),
        {"id": _SETTING_ID, "key": _SETTING_KEY, "value": value},
    )


def _insert_prices() -> None:
    stmt = sa.text(
        """
        INSERT INTO pricing_rules
          (id, scope, key, variant, unit, price_micro, enabled, note)
        VALUES
          (:id, 'video', :key, :variant, 'per_mtoken', :price_micro, true, :note)
        ON CONFLICT (scope, key, variant, unit) DO NOTHING
        """
    )
    bind = op.get_bind()
    for row_id, variant, price_micro in _PRICE_ROWS:
        bind.execute(
            stmt,
            {
                "id": row_id,
                "key": _MODEL,
                "variant": variant,
                "price_micro": price_micro,
                "note": _NOTE,
            },
        )


def upgrade() -> None:
    _insert_prices()
    _upsert_setting(_merge_estimates(_current_setting()))


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM pricing_rules
            WHERE scope = 'video'
              AND key = :key
              AND unit = 'per_mtoken'
              AND note = :note
            """
        ).bindparams(key=_MODEL, note=_NOTE)
    )
    _upsert_setting(_remove_estimates(_current_setting()))
