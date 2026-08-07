"""Add durable Telegram delivery, control, and quarantine operations.

Revision ID: 0060_telegram_delivery_control
Revises: 0059_reference_token_expiry
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision: str = "0060_telegram_delivery_control"
down_revision: str | None = "0059_reference_token_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _export_before_drop(bind: sa.Connection) -> None:
    target_raw = os.environ.get("LUMEN_MIGRATION_EXPORT_PATH", "").strip()
    if not target_raw:
        raise RuntimeError(
            "destructive Telegram downgrade requires "
            "LUMEN_MIGRATION_EXPORT_PATH"
        )
    target = Path(target_raw)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, object] = {}
    for table in (
        "telegram_control_commands",
        "telegram_delivery_attempts",
        "telegram_delivery_quarantines",
    ):
        rows = bind.execute(sa.text(f"SELECT * FROM {table}")).mappings().all()
        payload[table] = [dict(row) for row in rows]
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, default=str, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def upgrade() -> None:
    op.create_table(
        "telegram_delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "generation_id",
            sa.String(36),
            sa.ForeignKey("generations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_id",
            sa.String(36),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_token_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column(
            "dispatch_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "generation_id",
            "image_id",
            name="uq_tg_delivery_generation_image",
        ),
        sa.CheckConstraint(
            "state IN ('dispatching','delivered','failed_before_accept',"
            "'delivery_result_unknown')",
            name="ck_tg_delivery_attempt_state",
        ),
    )
    op.create_index(
        "ix_tg_delivery_attempt_state_updated",
        "telegram_delivery_attempts",
        ["state", "updated_at"],
    )

    op.create_table(
        "telegram_control_commands",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("command", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "requested_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stream_id", sa.String(64), nullable=True),
        sa.Column(
            "active_slot",
            sa.Integer(),
            nullable=True,
            server_default="1",
        ),
        sa.Column("publish_owner", sa.String(96), nullable=True),
        sa.Column("publish_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_fence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending','published','accepted','failed')",
            name="ck_tg_control_command_status",
        ),
        sa.CheckConstraint(
            "(status IN ('pending','published') AND active_slot = 1) OR "
            "(status IN ('accepted','failed') AND active_slot IS NULL)",
            name="ck_tg_control_command_active_slot",
        ),
        sa.UniqueConstraint(
            "target",
            "active_slot",
            name="uq_tg_control_target_active",
        ),
    )
    op.create_index(
        "ix_tg_control_dispatch_due",
        "telegram_control_commands",
        ["status", "publish_lease_until", "created_at"],
    )

    op.create_table(
        "telegram_delivery_quarantines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_stream", sa.String(255), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("stream_user_id", sa.String(64), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("generation_id", sa.String(36), nullable=True),
        sa.Column("payload_raw", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "redrive_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "redrive_command_id",
            sa.String(32),
            sa.ForeignKey("telegram_control_commands.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redis_stream_id", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
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
            "source_stream",
            "source_id",
            name="uq_tg_quarantine_source_entry",
        ),
        sa.CheckConstraint(
            "status IN ('pending','redrive_queued','resolved')",
            name="ck_tg_quarantine_status",
        ),
    )
    op.create_index(
        "ix_tg_quarantine_status_created",
        "telegram_delivery_quarantines",
        ["status", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    active_commands = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE status IN ('pending','published')"
        )
    ).scalar_one()
    active_deliveries = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_delivery_attempts "
            "WHERE state = 'dispatching'"
        )
    ).scalar_one()
    active_quarantines = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_delivery_quarantines "
            "WHERE status <> 'resolved'"
        )
    ).scalar_one()
    if active_commands or active_deliveries or active_quarantines:
        raise RuntimeError("cannot downgrade with active Telegram operations")
    _export_before_drop(bind)
    op.drop_index(
        "ix_tg_quarantine_status_created",
        table_name="telegram_delivery_quarantines",
    )
    op.drop_table("telegram_delivery_quarantines")
    op.drop_index(
        "ix_tg_control_dispatch_due",
        table_name="telegram_control_commands",
    )
    op.drop_table("telegram_control_commands")
    op.drop_index(
        "ix_tg_delivery_attempt_state_updated",
        table_name="telegram_delivery_attempts",
    )
    op.drop_table("telegram_delivery_attempts")
