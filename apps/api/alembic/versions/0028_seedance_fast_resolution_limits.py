"""Align Seedance 2.0 Fast defaults with official resolution limits.

Revision ID: 0028_seedance_fast_limits
Revises: 0027_video_ref_pricing
Create Date: 2026-06-06
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0028_seedance_fast_limits"
down_revision: str | None = "0027_video_ref_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FAST_MODEL = "seedance-2.0-fast"
_FAST_1080_VARIANTS = (
    "t2v_1080p",
    "i2v_1080p",
    "reference_image_1080p",
    "reference_video_1080p",
    "reference_1080p",
)
_FAST_1080_ESTIMATE_DEFAULTS = {
    "1080p:5": 130_000,
    "1080p:10": 280_000,
}


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


def _prune_fast_1080_estimates(raw: str | None) -> str | None:
    value = _load(raw)
    model_map = value.get(_FAST_MODEL)
    if not isinstance(model_map, dict):
        return None
    changed = False
    for action_map in model_map.values():
        if not isinstance(action_map, dict):
            continue
        for key in list(action_map):
            if isinstance(key, str) and key.startswith("1080p:"):
                action_map.pop(key, None)
                changed = True
    return _dump(value) if changed else None


def _restore_fast_1080_estimates(raw: str | None) -> str:
    value = _load(raw)
    model_map = value.setdefault(_FAST_MODEL, {})
    if not isinstance(model_map, dict):
        model_map = {}
        value[_FAST_MODEL] = model_map
    for action in ("t2v", "i2v", "reference"):
        action_map = model_map.setdefault(action, {})
        if not isinstance(action_map, dict):
            action_map = {}
            model_map[action] = action_map
        for key, estimate in _FAST_1080_ESTIMATE_DEFAULTS.items():
            action_map.setdefault(key, estimate)
    return _dump(value)


def _update_video_hold_estimates(value: str) -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE system_settings
            SET value = :value,
                updated_at = CURRENT_TIMESTAMP
            WHERE key = 'video.token_hold_estimates'
            """
        ),
        {"value": value},
    )


def upgrade() -> None:
    bind = op.get_bind()
    raw = bind.execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": "video.token_hold_estimates"},
    ).scalar_one_or_none()
    pruned = _prune_fast_1080_estimates(raw if isinstance(raw, str) else None)
    if pruned is not None:
        _update_video_hold_estimates(pruned)

    bind.execute(
        sa.text(
            """
            UPDATE pricing_rules
            SET enabled = false,
                note = COALESCE(note || '；', '')
                  || 'Seedance 2.0 Fast 不支持 1080P，已按官方文档停用',
                updated_at = CURRENT_TIMESTAMP
            WHERE scope = 'video'
              AND key = :model
              AND unit = 'per_mtoken'
              AND variant IN :variants
            """
        ).bindparams(sa.bindparam("variants", expanding=True)),
        {"model": _FAST_MODEL, "variants": _FAST_1080_VARIANTS},
    )


def downgrade() -> None:
    bind = op.get_bind()
    raw = bind.execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": "video.token_hold_estimates"},
    ).scalar_one_or_none()
    _update_video_hold_estimates(
        _restore_fast_1080_estimates(raw if isinstance(raw, str) else None)
    )
    bind.execute(
        sa.text(
            """
            UPDATE pricing_rules
            SET enabled = true,
                updated_at = CURRENT_TIMESTAMP
            WHERE scope = 'video'
              AND key = :model
              AND unit = 'per_mtoken'
              AND variant IN :variants
            """
        ).bindparams(sa.bindparam("variants", expanding=True)),
        {"model": _FAST_MODEL, "variants": _FAST_1080_VARIANTS},
    )
