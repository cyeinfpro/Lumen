"""Add revisioned Agent output, transcripts, and provider-call evidence.

Revision ID: 0072_agent_runtime_contracts
Revises: 0071_agent_audit_hardening
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0072_agent_runtime_contracts"
down_revision: str | None = "0071_agent_audit_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "output_revision", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "output_runtime_seq", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "transcript_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_agent_runs_output_revision_nonnegative",
        "agent_runs",
        "output_revision >= 0",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_agent_runs_output_runtime_seq_nonnegative",
        "agent_runs",
        "output_runtime_seq >= 0",
        postgresql_not_valid=True,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                'ALTER TABLE "agent_runs" VALIDATE CONSTRAINT '
                '"ck_agent_runs_output_revision_nonnegative"'
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE "agent_runs" VALIDATE CONSTRAINT '
                '"ck_agent_runs_output_runtime_seq_nonnegative"'
            )
        )
    op.create_table(
        "agent_provider_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("dispatch_ordinal", sa.Integer(), nullable=False),
        sa.Column("permit_id", sa.String(length=192), nullable=False),
        sa.Column(
            "delivery_state",
            sa.String(length=24),
            nullable=False,
            server_default="authorized",
        ),
        sa.Column(
            "result_state",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "exact_usage_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "evidence_event_seq", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("uncertainty_reason", sa.String(length=64), nullable=True),
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
            "agent_run_id",
            "execution_epoch",
            "dispatch_ordinal",
            name="uq_agent_provider_calls_run_epoch_ordinal",
        ),
        sa.CheckConstraint(
            "execution_epoch >= 0",
            name="ck_agent_provider_calls_epoch_nonnegative",
        ),
        sa.CheckConstraint(
            "dispatch_ordinal >= 1",
            name="ck_agent_provider_calls_ordinal_positive",
        ),
        sa.CheckConstraint(
            "evidence_event_seq >= 0",
            name="ck_agent_provider_calls_event_seq_nonnegative",
        ),
        sa.CheckConstraint(
            "delivery_state IN ('authorized', 'dispatched', 'responded', "
            "'completed', 'cancelled', 'unknown')",
            name="ck_agent_provider_calls_delivery_state",
        ),
        sa.CheckConstraint(
            "result_state IN ('pending', 'exact', 'missing', 'failed', 'unknown')",
            name="ck_agent_provider_calls_result_state",
        ),
    )
    op.create_index(
        "ix_agent_provider_calls_run_epoch",
        "agent_provider_calls",
        ["agent_run_id", "execution_epoch", "dispatch_ordinal"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    protected = bind.execute(
        sa.text("SELECT COUNT(*) FROM agent_provider_calls")
    ).scalar()
    revised = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_runs WHERE output_revision > 0 "
            "OR output_runtime_seq > 0 OR transcript_jsonb <> '{}'"
        )
    ).scalar()
    if int(protected or 0) > 0 or int(revised or 0) > 0:
        raise RuntimeError(
            "0072 downgrade requires backup restoration after revisioned Agent state exists"
        )
    op.drop_index(
        "ix_agent_provider_calls_run_epoch", table_name="agent_provider_calls"
    )
    op.drop_table("agent_provider_calls")
    op.drop_constraint(
        "ck_agent_runs_output_runtime_seq_nonnegative",
        "agent_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_runs_output_revision_nonnegative",
        "agent_runs",
        type_="check",
    )
    op.drop_column("agent_runs", "transcript_jsonb")
    op.drop_column("agent_runs", "output_runtime_seq")
    op.drop_column("agent_runs", "output_revision")
