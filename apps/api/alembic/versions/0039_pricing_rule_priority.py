"""Add deterministic priority to pricing rule groups.

Revision ID: 0039_pricing_priority
Revises: 0038_telegram_tg_user_id
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0039_pricing_priority"
down_revision: str | None = "0038_telegram_tg_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pricing_rules",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pricing_rules", "priority")
