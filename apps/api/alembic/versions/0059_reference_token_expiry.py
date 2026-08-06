"""Revoke legacy reference tokens without a trustworthy expiry.

Revision ID: 0059_reference_token_expiry
Revises: 0058_storage_apply_operations
Create Date: 2026-08-06
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "0059_reference_token_expiry"
down_revision: str | None = "0058_storage_apply_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 500
_REFERENCE_TOKEN_FIELDS = (
    (
        "images",
        "video_reference_access_token",
        "video_reference_access_token_expires_at",
    ),
    (
        "videos",
        "reference_access_token",
        "reference_access_token_expires_at",
    ),
)


def _metadata_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return dict(decoded) if isinstance(decoded, dict) else None


def _expiry_is_parseable(raw: Any) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        return False
    return True


def _revoke_unbounded_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> dict[str, Any] | None:
    token = metadata.get(token_key)
    expiry = metadata.get(expires_key)
    token_is_valid = isinstance(token, str) and bool(token)
    if token_is_valid and _expiry_is_parseable(expiry):
        return None
    if token_key not in metadata and expires_key not in metadata:
        return None
    sanitized = dict(metadata)
    sanitized.pop(token_key, None)
    sanitized.pop(expires_key, None)
    return sanitized


def _revoke_table_tokens(
    table_name: str,
    token_key: str,
    expires_key: str,
) -> None:
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("id", sa.String()),
        sa.column("metadata_jsonb", sa.JSON()),
    )
    result = bind.execute(
        sa.select(table.c.id, table.c.metadata_jsonb).where(
            table.c.metadata_jsonb.is_not(None)
        )
    )
    while rows := result.fetchmany(_BATCH_SIZE):
        for row_id, raw_metadata in rows:
            metadata = _metadata_dict(raw_metadata)
            if metadata is None:
                continue
            sanitized = _revoke_unbounded_token(
                metadata,
                token_key=token_key,
                expires_key=expires_key,
            )
            if sanitized is None:
                continue
            bind.execute(
                sa.update(table)
                .where(table.c.id == row_id)
                .values(metadata_jsonb=sanitized)
            )


def upgrade() -> None:
    for table_name, token_key, expires_key in _REFERENCE_TOKEN_FIELDS:
        _revoke_table_tokens(table_name, token_key, expires_key)


def downgrade() -> None:
    # Revoked bearer secrets cannot be reconstructed safely.
    pass
