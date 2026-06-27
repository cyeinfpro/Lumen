"""Add Seedance 2.0 Mini video pricing and hold estimates.

Revision ID: 0036_seedance_20_mini
Revises: 0035_seedance_20_4k
Create Date: 2026-06-27
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0036_seedance_20_mini"
down_revision: str | None = "0035_seedance_20_4k"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "seedance-2.0-mini"
_SETTING_ID = "00000000-0000-7000-8000-000000000067"
_SETTING_KEY = "video.token_hold_estimates"
_DURATIONS = tuple(range(4, 16))
_NO_VIDEO_ACTIONS = ("t2v", "i2v", "reference", "reference_image")
_RESOLUTIONS = ("480p", "720p")

# BytePlus ModelArk pricing docs, updated 2026-06-23:
# - dreamina-seedance-2-0-mini-260615 supports 480p/720p output.
# - Online unit price is USD 3.5/MTok without video input and USD 2.1/MTok
#   with video input.
# - The local billing table stores the existing Volcengine RMB-style unit
#   prices; these values keep the same ratio against Seedance 2.0/Fast.
_NO_VIDEO_UNIT_PRICE = 23_000_000
_VIDEO_INPUT_UNIT_PRICE = 14_000_000

# Official 5s 16:9 examples: no-video Mini is USD 0.18/0.38 for 480p/720p.
# Video-input examples list the 15s input upper bound as USD 0.42/0.91.
_NO_VIDEO_5S_TOKENS = {
    "480p": 51_429,
    "720p": 108_900,
}
_VIDEO_INPUT_MAX_5S_TOKENS = {
    "480p": 200_000,
    "720p": 433_334,
}

_PRICE_ROWS = tuple(
    (
        f"00000000-0000-7000-8000-000000000{96 + index:03d}",
        f"{variant}_{resolution}",
        _VIDEO_INPUT_UNIT_PRICE
        if variant == "reference_video"
        else _NO_VIDEO_UNIT_PRICE,
        (
            "火山/ModelArk 官方价折算：Seedance 2.0 Mini 含视频输入 14 元/百万 token"
            if variant == "reference_video"
            else "火山/ModelArk 官方价折算：Seedance 2.0 Mini 无视频输入 23 元/百万 token"
        ),
    )
    for index, (variant, resolution) in enumerate(
        (variant, resolution)
        for variant in (
            "t2v",
            "i2v",
            "reference_image",
            "reference_video",
            "reference",
        )
        for resolution in _RESOLUTIONS
    )
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
        f"{resolution}:{duration_s}": _ceil_div(base_tokens * max(duration_s, 5), 5)
        for resolution, base_tokens in _NO_VIDEO_5S_TOKENS.items()
        for duration_s in _DURATIONS
    }


def _video_input_duration_entries() -> dict[str, int]:
    return {
        f"{resolution}:{duration_s}": _ceil_div(base_tokens * (15 + duration_s), 20)
        for resolution, base_tokens in _VIDEO_INPUT_MAX_5S_TOKENS.items()
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
        ON CONFLICT (scope, key, variant, unit) DO UPDATE
        SET price_micro = EXCLUDED.price_micro,
            enabled = true,
            note = CASE
                WHEN pricing_rules.note IS NULL
                  OR pricing_rules.note = ''
                  OR pricing_rules.note LIKE '%火山官方%'
                  OR pricing_rules.note LIKE '%ModelArk 官方%'
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
