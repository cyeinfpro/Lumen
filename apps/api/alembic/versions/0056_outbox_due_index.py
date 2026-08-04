"""Add the outbox due-event index in its own retry boundary.

Revision ID: 0056_outbox_due_index
Revises: 0055_outbox_next_attempt
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import sys

import sqlalchemy as sa
from alembic import op

ALEMBIC_ROOT = Path(__file__).resolve().parents[1]
if str(ALEMBIC_ROOT) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_ROOT))

from concurrent_index_state import (  # noqa: E402
    PostgresqlIndexSpec,
    drop_postgresql_index,
    ensure_postgresql_index,
)


revision: str = "0056_outbox_due_index"
down_revision: str | None = "0055_outbox_next_attempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUE_INDEX = PostgresqlIndexSpec(
    name="ix_outbox_due",
    table_name="outbox_events",
    columns=("next_attempt_at", "created_at", "id"),
    predicate="published_at IS NULL",
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        ensure_postgresql_index(op, _DUE_INDEX)
    else:
        op.create_index(
            "ix_outbox_due",
            "outbox_events",
            ["next_attempt_at", "created_at", "id"],
            sqlite_where=sa.text("published_at IS NULL"),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        drop_postgresql_index(op, _DUE_INDEX)
    else:
        op.drop_index("ix_outbox_due", table_name="outbox_events")
