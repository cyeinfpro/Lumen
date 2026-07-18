"""Move memory extraction ownership state out of message content.

Revision ID: 0045_memory_extraction_runs
Revises: 0044_infinite_canvas
Create Date: 2026-07-18
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0045_memory_extraction_runs"
down_revision: str | None = "0044_infinite_canvas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backfill_legacy_runs(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "postgresql":
        legacy_rows = bind.execute(
            sa.text(
                """
                SELECT
                    m.id AS source_message_id,
                    m.conversation_id,
                    m.content,
                    m.updated_at,
                    c.user_id
                FROM messages AS m
                JOIN conversations AS c ON c.id = m.conversation_id
                WHERE m.role = 'user'
                  AND m.content ? '_memory_extraction'
                """
            )
        ).mappings()
    elif bind.dialect.name == "sqlite":
        legacy_rows = bind.execute(
            sa.text(
                """
                SELECT
                    m.id AS source_message_id,
                    m.conversation_id,
                    m.content,
                    m.updated_at,
                    c.user_id
                FROM messages AS m
                JOIN conversations AS c ON c.id = m.conversation_id
                WHERE m.role = 'user'
                  AND json_type(m.content, '$._memory_extraction') IS NOT NULL
                """
            )
        ).mappings()
    else:
        return

    run_table = sa.table(
        "memory_extraction_runs",
        sa.column("id", sa.String(length=36)),
        sa.column("event_id", sa.String(length=160)),
        sa.column("user_id", sa.String(length=36)),
        sa.column("conversation_id", sa.String(length=36)),
        sa.column("source_message_id", sa.String(length=36)),
        sa.column("assistant_message_id", sa.String(length=36)),
        sa.column("status", sa.String(length=24)),
        sa.column("owner", sa.String(length=255)),
        sa.column("job_id", sa.String(length=255)),
        sa.column("fence", sa.Integer()),
        sa.column("attempt", sa.Integer()),
        sa.column("recovery_count", sa.Integer()),
        sa.column("claimed_at", sa.DateTime(timezone=True)),
        sa.column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.column("committed_at", sa.DateTime(timezone=True)),
        sa.column("undo_expires_at", sa.DateTime(timezone=True)),
        sa.column("canceled_at", sa.DateTime(timezone=True)),
        sa.column("retry_reason", sa.Text()),
        sa.column("cancel_reason", sa.Text()),
        sa.column("memory_writes", _json_type()),
        sa.column("undo_operations", _json_type()),
        sa.column("undo_status", sa.String(length=16)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    seen_events: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for row in legacy_rows:
        content = _json_object(row["content"])
        state = content.get("_memory_extraction")
        if not isinstance(state, dict):
            continue
        assistant_message_id = state.get("assistant_message_id")
        if not isinstance(assistant_message_id, str) or not assistant_message_id:
            continue
        assistant_exists = bind.execute(
            sa.text(
                """
                SELECT id
                FROM messages
                WHERE id = :assistant_message_id
                  AND conversation_id = :conversation_id
                  AND role = 'assistant'
                """
            ),
            {
                "assistant_message_id": assistant_message_id,
                "conversation_id": row["conversation_id"],
            },
        ).scalar_one_or_none()
        if assistant_exists is None:
            continue
        source_message_id = str(row["source_message_id"])
        pair = (source_message_id, assistant_message_id)
        if pair in seen_pairs:
            continue
        fallback_event_id = f"memory-extract:{source_message_id}:{assistant_message_id}"
        raw_event_id = state.get("event_id")
        event_id = (
            raw_event_id
            if isinstance(raw_event_id, str) and raw_event_id
            else fallback_event_id
        )[:160]
        if event_id in seen_events:
            event_id = fallback_event_id
        if event_id in seen_events:
            continue

        legacy_status = state.get("status")
        status = "committed" if legacy_status == "committed" else "retryable"
        raw_writes = state.get("memory_writes")
        memory_writes = (
            [
                {key: value for key, value in item.items() if key != "undo_token"}
                for item in raw_writes
                if isinstance(item, dict)
            ]
            if status == "committed" and isinstance(raw_writes, list)
            else []
        )
        owner = state.get("owner")
        job_id = state.get("job_id")
        timestamp = _aware_datetime(row["updated_at"])
        bind.execute(
            run_table.insert().values(
                id=source_message_id,
                event_id=event_id,
                user_id=str(row["user_id"]),
                conversation_id=str(row["conversation_id"]),
                source_message_id=source_message_id,
                assistant_message_id=assistant_message_id,
                status=status,
                owner=owner[:255] if isinstance(owner, str) and owner else None,
                job_id=job_id[:255] if isinstance(job_id, str) and job_id else None,
                fence=_safe_nonnegative_int(state.get("fence")),
                attempt=max(1, _safe_nonnegative_int(state.get("attempt"), 1)),
                recovery_count=_safe_nonnegative_int(state.get("recovery_count")),
                claimed_at=None,
                lease_expires_at=None,
                committed_at=timestamp if status == "committed" else None,
                undo_expires_at=None,
                canceled_at=None,
                retry_reason=(
                    None if status == "committed" else "migrated_legacy_state"
                ),
                cancel_reason=None,
                memory_writes=memory_writes,
                undo_operations=[],
                undo_status="none",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        seen_pairs.add(pair)
        seen_events.add(event_id)


def upgrade() -> None:
    op.create_table(
        "memory_extraction_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            sa.String(length=36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("owner", sa.String(length=255), nullable=True),
        sa.Column("job_id", sa.String(length=255), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undo_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_reason", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column(
            "memory_writes",
            _json_type(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "undo_operations",
            _json_type(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "undo_status",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
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
            "event_id",
            name="uq_memory_extraction_runs_event_id",
        ),
        sa.UniqueConstraint(
            "source_message_id",
            "assistant_message_id",
            name="uq_memory_extraction_runs_source_assistant",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retryable', 'committed', 'canceled')",
            name="ck_memory_extraction_runs_status",
        ),
        sa.CheckConstraint(
            "undo_status IN ('none', 'pending', 'ready')",
            name="ck_memory_extraction_runs_undo_status",
        ),
        sa.CheckConstraint(
            "fence >= 0",
            name="ck_memory_extraction_runs_fence",
        ),
        sa.CheckConstraint(
            "attempt >= 0",
            name="ck_memory_extraction_runs_attempt",
        ),
        sa.CheckConstraint(
            "recovery_count >= 0",
            name="ck_memory_extraction_runs_recovery_count",
        ),
    )
    op.create_index(
        "ix_memory_extraction_runs_status_lease",
        "memory_extraction_runs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_memory_extraction_runs_user_status",
        "memory_extraction_runs",
        ["user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_memory_extraction_runs_conversation_status",
        "memory_extraction_runs",
        ["conversation_id", "status"],
    )
    op.create_index(
        "ix_memory_extraction_runs_undo_expiry",
        "memory_extraction_runs",
        ["status", "undo_expires_at"],
    )

    bind = op.get_bind()
    _backfill_legacy_runs(bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE messages
                SET content = content - '_memory_extraction'
                WHERE content ? '_memory_extraction'
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                UPDATE messages
                SET content = json_remove(content, '$._memory_extraction')
                WHERE json_type(content, '$._memory_extraction') IS NOT NULL
                """
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_extraction_runs_undo_expiry",
        table_name="memory_extraction_runs",
    )
    op.drop_index(
        "ix_memory_extraction_runs_conversation_status",
        table_name="memory_extraction_runs",
    )
    op.drop_index(
        "ix_memory_extraction_runs_user_status",
        table_name="memory_extraction_runs",
    )
    op.drop_index(
        "ix_memory_extraction_runs_status_lease",
        table_name="memory_extraction_runs",
    )
    op.drop_table("memory_extraction_runs")
