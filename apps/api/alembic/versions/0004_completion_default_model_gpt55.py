"""Update completion default model to gpt-5.5.

Revision ID: 0004_chat_model_gpt55
Revises: 0003_robust_indexes
Create Date: 2026-04-24 04:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_chat_model_gpt55"
down_revision = "0003_robust_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "completions",
        "model",
        server_default="gpt-5.5",
        existing_type=sa.String(length=64),
    )


def downgrade() -> None:
    op.alter_column(
        "completions",
        "model",
        server_default="gpt-5.4",
        existing_type=sa.String(length=64),
    )
