"""Repair historical PostgreSQL concurrent-index state without rewriting history.

Revision ID: 0057_repair_concurrent_indexes
Revises: 0056_outbox_due_index
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import sys

from alembic import op

ALEMBIC_ROOT = Path(__file__).resolve().parents[1]
if str(ALEMBIC_ROOT) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_ROOT))

from concurrent_index_state import (  # noqa: E402
    PostgresqlIndexSpec,
    PostgresqlUniqueConstraintSpec,
    drop_postgresql_unique_constraint,
    ensure_postgresql_index,
)


revision: str = "0057_repair_concurrent_indexes"
down_revision: str | None = "0056_outbox_due_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    PostgresqlIndexSpec(
        name="uq_users_email_active",
        table_name="users",
        columns=("email",),
        predicate="deleted_at IS NULL",
        unique=True,
    ),
    PostgresqlIndexSpec(
        name="ix_generations_cancel_requested",
        table_name="generations",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
        ),
    ),
    PostgresqlIndexSpec(
        name="ix_completions_cancel_requested",
        table_name="completions",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'streaming')"
        ),
    ),
)
_LEGACY_EMAIL_CONSTRAINT = PostgresqlUniqueConstraintSpec(
    name="users_email_key",
    table_name="users",
    columns=("email",),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if op.get_context().as_sql:
        raise RuntimeError(
            "0057 concurrent-index repair requires an online PostgreSQL migration"
        )

    for index in _INDEXES:
        ensure_postgresql_index(op, index)
    drop_postgresql_unique_constraint(op, _LEGACY_EMAIL_CONSTRAINT)


def downgrade() -> None:
    # This corrective revision does not own the indexes introduced by history.
    pass
