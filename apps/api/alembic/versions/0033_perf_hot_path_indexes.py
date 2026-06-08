"""Add hot-path performance indexes.

Revision ID: 0033_perf_hot_path_indexes
Revises: 0032_happyhorse_defaults
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0033_perf_hot_path_indexes"
down_revision: str | None = "0032_happyhorse_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_index(
        "ix_messages_conv_alive_created_id",
        "messages",
        ["conversation_id", "deleted_at", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_generations_user_message_created",
        "generations",
        ["user_id", "message_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_completions_user_message_created",
        "completions",
        ["user_id", "message_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_gen_queued_created",
        "generations",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
        sqlite_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_generations_active_updated",
        "generations",
        ["status", "updated_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_completions_active_updated",
        "completions",
        ["status", "updated_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'streaming')"),
        sqlite_where=sa.text("status IN ('queued', 'streaming')"),
    )
    op.create_index(
        "ix_outbox_unpublished_created",
        "outbox_events",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
        sqlite_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        "ix_wallet_tx_user_ref_kind",
        "wallet_transactions",
        ["user_id", "ref_type", "ref_id", "kind", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_wallet_hold_created",
        "wallet_transactions",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("kind = 'hold'"),
        sqlite_where=sa.text("kind = 'hold'"),
    )
    op.create_index(
        "ix_audit_logs_billing_created",
        "audit_logs",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "event_type LIKE 'wallet.%' OR event_type LIKE 'redemption.%' OR event_type LIKE 'billing.%'"
        ),
        sqlite_where=sa.text(
            "event_type LIKE 'wallet.%' OR event_type LIKE 'redemption.%' OR event_type LIKE 'billing.%'"
        ),
    )
    if _is_postgres():
        op.create_index(
            "ix_shares_image_ids_gin",
            "shares",
            ["image_ids"],
            unique=False,
            postgresql_using="gin",
        )


def downgrade() -> None:
    if _is_postgres():
        op.drop_index("ix_shares_image_ids_gin", table_name="shares")
    op.drop_index("ix_audit_logs_billing_created", table_name="audit_logs")
    op.drop_index("ix_wallet_hold_created", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_user_ref_kind", table_name="wallet_transactions")
    op.drop_index("ix_outbox_unpublished_created", table_name="outbox_events")
    op.drop_index("ix_completions_active_updated", table_name="completions")
    op.drop_index("ix_generations_active_updated", table_name="generations")
    op.drop_index("ix_gen_queued_created", table_name="generations")
    op.drop_index("ix_completions_user_message_created", table_name="completions")
    op.drop_index("ix_generations_user_message_created", table_name="generations")
    op.drop_index("ix_messages_conv_alive_created_id", table_name="messages")
