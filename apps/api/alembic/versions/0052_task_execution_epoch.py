"""Add durable execution epochs for generation and completion tasks.

Revision ID: 0052_task_execution_epoch
Revises: 0051_task_cancel_intent
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0052_task_execution_epoch"
down_revision: str | None = "0051_task_cancel_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    for table_name in ("generations", "completions"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "execution_epoch",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )

        constraint_name = f"ck_{table_name}_execution_epoch_nonnegative"
        if dialect_name == "postgresql":
            op.create_check_constraint(
                constraint_name,
                table_name,
                "execution_epoch >= 0",
                postgresql_not_valid=True,
            )
            op.execute(
                sa.text(
                    f'ALTER TABLE "{table_name}" '
                    f'VALIDATE CONSTRAINT "{constraint_name}"'
                )
            )
        else:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.create_check_constraint(
                    constraint_name,
                    "execution_epoch >= 0",
                    postgresql_not_valid=True,
                )


def downgrade() -> None:
    for table_name in ("completions", "generations"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                f"ck_{table_name}_execution_epoch_nonnegative",
                type_="check",
            )
            batch_op.drop_column("execution_epoch")
