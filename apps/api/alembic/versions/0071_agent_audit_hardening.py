"""Add safe Agent continuation, checkpoint, and image-catalog state.

Revision ID: 0071_agent_audit_hardening
Revises: 0070_agent_session_context
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0071_agent_audit_hardening"
down_revision: str | None = "0070_agent_session_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "agent_runs",
        "reasoning_effort",
        server_default=None,
        existing_type=sa.String(length=16),
        existing_nullable=True,
    )
    op.add_column(
        "agent_runs",
        sa.Column("continuation_source_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("active_pi_compaction_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("active_pi_compaction_schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("active_pi_compaction_event_seq", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "agent_session_images",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_session_id",
            sa.String(length=36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reference_label", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("display_label", sa.String(length=80), nullable=True),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="history",
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "agent_session_id", "image_id", name="uq_agent_session_images_image"
        ),
        sa.UniqueConstraint(
            "agent_session_id",
            "reference_label",
            name="uq_agent_session_images_label",
        ),
    )
    op.create_index(
        "ix_agent_session_images_active",
        "agent_session_images",
        ["agent_session_id", "active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    protected_state = (
        bind.execute(sa.text("SELECT COUNT(*) FROM agent_session_images")).scalar()
        or bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM agent_runs "
                "WHERE continuation_source_run_id IS NOT NULL"
            )
        ).scalar()
        or bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM agent_sessions "
                "WHERE active_pi_compaction_run_id IS NOT NULL"
            )
        ).scalar()
    )
    if int(protected_state or 0) > 0:
        raise RuntimeError(
            "0071 downgrade requires backup restoration after Agent v3 state exists"
        )
    op.drop_index("ix_agent_session_images_active", table_name="agent_session_images")
    op.drop_table("agent_session_images")
    op.drop_column("agent_sessions", "active_pi_compaction_event_seq")
    op.drop_column("agent_sessions", "active_pi_compaction_schema_version")
    op.drop_column("agent_sessions", "active_pi_compaction_run_id")
    op.drop_column("agent_runs", "continuation_source_run_id")
    op.alter_column(
        "agent_runs",
        "reasoning_effort",
        server_default="max",
        existing_type=sa.String(length=16),
        existing_nullable=True,
    )
