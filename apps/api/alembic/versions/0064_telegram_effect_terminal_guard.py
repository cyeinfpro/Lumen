"""Repair terminal Telegram effects and prevent terminal commands from claims.

Revision ID: 0064_tg_effect_terminal_guard
Revises: 0063_storage_apply_retry_fence
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0064_tg_effect_terminal_guard"
down_revision: str | None = "0063_storage_apply_retry_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    unknown_statuses = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE status NOT IN ('pending','published','accepted','failed')"
        )
    ).scalar_one()
    if unknown_statuses:
        raise RuntimeError(
            "cannot repair Telegram effect state with unknown command statuses"
        )

    bind.execute(
        sa.text(
            """
            UPDATE telegram_control_commands
            SET effect_status = CASE
                    WHEN status = 'accepted' THEN 'succeeded'
                    WHEN status = 'failed' THEN 'failed'
                    ELSE effect_status
                END,
                effect_owner = CASE
                    WHEN status IN ('accepted','failed') THEN NULL
                    ELSE effect_owner
                END,
                effect_lease_until = CASE
                    WHEN status IN ('accepted','failed') THEN NULL
                    ELSE effect_lease_until
                END,
                effect_completed_at = CASE
                    WHEN status IN ('accepted','failed') THEN COALESCE(
                        effect_completed_at,
                        completed_at,
                        accepted_at,
                        updated_at,
                        created_at
                    )
                    ELSE effect_completed_at
                END
            WHERE status IN ('accepted','failed')
            """
        )
    )
    op.create_check_constraint(
        "ck_tg_control_effect_active_command",
        "telegram_control_commands",
        "status IN ('pending','published') OR effect_status IN ('succeeded','failed')",
        postgresql_not_valid=True,
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                'ALTER TABLE "telegram_control_commands" '
                "VALIDATE CONSTRAINT "
                '"ck_tg_control_effect_active_command"'
            )
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_tg_control_effect_active_command",
        "telegram_control_commands",
        type_="check",
    )
