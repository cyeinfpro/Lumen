"""Normalize the Seedance duration constraint with online validation.

Revision ID: 0066_seedance_duration_online
Revises: 0065_seedance_25_defaults
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0066_seedance_duration_online"
down_revision: str | None = "0065_seedance_25_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "video_generations"
_LEGACY_CONSTRAINT = "ck_video_gen_duration_positive"
_ONLINE_CONSTRAINT = "ck_video_gen_duration_positive_online"
_EXPRESSION = "duration_s = -1 OR (duration_s >= 3 AND duration_s <= 30)"


def _validate_constraint(name: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" VALIDATE CONSTRAINT "{name}"'))


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_check_constraint(
        _ONLINE_CONSTRAINT,
        _TABLE,
        _EXPRESSION,
        postgresql_not_valid=True,
    )
    _validate_constraint(_ONLINE_CONSTRAINT)
    op.drop_constraint(_LEGACY_CONSTRAINT, _TABLE, type_="check")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_check_constraint(
        _LEGACY_CONSTRAINT,
        _TABLE,
        _EXPRESSION,
        postgresql_not_valid=True,
    )
    _validate_constraint(_LEGACY_CONSTRAINT)
    op.drop_constraint(_ONLINE_CONSTRAINT, _TABLE, type_="check")
