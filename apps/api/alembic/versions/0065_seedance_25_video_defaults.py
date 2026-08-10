"""Add Seedance 2.5 provider mappings, pricing, and hold estimates.

Revision ID: 0065_seedance_25_defaults
Revises: 0064_tg_effect_terminal_guard
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "0065_seedance_25_defaults"
down_revision: str | None = "0064_tg_effect_terminal_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MODEL = "seedance-2.5"
_UPSTREAM_MODEL = "doubao-seedance-2-5-260628"
_PROVIDER_SETTING_KEY = "video.providers"
_ESTIMATE_SETTING_KEY = "video.token_hold_estimates"
_ESTIMATE_SETTING_ID = "00000000-0000-7000-8000-000000000210"
_DURATIONS = tuple(range(4, 31))
_NO_VIDEO_UNIT_PRICE = 70_000_000
_VIDEO_INPUT_UNIT_PRICE = 42_000_000
_FPS = 24

# Seedance 2.5 output dimensions from the official API reference. Holds use
# the largest pixel count in each resolution tier because pricing options are
# keyed by resolution, not aspect ratio.
_MAX_OUTPUT_PIXELS = {
    "480p": 992 * 432,
    "720p": 1112 * 834,
}
_MAX_REFERENCE_VIDEO_SECONDS = 30
_NO_VIDEO_ACTIONS = (
    "t2v",
    "i2v",
    "reference",
    "reference_image",
)
_MODEL_ACTION_KEYS = tuple(
    f"{_MODEL}:{action}" for action in ("t2v", "i2v", "reference")
)
_PRICE_VARIANTS = tuple(
    (variant, resolution)
    for variant in (
        "t2v",
        "i2v",
        "reference",
        "reference_image",
        "reference_video",
    )
    for resolution in _MAX_OUTPUT_PIXELS
)
_PRICE_ROWS = tuple(
    (
        f"00000000-0000-7000-8000-{200 + index:012d}",
        f"{variant}_{resolution}",
        _VIDEO_INPUT_UNIT_PRICE
        if variant == "reference_video"
        else _NO_VIDEO_UNIT_PRICE,
        (
            "火山方舟官方价（2026-08-07）：Seedance 2.5 "
            + (
                "含视频输入 42 元/百万 token"
                if variant == "reference_video"
                else "不含视频输入 70 元/百万 token"
            )
        ),
    )
    for index, (variant, resolution) in enumerate(_PRICE_VARIANTS)
)


def _widen_video_duration_constraint() -> None:
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s = -1 OR (duration_s >= 3 AND duration_s <= 30)",
        )


def _restore_video_duration_constraint() -> None:
    oversized = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*)
            FROM video_generations
            WHERE duration_s > 15
            """
            )
        )
        .scalar_one()
    )
    if oversized:
        raise RuntimeError(
            "cannot downgrade Seedance 2.5 defaults with video durations above 15 seconds"
        )
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s = -1 OR (duration_s >= 3 AND duration_s <= 15)",
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
    resolution: str,
    output_seconds: int,
    *,
    input_video_seconds: int = 0,
) -> int:
    return _ceil_div(
        (int(input_video_seconds) + int(output_seconds))
        * _MAX_OUTPUT_PIXELS[resolution]
        * _FPS,
        1024,
    )


def _official_estimates() -> dict[str, dict[str, dict[str, int]]]:
    no_video = {
        f"{resolution}:{duration_s}": _estimated_tokens(
            resolution,
            duration_s,
        )
        for resolution in _MAX_OUTPUT_PIXELS
        for duration_s in _DURATIONS
    }
    with_video = {
        f"{resolution}:{duration_s}": _estimated_tokens(
            resolution,
            duration_s,
            input_video_seconds=_MAX_REFERENCE_VIDEO_SECONDS,
        )
        for resolution in _MAX_OUTPUT_PIXELS
        for duration_s in _DURATIONS
    }
    return {
        _MODEL: {
            **{action: dict(no_video) for action in _NO_VIDEO_ACTIONS},
            "reference_video": with_video,
        }
    }


def _merge_estimates(raw: str | None) -> str:
    value = _load(raw)
    if not isinstance(value, dict):
        value = {}
    value[_MODEL] = _official_estimates()[_MODEL]
    return _dump(value)


def _remove_estimates(raw: str | None) -> str:
    value = _load(raw)
    if not isinstance(value, dict):
        return _dump({})
    value.pop(_MODEL, None)
    return _dump(value)


def _provider_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("providers"), list):
        return value["providers"]
    return []


def _merge_provider_models(raw: str | None) -> str | None:
    value = _load(raw)
    if value is None:
        return None
    changed = False
    for item in _provider_items(value):
        if not isinstance(item, dict):
            continue
        raw_kind = item.get("kind", "volcano")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if kind != "volcano":
            continue
        models = item.get("models")
        if not isinstance(models, dict):
            continue
        for key in _MODEL_ACTION_KEYS:
            if key not in models:
                models[key] = _UPSTREAM_MODEL
                changed = True
    return _dump(value) if changed else raw


def _remove_provider_models(raw: str | None) -> str | None:
    value = _load(raw)
    if value is None:
        return None
    changed = False
    for item in _provider_items(value):
        if not isinstance(item, dict):
            continue
        raw_kind = item.get("kind", "volcano")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if kind != "volcano":
            continue
        models = item.get("models")
        if not isinstance(models, dict):
            continue
        for key in _MODEL_ACTION_KEYS:
            if models.get(key) == _UPSTREAM_MODEL:
                models.pop(key)
                changed = True
    return _dump(value) if changed else raw


def _current_setting(key: str) -> str | None:
    raw = (
        op.get_bind()
        .execute(
            sa.text("SELECT value FROM system_settings WHERE key = :key"),
            {"key": key},
        )
        .scalar_one_or_none()
    )
    return raw if isinstance(raw, str) else None


def _upsert_setting(*, row_id: str, key: str, value: str) -> None:
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
        {"id": row_id, "key": key, "value": value},
    )


def _update_existing_setting(*, key: str, value: str | None) -> None:
    if value is None:
        return
    op.get_bind().execute(
        sa.text(
            """
            UPDATE system_settings
            SET value = :value,
                updated_at = CURRENT_TIMESTAMP
            WHERE key = :key
            """
        ),
        {"key": key, "value": value},
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
    _widen_video_duration_constraint()
    _insert_prices()
    _upsert_setting(
        row_id=_ESTIMATE_SETTING_ID,
        key=_ESTIMATE_SETTING_KEY,
        value=_merge_estimates(_current_setting(_ESTIMATE_SETTING_KEY)),
    )
    _update_existing_setting(
        key=_PROVIDER_SETTING_KEY,
        value=_merge_provider_models(_current_setting(_PROVIDER_SETTING_KEY)),
    )


def downgrade() -> None:
    _restore_video_duration_constraint()
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
        {
            "key": _MODEL,
            "variants": tuple(row[1] for row in _PRICE_ROWS),
        },
    )
    _upsert_setting(
        row_id=_ESTIMATE_SETTING_ID,
        key=_ESTIMATE_SETTING_KEY,
        value=_remove_estimates(_current_setting(_ESTIMATE_SETTING_KEY)),
    )
    _update_existing_setting(
        key=_PROVIDER_SETTING_KEY,
        value=_remove_provider_models(_current_setting(_PROVIDER_SETTING_KEY)),
    )
