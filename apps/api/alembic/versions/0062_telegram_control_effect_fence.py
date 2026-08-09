"""Add fenced Telegram control effect receipts.

Revision ID: 0062_tg_control_effect_fence
Revises: 0061_video_jsonb_types
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from telegram_downgrade_guard import require_prepared_downgrade_export


revision: str = "0062_tg_control_effect_fence"
down_revision: str | None = "0061_video_jsonb_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_owner", sa.String(96), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_fence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_tg_control_effect_status",
        "telegram_control_commands",
        "effect_status IN ('pending','running','succeeded','failed')",
        postgresql_not_valid=True,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                'ALTER TABLE "telegram_control_commands" '
                'VALIDATE CONSTRAINT "ck_tg_control_effect_status"'
            )
        )
    op.create_index(
        "ix_tg_control_effect_due",
        "telegram_control_commands",
        ["effect_status", "effect_lease_until"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE status IN ('pending','published') "
            "AND effect_status IN ('pending','running')"
        )
    ).scalar_one()
    if active:
        raise RuntimeError("cannot downgrade with active Telegram control effects")
    invalid_terminal = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE status IN ('accepted','failed') "
            "AND effect_status IN ('pending','running')"
        )
    ).scalar_one()
    if invalid_terminal:
        raise RuntimeError(
            "cannot downgrade with non-terminal effects on terminal Telegram commands"
        )
    require_prepared_downgrade_export(revision)
    op.drop_index(
        "ix_tg_control_effect_due",
        table_name="telegram_control_commands",
    )
    op.drop_constraint(
        "ck_tg_control_effect_status",
        "telegram_control_commands",
        type_="check",
    )
    for column in (
        "effect_error",
        "effect_completed_at",
        "effect_attempts",
        "effect_fence",
        "effect_lease_until",
        "effect_owner",
        "effect_status",
    ):
        op.drop_column("telegram_control_commands", column)
