"""Add audit_logs and outbox_dead_letter tables.

Revision ID: 0008_audit_dlq
Revises: 0007_messages_soft_delete
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008_audit_dlq"
down_revision: str | None = "0007_messages_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_email_hash", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("target_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])

    op.create_table(
        "outbox_dead_letter",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("outbox_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["outbox_id"], ["outbox_events.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_outbox_dead_letter_failed_at", "outbox_dead_letter", ["failed_at"]
    )
    op.create_index(
        "ix_outbox_dead_letter_resolved_at", "outbox_dead_letter", ["resolved_at"]
    )
    op.create_index(
        "ix_outbox_dead_letter_event_type", "outbox_dead_letter", ["event_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_dead_letter_event_type", table_name="outbox_dead_letter")
    op.drop_index("ix_outbox_dead_letter_resolved_at", table_name="outbox_dead_letter")
    op.drop_index("ix_outbox_dead_letter_failed_at", table_name="outbox_dead_letter")
    op.drop_table("outbox_dead_letter")

    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event_type", table_name="audit_logs")
    op.drop_table("audit_logs")
