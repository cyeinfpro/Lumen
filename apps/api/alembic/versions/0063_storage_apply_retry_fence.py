"""Bound storage apply retries and lease renewal.

Revision ID: 0063_storage_apply_retry_fence
Revises: 0062_tg_control_effect_fence
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision: str = "0063_storage_apply_retry_fence"
down_revision: str | None = "0062_tg_control_effect_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _export_before_drop(bind: sa.Connection) -> None:
    target_raw = os.environ.get("LUMEN_MIGRATION_EXPORT_PATH", "").strip()
    if not target_raw:
        raise RuntimeError(
            "destructive storage retry downgrade requires "
            "LUMEN_MIGRATION_EXPORT_PATH"
        )
    target = Path(target_raw)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = bind.execute(
        sa.text("SELECT * FROM storage_apply_operations")
    ).mappings().all()
    payload: dict[str, object] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"refusing to merge into invalid migration export: {target}"
            ) from exc
        if not isinstance(existing, dict):
            raise RuntimeError(
                f"refusing to merge into non-object migration export: {target}"
            )
        payload.update(existing)
    payload["storage_apply_operations"] = [dict(row) for row in rows]
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
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
        "storage_apply_operations",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storage_apply_operations",
        sa.Column("failure_class", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_storage_apply_next_attempt",
        "storage_apply_operations",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    active = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM storage_apply_operations "
            "WHERE status IN ('pending','dispatched') "
            "OR next_attempt_at IS NOT NULL"
        )
    ).scalar_one()
    if active:
        raise RuntimeError("cannot downgrade with active storage apply retries")
    _export_before_drop(op.get_bind())
    op.drop_index(
        "ix_storage_apply_next_attempt",
        table_name="storage_apply_operations",
    )
    op.drop_column("storage_apply_operations", "failure_class")
    op.drop_column("storage_apply_operations", "next_attempt_at")
