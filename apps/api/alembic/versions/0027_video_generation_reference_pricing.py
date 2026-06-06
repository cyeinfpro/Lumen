"""Video reference pricing and smart-duration migration.

Revision ID: 0027_video_generation_reference_pricing
Revises: 0026_video_generation
Create Date: 2026-06-05
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0027_video_generation_reference_pricing"
down_revision: str | None = "0026_video_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_VIDEO_PRICE_NOTE = "默认视频价格；需按火山最新价格复核"
_NEW_VIDEO_HOLD_ESTIMATES: dict[str, dict[str, dict[str, int]]] = {
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
}

_VIDEO_PRICE_ROWS: tuple[tuple[str, str, str, int, str], ...] = (
    (
        "00000000-0000-7000-8000-000000000026",
        "seedance-2.0",
        "t2v",
        46_000_000,
        "火山官方基准价：无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000027",
        "seedance-2.0",
        "i2v",
        46_000_000,
        "火山官方基准价：无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-00000000002c",
        "seedance-2.0",
        "reference",
        46_000_000,
        "旧 Reference fallback；按无视频输入官方基准价",
    ),
    (
        "00000000-0000-7000-8000-00000000002e",
        "seedance-2.0",
        "reference_image",
        46_000_000,
        "火山官方基准价：无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-00000000002f",
        "seedance-2.0",
        "reference_video",
        28_000_000,
        "火山官方基准价：含视频输入 28 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000028",
        "seedance-2.0-fast",
        "t2v",
        37_000_000,
        "火山官方基准价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000029",
        "seedance-2.0-fast",
        "i2v",
        37_000_000,
        "火山官方基准价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-00000000002d",
        "seedance-2.0-fast",
        "reference",
        37_000_000,
        "旧 Reference fallback；按无视频输入官方基准价",
    ),
    (
        "00000000-0000-7000-8000-000000000030",
        "seedance-2.0-fast",
        "reference_image",
        37_000_000,
        "火山官方基准价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000031",
        "seedance-2.0-fast",
        "reference_video",
        22_000_000,
        "火山官方基准价：含视频输入 22 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000032",
        "seedance-2.0",
        "t2v_480p",
        46_000_000,
        "火山官方价：480P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000033",
        "seedance-2.0",
        "t2v_720p",
        46_000_000,
        "火山官方价：720P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000034",
        "seedance-2.0",
        "t2v_1080p",
        51_000_000,
        "火山官方价：1080P 无视频输入 51 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000035",
        "seedance-2.0",
        "i2v_480p",
        46_000_000,
        "火山官方价：480P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000036",
        "seedance-2.0",
        "i2v_720p",
        46_000_000,
        "火山官方价：720P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000037",
        "seedance-2.0",
        "i2v_1080p",
        51_000_000,
        "火山官方价：1080P 无视频输入 51 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000038",
        "seedance-2.0",
        "reference_image_480p",
        46_000_000,
        "火山官方价：480P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000039",
        "seedance-2.0",
        "reference_image_720p",
        46_000_000,
        "火山官方价：720P 无视频输入 46 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000040",
        "seedance-2.0",
        "reference_image_1080p",
        51_000_000,
        "火山官方价：1080P 无视频输入 51 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000041",
        "seedance-2.0",
        "reference_video_480p",
        28_000_000,
        "火山官方价：480P 含视频输入 28 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000042",
        "seedance-2.0",
        "reference_video_720p",
        28_000_000,
        "火山官方价：720P 含视频输入 28 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000043",
        "seedance-2.0",
        "reference_video_1080p",
        31_000_000,
        "火山官方价：1080P 含视频输入 31 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000044",
        "seedance-2.0",
        "reference_480p",
        46_000_000,
        "旧 Reference fallback；480P 无视频输入官方价",
    ),
    (
        "00000000-0000-7000-8000-000000000045",
        "seedance-2.0",
        "reference_720p",
        46_000_000,
        "旧 Reference fallback；720P 无视频输入官方价",
    ),
    (
        "00000000-0000-7000-8000-000000000046",
        "seedance-2.0",
        "reference_1080p",
        51_000_000,
        "旧 Reference fallback；1080P 无视频输入官方价",
    ),
    (
        "00000000-0000-7000-8000-000000000047",
        "seedance-2.0-fast",
        "t2v_480p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000048",
        "seedance-2.0-fast",
        "t2v_720p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000049",
        "seedance-2.0-fast",
        "t2v_1080p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000050",
        "seedance-2.0-fast",
        "i2v_480p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000051",
        "seedance-2.0-fast",
        "i2v_720p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000052",
        "seedance-2.0-fast",
        "i2v_1080p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000053",
        "seedance-2.0-fast",
        "reference_image_480p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000054",
        "seedance-2.0-fast",
        "reference_image_720p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000055",
        "seedance-2.0-fast",
        "reference_image_1080p",
        37_000_000,
        "火山官方价：无视频输入 37 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000056",
        "seedance-2.0-fast",
        "reference_video_480p",
        22_000_000,
        "火山官方价：含视频输入 22 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000057",
        "seedance-2.0-fast",
        "reference_video_720p",
        22_000_000,
        "火山官方价：含视频输入 22 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000058",
        "seedance-2.0-fast",
        "reference_video_1080p",
        22_000_000,
        "火山官方价：含视频输入 22 元/百万 token",
    ),
    (
        "00000000-0000-7000-8000-000000000059",
        "seedance-2.0-fast",
        "reference_480p",
        37_000_000,
        "旧 Reference fallback；无视频输入官方价",
    ),
    (
        "00000000-0000-7000-8000-000000000060",
        "seedance-2.0-fast",
        "reference_720p",
        37_000_000,
        "旧 Reference fallback；无视频输入官方价",
    ),
    (
        "00000000-0000-7000-8000-000000000061",
        "seedance-2.0-fast",
        "reference_1080p",
        37_000_000,
        "旧 Reference fallback；无视频输入官方价",
    ),
)


def _dump_estimates(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_video_hold_estimates(raw: str | None) -> str:
    try:
        current = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        current = {}
    if not isinstance(current, dict):
        current = {}
    for model, model_defaults in _NEW_VIDEO_HOLD_ESTIMATES.items():
        model_map = current.get(model)
        if not isinstance(model_map, dict):
            model_map = {}
            current[model] = model_map
        for action, action_defaults in model_defaults.items():
            action_map = model_map.get(action)
            if not isinstance(action_map, dict):
                action_map = {}
                model_map[action] = action_map
            for key, estimate in action_defaults.items():
                action_map.setdefault(key, estimate)
    return _dump_estimates(current)


def _upgrade_video_generation_table() -> None:
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s = -1 OR (duration_s >= 4 AND duration_s <= 15)",
        )
        batch_op.alter_column(
            "generate_audio",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.text("true"),
        )
        batch_op.alter_column(
            "seed",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def _downgrade_video_generation_table() -> None:
    op.execute("UPDATE video_generations SET duration_s = 15 WHERE duration_s = -1")
    with op.batch_alter_table("video_generations") as batch_op:
        batch_op.drop_constraint("ck_video_gen_duration_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_video_gen_duration_positive",
            "duration_s > 0",
        )
        batch_op.alter_column(
            "generate_audio",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.text("false"),
        )
        batch_op.alter_column(
            "seed",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )


def _upsert_video_prices() -> None:
    stmt = sa.text(
        """
        INSERT INTO pricing_rules
          (id, scope, key, variant, unit, price_micro, enabled, note)
        VALUES
          (:id, 'video', :key, :variant, 'per_mtoken', :price_micro, true, :note)
        ON CONFLICT (scope, key, variant, unit) DO UPDATE SET
          price_micro = CASE
            WHEN pricing_rules.note = :legacy_note
            THEN excluded.price_micro
            ELSE pricing_rules.price_micro
          END,
          note = CASE
            WHEN pricing_rules.note = :legacy_note
            THEN excluded.note
            ELSE pricing_rules.note
          END,
          enabled = pricing_rules.enabled,
          updated_at = CASE
            WHEN pricing_rules.note = :legacy_note
            THEN CURRENT_TIMESTAMP
            ELSE pricing_rules.updated_at
          END
        """
    )
    bind = op.get_bind()
    for row_id, key, variant, price_micro, note in _VIDEO_PRICE_ROWS:
        bind.execute(
            stmt,
            {
                "id": row_id,
                "key": key,
                "variant": variant,
                "price_micro": price_micro,
                "note": note,
                "legacy_note": _LEGACY_VIDEO_PRICE_NOTE,
            },
        )


def _upgrade_video_hold_estimates() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO system_settings (id, key, value)
            VALUES ('00000000-0000-7000-8000-000000000062', 'video.enabled', '0')
            ON CONFLICT (key) DO NOTHING
            """
        )
    )
    raw = bind.execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": "video.token_hold_estimates"},
    ).scalar_one_or_none()
    merged = _merge_video_hold_estimates(raw if isinstance(raw, str) else None)
    bind.execute(
        sa.text(
            """
            INSERT INTO system_settings (id, key, value)
            VALUES (
              '00000000-0000-7000-8000-000000000063',
              'video.token_hold_estimates',
              :value
            )
            ON CONFLICT (key) DO UPDATE SET
              value = excluded.value,
              updated_at = CURRENT_TIMESTAMP
            """
        ),
        {"value": merged},
    )


def upgrade() -> None:
    _upgrade_video_generation_table()
    _upsert_video_prices()
    _upgrade_video_hold_estimates()


def downgrade() -> None:
    _downgrade_video_generation_table()
    op.execute(
        sa.text(
            """
            DELETE FROM pricing_rules
            WHERE scope = 'video'
              AND unit = 'per_mtoken'
              AND variant NOT IN ('t2v', 'i2v')
            """
        )
    )
    legacy_rows = (
        ("seedance-2.0", "t2v", 20_000_000),
        ("seedance-2.0", "i2v", 20_000_000),
        ("seedance-2.0-fast", "t2v", 15_000_000),
        ("seedance-2.0-fast", "i2v", 15_000_000),
    )
    bind = op.get_bind()
    for key, variant, price_micro in legacy_rows:
        bind.execute(
            sa.text(
                """
                UPDATE pricing_rules
                SET price_micro = :price_micro,
                    note = :note,
                    updated_at = CURRENT_TIMESTAMP
                WHERE scope = 'video'
                  AND key = :key
                  AND variant = :variant
                  AND unit = 'per_mtoken'
                  AND note != :note
                """
            ),
            {
                "key": key,
                "variant": variant,
                "price_micro": price_micro,
                "note": _LEGACY_VIDEO_PRICE_NOTE,
            },
        )
