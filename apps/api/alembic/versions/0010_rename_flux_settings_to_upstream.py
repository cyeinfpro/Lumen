"""Rename legacy flux system setting keys to upstream.

Revision ID: 0010_upstream_settings
Revises: 0009_indexes_generation_default
Create Date: 2026-04-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010_upstream_settings"
down_revision: str | None = "0009_indexes_generation_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KEY_RENAMES = (
    ("flux.base_url", "upstream.base_url"),
    ("flux.api_key", "upstream.api_key"),
    ("flux.pixel_budget", "upstream.pixel_budget"),
    ("flux.global_concurrency", "upstream.global_concurrency"),
    ("flux.default_model", "upstream.default_model"),
)


def _rename_setting_key(old_key: str, new_key: str) -> None:
    op.execute(
        sa.text(
            """
            UPDATE system_settings
            SET key = :new_key
            WHERE key = :old_key
              AND NOT EXISTS (
                  SELECT 1 FROM system_settings WHERE key = :new_key
              )
            """
        ).bindparams(old_key=old_key, new_key=new_key)
    )
    op.execute(
        sa.text("DELETE FROM system_settings WHERE key = :old_key").bindparams(
            old_key=old_key
        )
    )


def upgrade() -> None:
    for old_key, new_key in _KEY_RENAMES:
        _rename_setting_key(old_key, new_key)


def downgrade() -> None:
    for old_key, new_key in reversed(_KEY_RENAMES):
        _rename_setting_key(new_key, old_key)
