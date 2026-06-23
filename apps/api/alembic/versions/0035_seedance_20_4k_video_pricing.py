"""Add Seedance 2.0 4k pricing and hold estimates.

Revision ID: 0035_seedance_20_4k
Revises: 0034_omni_flash_defaults
Create Date: 2026-06-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0035_seedance_20_4k"
down_revision: str | None = "0034_omni_flash_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "seedance-2.0"
_SETTING_ID = "00000000-0000-7000-8000-000000000066"
_SETTING_KEY = "video.token_hold_estimates"
_DURATIONS = tuple(range(4, 16))

# Volcengine ModelArk docs, updated 2026-06-23:
# - Seedance 2.0 supports 4k output.
# - 4k per-token unit prices are 26 RMB/MTok without video input and
#   16 RMB/MTok with video input.
# - Official quick-price examples list 4k 16:9 output 5s as 25.27 RMB
#   without video input, and 27.99-62.21 RMB with 2-15s video input.
_NO_VIDEO_4K_5S_TOKENS = 971_924
_VIDEO_INPUT_4K_MAX_5S_TOKENS = 3_888_125
_NO_VIDEO_4K_UNIT_PRICE = 26_000_000
_VIDEO_INPUT_4K_UNIT_PRICE = 16_000_000
_NO_VIDEO_ACTIONS = ("t2v", "i2v", "reference", "reference_image")

_PRICE_ROWS = (
    (
        "00000000-0000-7000-8000-000000000091",
        "t2v_4k",
        _NO_VIDEO_4K_UNIT_PRICE,
        "火山官方价：4k 无视频输入 26 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000092",
        "i2v_4k",
        _NO_VIDEO_4K_UNIT_PRICE,
        "火山官方价：4k 无视频输入 26 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000093",
        "reference_image_4k",
        _NO_VIDEO_4K_UNIT_PRICE,
        "火山官方价：4k 无视频输入 26 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000094",
        "reference_video_4k",
        _VIDEO_INPUT_4K_UNIT_PRICE,
        "火山官方价：4k 含视频输入 16 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000095",
        "reference_4k",
        _NO_VIDEO_4K_UNIT_PRICE,
        "旧 Reference fallback；4k 无视频输入官方价",
    ),
)


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


def _ceil_div(numerator: int, denominator: int) -> int:
    return (int(numerator) + int(denominator) - 1) // int(denominator)


def _no_video_duration_entries() -> dict[str, int]:
    return {
        f"4k:{duration_s}": _ceil_div(
            _NO_VIDEO_4K_5S_TOKENS * max(duration_s, 5), 5
        )
        for duration_s in _DURATIONS
    }


def _video_input_duration_entries() -> dict[str, int]:
    return {
        f"4k:{duration_s}": _ceil_div(
            _VIDEO_INPUT_4K_MAX_5S_TOKENS * (15 + duration_s), 20
        )
        for duration_s in _DURATIONS
    }


def _merge_estimates(raw: str | None) -> str:
    value = _load(raw)
    model_map = value.get(_MODEL)
    if not isinstance(model_map, dict):
        model_map = {}
        value[_MODEL] = model_map
    no_video_entries = _no_video_duration_entries()
    for action in _NO_VIDEO_ACTIONS:
        action_map = model_map.get(action)
        if not isinstance(action_map, dict):
            action_map = {}
            model_map[action] = action_map
        action_map.update(no_video_entries)
    action_map = model_map.get("reference_video")
    if not isinstance(action_map, dict):
        action_map = {}
        model_map["reference_video"] = action_map
    action_map.update(_video_input_duration_entries())
    return _dump(value)


def _remove_estimates(raw: str | None) -> str:
    value = _load(raw)
    model_map = value.get(_MODEL)
    if isinstance(model_map, dict):
        for action_map in model_map.values():
            if not isinstance(action_map, dict):
                continue
            for key in list(action_map):
                if isinstance(key, str) and key.startswith("4k:"):
                    action_map.pop(key, None)
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
        ON CONFLICT (scope, key, variant, unit) DO UPDATE
        SET price_micro = EXCLUDED.price_micro,
            enabled = true,
            note = CASE
                WHEN pricing_rules.note IS NULL
                  OR pricing_rules.note = ''
                  OR pricing_rules.note LIKE '%火山官方%'
                  OR pricing_rules.note LIKE '%需按火山最新价格复核%'
                THEN EXCLUDED.note
                ELSE pricing_rules.note
            END,
            updated_at = CURRENT_TIMESTAMP
        """
    )
    bind = op.get_bind()
    for row_id, variant, price_micro, note in _PRICE_ROWS:
        bind.execute(
            stmt,
            {
                "id": row_id,
                "key": _MODEL,
                "variant": variant,
                "price_micro": price_micro,
                "note": note,
            },
        )


def upgrade() -> None:
    _insert_prices()
    _upsert_setting(_merge_estimates(_current_setting()))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM pricing_rules
            WHERE scope = 'video'
              AND key = :key
              AND unit = 'per_mtoken'
              AND variant IN :variants
            """
        ).bindparams(sa.bindparam("variants", expanding=True)),
        {"key": _MODEL, "variants": tuple(row[1] for row in _PRICE_ROWS)},
    )
    _upsert_setting(_remove_estimates(_current_setting()))
