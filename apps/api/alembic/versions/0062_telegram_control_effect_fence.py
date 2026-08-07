"""Add fenced Telegram control effect receipts.

Revision ID: 0062_tg_control_effect_fence
Revises: 0061_video_jsonb_types
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision: str = "0062_tg_control_effect_fence"
down_revision: str | None = "0061_video_jsonb_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _export_before_drop(bind: sa.Connection) -> None:
    target_raw = os.environ.get("LUMEN_MIGRATION_EXPORT_PATH", "").strip()
    if not target_raw:
        raise RuntimeError(
            "destructive Telegram effect downgrade requires "
            "LUMEN_MIGRATION_EXPORT_PATH"
        )
    target = Path(target_raw)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = bind.execute(
        sa.text("SELECT * FROM telegram_control_commands")
    ).mappings().all()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {"telegram_control_commands": [dict(row) for row in rows]},
            default=str,
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n",
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
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_owner", sa.String(96), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_fence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column(
            "effect_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telegram_control_commands",
        sa.Column("effect_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_tg_control_effect_status",
        "telegram_control_commands",
        "effect_status IN ('pending','running','succeeded','failed')",
    )
    op.create_index(
        "ix_tg_control_effect_due",
        "telegram_control_commands",
        ["effect_status", "effect_lease_until"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE effect_status IN ('pending','running')"
        )
    ).scalar_one()
    if active:
        raise RuntimeError("cannot downgrade with active Telegram control effects")
    _export_before_drop(bind)
    op.drop_index(
        "ix_tg_control_effect_due",
        table_name="telegram_control_commands",
    )
    op.drop_constraint(
        "ck_tg_control_effect_status",
        "telegram_control_commands",
        type_="check",
    )
    for column in (
        "effect_error",
        "effect_completed_at",
        "effect_attempts",
        "effect_fence",
        "effect_lease_until",
        "effect_owner",
        "effect_status",
    ):
        op.drop_column("telegram_control_commands", column)
