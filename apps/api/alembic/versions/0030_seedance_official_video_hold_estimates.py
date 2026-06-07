"""Align Seedance hold estimates with official video prices.

Revision ID: 0030_seedance_hold_estimates
Revises: 0029_media_owner_indexes
Create Date: 2026-06-07
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0030_seedance_hold_estimates"
down_revision: str | None = "0029_media_owner_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SETTING_ID = "00000000-0000-7000-8000-000000000062"
_SETTING_KEY = "video.token_hold_estimates"
_DURATIONS = tuple(range(4, 16))
_NO_VIDEO_720P_5S_MIN_TOKENS = 108_900

# Volcengine model price docs, updated 2026-05-28:
# no-video 5s 16:9 price is 480p/720p/1080p = 2.31/4.97/12.39 RMB
# for Seedance 2.0, and 1.86/4.00 RMB for Seedance 2.0 Fast.
# The price table is rounded. The query-task API example, and Volcengine
# billing details, show 720p 5s no-video usage as 108.9K tokens.
_NO_VIDEO_5S_PRICE_ROWS: dict[str, dict[str, tuple[int, int]]] = {
    "seedance-2.0": {
        "480p": (2_310_000, 46_000_000),
        "720p": (4_970_000, 46_000_000),
        "1080p": (12_390_000, 51_000_000),
    },
    "seedance-2.0-fast": {
        "480p": (1_860_000, 37_000_000),
        "720p": (4_000_000, 37_000_000),
    },
}

# Same docs: video-input 5s output, input 2-15s price range high end.
# Hold uses the 15s input upper bound; settlement refunds by actual usage.
_VIDEO_INPUT_MAX_5S_PRICE_ROWS: dict[str, dict[str, tuple[int, int]]] = {
    "seedance-2.0": {
        "480p": (5_620_000, 28_000_000),
        "720p": (12_100_000, 28_000_000),
        "1080p": (30_130_000, 31_000_000),
    },
    "seedance-2.0-fast": {
        "480p": (4_420_000, 22_000_000),
        "720p": (9_500_000, 22_000_000),
    },
}

_LEGACY_DEFAULTS: dict[str, dict[str, dict[str, int]]] = {
    "seedance-2.0": {
        "t2v": {
            "480p:5": 60_000,
            "720p:5": 60_000,
            "1080p:5": 130_000,
            "1080p:10": 280_000,
        },
        "i2v": {
            "480p:5": 60_000,
            "720p:5": 60_000,
            "1080p:5": 130_000,
            "1080p:10": 280_000,
        },
        "reference": {
            "480p:5": 60_000,
            "720p:5": 60_000,
            "1080p:5": 130_000,
            "1080p:10": 280_000,
        },
    },
    "seedance-2.0-fast": {
        "t2v": {"480p:5": 60_000, "720p:5": 60_000},
        "i2v": {"480p:5": 60_000, "720p:5": 60_000},
        "reference": {"480p:5": 60_000, "720p:5": 60_000},
    },
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


def _ceil_div(numerator: int, denominator: int) -> int:
    return (int(numerator) + int(denominator) - 1) // int(denominator)


def _ceil_tokens(price_micro: int, price_per_mtoken_micro: int) -> int:
    return _ceil_div(int(price_micro) * 1_000_000, int(price_per_mtoken_micro))


def _no_video_duration_entries(by_resolution: dict[str, int]) -> dict[str, int]:
    return {
        f"{resolution}:{duration_s}": _ceil_div(base_tokens * max(duration_s, 5), 5)
        for resolution, base_tokens in by_resolution.items()
        for duration_s in _DURATIONS
    }


def _video_input_duration_entries(by_resolution: dict[str, int]) -> dict[str, int]:
    return {
        f"{resolution}:{duration_s}": _ceil_div(base_tokens * (15 + duration_s), 20)
        for resolution, base_tokens in by_resolution.items()
        for duration_s in _DURATIONS
    }


def _official_estimates() -> dict[str, dict[str, dict[str, int]]]:
    estimates: dict[str, dict[str, dict[str, int]]] = {}
    for model, price_rows in _NO_VIDEO_5S_PRICE_ROWS.items():
        no_video_5s = {
            resolution: _ceil_tokens(price_micro, unit_price_micro)
            for resolution, (price_micro, unit_price_micro) in price_rows.items()
        }
        if "720p" in no_video_5s:
            no_video_5s["720p"] = max(
                no_video_5s["720p"], _NO_VIDEO_720P_5S_MIN_TOKENS
            )
        no_video_entries = _no_video_duration_entries(no_video_5s)
        estimates[model] = {
            "t2v": dict(no_video_entries),
            "i2v": dict(no_video_entries),
            "reference": dict(no_video_entries),
            "reference_image": dict(no_video_entries),
        }

    for model, price_rows in _VIDEO_INPUT_MAX_5S_PRICE_ROWS.items():
        video_input_5s = {
            resolution: _ceil_tokens(price_micro, unit_price_micro)
            for resolution, (price_micro, unit_price_micro) in price_rows.items()
        }
        estimates.setdefault(model, {})[
            "reference_video"
        ] = _video_input_duration_entries(video_input_5s)
    return estimates


def _merge_official_estimates(raw: str | None) -> str:
    value = _load(raw)
    for model, model_defaults in _official_estimates().items():
        model_map = value.get(model)
        if not isinstance(model_map, dict):
            model_map = {}
            value[model] = model_map
        for action, action_defaults in model_defaults.items():
            action_map = model_map.get(action)
            if not isinstance(action_map, dict):
                action_map = {}
                model_map[action] = action_map
            action_map.update(action_defaults)
    return _dump(value)


def _restore_legacy_estimates(raw: str | None) -> str:
    value = _load(raw)
    for model, model_defaults in _LEGACY_DEFAULTS.items():
        model_map = value.get(model)
        if not isinstance(model_map, dict):
            model_map = {}
            value[model] = model_map
        for action in ("reference_image", "reference_video"):
            model_map.pop(action, None)
        for action, action_defaults in model_defaults.items():
            model_map[action] = dict(action_defaults)
    return _dump(value)


def _current_setting() -> str | None:
    raw = op.get_bind().execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": _SETTING_KEY},
    ).scalar_one_or_none()
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


def upgrade() -> None:
    _upsert_setting(_merge_official_estimates(_current_setting()))


def downgrade() -> None:
    _upsert_setting(_restore_legacy_estimates(_current_setting()))
