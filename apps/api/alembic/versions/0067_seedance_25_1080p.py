"""Add Seedance 2.5 1080p pricing and hold estimates.

Revision ID: 0067_seedance_25_1080p
Revises: 0066_seedance_duration_online
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "0067_seedance_25_1080p"
down_revision: str | None = "0066_seedance_duration_online"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "seedance-2.5"
_ESTIMATE_SETTING_KEY = "video.token_hold_estimates"
_ESTIMATE_SETTING_ID = "00000000-0000-7000-8000-000000000210"
_DURATIONS = tuple(range(4, 31))
_RESOLUTION = "1080p"
_MAX_OUTPUT_PIXELS = 2206 * 946
_MAX_REFERENCE_VIDEO_SECONDS = 30
_FPS = 24
_NO_VIDEO_UNIT_PRICE = 77_000_000
_VIDEO_INPUT_UNIT_PRICE = 46_000_000
_NO_VIDEO_ACTIONS = (
    "t2v",
    "i2v",
    "reference",
    "reference_image",
)
_PRICE_VARIANTS = (
    "t2v",
    "i2v",
    "reference",
    "reference_image",
    "reference_video",
)
_PRICE_ROWS = tuple(
    (
        f"00000000-0000-7000-8000-{210 + index:012d}",
        f"{variant}_{_RESOLUTION}",
        (
            _VIDEO_INPUT_UNIT_PRICE
            if variant == "reference_video"
            else _NO_VIDEO_UNIT_PRICE
        ),
        (
            "Volcengine official list price (2026-08-17): Seedance 2.5 "
            + (
                "1080p with video input, RMB 46 per million tokens"
                if variant == "reference_video"
                else "1080p without video input, RMB 77 per million tokens"
            )
        ),
    )
    for index, variant in enumerate(_PRICE_VARIANTS)
)


def _dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _ceil_div(numerator: int, denominator: int) -> int:
    return (int(numerator) + int(denominator) - 1) // int(denominator)


def _estimated_tokens(
    output_seconds: int,
    *,
    input_video_seconds: int = 0,
) -> int:
    return _ceil_div(
        (int(input_video_seconds) + int(output_seconds))
        * _MAX_OUTPUT_PIXELS
        * _FPS,
        1024,
    )


def _official_1080p_estimates() -> dict[str, dict[str, int]]:
    no_video = {
        f"{_RESOLUTION}:{duration_s}": _estimated_tokens(duration_s)
        for duration_s in _DURATIONS
    }
    with_video = {
        f"{_RESOLUTION}:{duration_s}": _estimated_tokens(
            duration_s,
            input_video_seconds=_MAX_REFERENCE_VIDEO_SECONDS,
        )
        for duration_s in _DURATIONS
    }
    return {
        **{action: dict(no_video) for action in _NO_VIDEO_ACTIONS},
        "reference_video": with_video,
    }


def _merge_estimates(raw: str | None) -> str:
    value = _load(raw)
    if not isinstance(value, dict):
        value = {}
    model = value.get(_MODEL)
    if not isinstance(model, dict):
        model = {}
        value[_MODEL] = model
    for action, estimates in _official_1080p_estimates().items():
        action_map = model.get(action)
        if not isinstance(action_map, dict):
            action_map = {}
            model[action] = action_map
        for key, estimate in estimates.items():
            action_map.setdefault(key, estimate)
    return _dump(value)


def _remove_estimates(raw: str | None) -> str:
    value = _load(raw)
    if not isinstance(value, dict):
        return _dump({})
    model = value.get(_MODEL)
    if not isinstance(model, dict):
        return _dump(value)
    for action, estimates in _official_1080p_estimates().items():
        action_map = model.get(action)
        if not isinstance(action_map, dict):
            continue
        for key, estimate in estimates.items():
            if action_map.get(key) == estimate:
                action_map.pop(key, None)
    return _dump(value)


def _current_setting() -> str | None:
    raw = (
        op.get_bind()
        .execute(
            sa.text("SELECT value FROM system_settings WHERE key = :key"),
            {"key": _ESTIMATE_SETTING_KEY},
        )
        .scalar_one_or_none()
    )
    return raw if isinstance(raw, str) else None


def _upsert_estimates(value: str) -> None:
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
        {
            "id": _ESTIMATE_SETTING_ID,
            "key": _ESTIMATE_SETTING_KEY,
            "value": value,
        },
    )


def _insert_prices() -> None:
    statement = sa.text(
        """
        INSERT INTO pricing_rules
          (id, scope, key, variant, unit, price_micro, enabled, note)
        VALUES
          (:id, 'video', :key, :variant, 'per_mtoken', :price_micro, true, :note)
        ON CONFLICT (scope, key, variant, unit) DO UPDATE
        SET price_micro = EXCLUDED.price_micro,
            enabled = true,
            note = EXCLUDED.note,
            updated_at = CURRENT_TIMESTAMP
        """
    )
    bind = op.get_bind()
    for row_id, variant, price_micro, note in _PRICE_ROWS:
        bind.execute(
            statement,
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
    _upsert_estimates(_merge_estimates(_current_setting()))


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM pricing_rules
            WHERE scope = 'video'
              AND key = :key
              AND unit = 'per_mtoken'
              AND variant IN :variants
            """
        ).bindparams(sa.bindparam("variants", expanding=True)),
        {
            "key": _MODEL,
            "variants": tuple(row[1] for row in _PRICE_ROWS),
        },
    )
    _upsert_estimates(_remove_estimates(_current_setting()))
