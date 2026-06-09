"""Add Omni Flash video pricing and hold estimates.

Revision ID: 0034_omni_flash_defaults
Revises: 0033_perf_hot_path_indexes
Create Date: 2026-06-09
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0034_omni_flash_defaults"
down_revision: str | None = "0033_perf_hot_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "omni-flash"
_SETTING_ID = "00000000-0000-7000-8000-000000000065"
_SETTING_KEY = "video.token_hold_estimates"
_TOKENS_PER_SECOND = 1_000_000
_DURATIONS = tuple(range(6, 11))
_RESOLUTIONS = ("720p", "1080p", "4k")
_ACTIONS = ("t2v", "i2v", "reference_image")

# Third-party Omni Flash gateways expose different commercial prices. These
# defaults keep the model visible and use conservative per-second RMB pricing;
# operators should review them against their chosen gateway before production.
_PRICE_BY_RESOLUTION = {
    "720p": 1_008_000,
    "1080p": 1_728_000,
    "4k": 3_456_000,
}
_PRICE_ROWS = tuple(
    (
        f"00000000-0000-7000-8000-00000000008{idx}",
        f"{action}_{resolution}",
        price_micro,
    )
    for idx, (action, resolution, price_micro) in enumerate(
        (
            (action, resolution, _PRICE_BY_RESOLUTION[resolution])
            for action in _ACTIONS
            for resolution in _RESOLUTIONS
        ),
        start=1,
    )
)
_NOTE = "Omni Flash third-party unified video gateway default; review before production"


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
        for resolution in _RESOLUTIONS
        for duration_s in _DURATIONS
    }


def _merge_estimates(raw: str | None) -> str:
    value = _load(raw)
    model_map = value.get(_MODEL)
    if not isinstance(model_map, dict):
        model_map = {}
        value[_MODEL] = model_map
    entries = _duration_entries()
    for action in _ACTIONS:
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
