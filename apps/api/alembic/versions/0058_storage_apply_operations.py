"""Add durable storage apply operations.

Revision ID: 0058_storage_apply_operations
Revises: 0057_repair_concurrent_indexes
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0058_storage_apply_operations"
down_revision: str | None = "0057_repair_concurrent_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_apply_operations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "requested_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("desired_config_sha256", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "active_slot",
            sa.Integer(),
            nullable=True,
            server_default="1",
        ),
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("dispatch_owner", sa.String(64), nullable=True),
        sa.Column(
            "dispatch_lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "dispatch_fence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_message", sa.Text(), nullable=True),
        sa.Column("host_started_at", sa.BigInteger(), nullable=True),
        sa.Column("host_finished_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','dispatched','succeeded','failed')",
            name="ck_storage_apply_operations_status",
        ),
        sa.CheckConstraint(
            "(status IN ('pending','dispatched') AND active_slot = 1) OR "
            "(status IN ('succeeded','failed') AND active_slot IS NULL)",
            name="ck_storage_apply_operations_active_slot",
        ),
        sa.UniqueConstraint(
            "active_slot",
            name="uq_storage_apply_one_active",
        ),
    )
    op.create_index(
        "ix_storage_apply_dispatch_due",
        "storage_apply_operations",
        ["status", "dispatch_lease_until", "created_at"],
    )


def downgrade() -> None:
    active = op.get_bind().execute(
        sa.text(
            """
            SELECT count(*)
            FROM storage_apply_operations
            WHERE status IN ('pending', 'dispatched')
            """
        )
    ).scalar_one()
    if active:
        raise RuntimeError(
            "cannot downgrade with pending or dispatched storage operations"
        )
    op.drop_index(
        "ix_storage_apply_dispatch_due",
        table_name="storage_apply_operations",
    )
    op.drop_table("storage_apply_operations")
