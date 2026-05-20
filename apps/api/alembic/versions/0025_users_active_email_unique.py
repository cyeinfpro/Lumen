"""Allow soft-deleted user emails to be reused by active accounts.

Revision ID: 0025_users_active_email_unique
Revises: 0024_billing_cache_tokens
Create Date: 2026-05-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0025_users_active_email_unique"
down_revision: str | None = "0024_billing_cache_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    kwargs = {
        "unique": True,
        "postgresql_where": sa.text("deleted_at IS NULL"),
        "sqlite_where": sa.text("deleted_at IS NULL"),
    }
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.create_index(
                "uq_users_email_active",
                "users",
                ["email"],
                postgresql_concurrently=True,
                **kwargs,
            )
        op.drop_constraint("users_email_key", "users", type_="unique")
    else:
        op.drop_constraint("users_email_key", "users", type_="unique")
        op.create_index("uq_users_email_active", "users", ["email"], **kwargs)


def downgrade() -> None:
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT email FROM users GROUP BY email HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade users email uniqueness while duplicate emails exist"
        )
    if op.get_bind().dialect.name == "postgresql":
        op.create_unique_constraint("users_email_key", "users", ["email"])
        with op.get_context().autocommit_block():
            op.drop_index(
                "uq_users_email_active",
                table_name="users",
                postgresql_concurrently=True,
            )
    else:
        op.create_unique_constraint("users_email_key", "users", ["email"])
        op.drop_index("uq_users_email_active", table_name="users")
