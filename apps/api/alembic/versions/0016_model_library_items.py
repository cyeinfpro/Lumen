"""Move user model library items from JSON files to PostgreSQL.

User-level apparel-model-library data was stored as per-user index.json on
disk (apparel-model-library/users/{user_id}/index.json). Concurrent writes
trampled each other (read whole file → mutate → write whole file, no lock),
which caused "favorited but not appended" issues when users tapped multiple
items in quick succession.

This migration creates two tables that own that state instead:

- ``model_library_items`` — one row per saved/favorited/generated user item.
- ``model_library_hidden_presets`` — per-user list of preset ids that were
  "deleted" (presets are global, so user-level deletion just hides them).

The global preset index (apparel-model-library/index.json) and the sync
state file are unchanged: presets are read-only globals synced from GitHub.

Revision ID: 0016_model_library_items
Revises: 0015_workflow_showcase
Create Date: 2026-05-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0016_model_library_items"
down_revision: str | None = "0015_workflow_showcase"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_library_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "age_segment",
            sa.String(length=32),
            nullable=False,
            server_default="user_favorites",
        ),
        sa.Column("gender", sa.String(length=40), nullable=True),
        sa.Column("appearance_direction", sa.String(length=80), nullable=True),
        sa.Column(
            "style_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("library_folder", sa.String(length=64), nullable=True),
        sa.Column("prompt_hint", sa.Text(), nullable=True),
        sa.Column("auto_tagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_tag_notes", sa.Text(), nullable=True),
        sa.Column(
            "metadata_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_model_library_items_user_age",
        "model_library_items",
        ["user_id", "age_segment"],
    )
    op.create_index(
        "ix_model_library_items_user_source",
        "model_library_items",
        ["user_id", "source"],
    )
    op.create_index(
        "ix_model_library_items_user_created",
        "model_library_items",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_model_library_items_image",
        "model_library_items",
        ["image_id"],
    )
    op.create_index(
        "ix_model_library_items_style_tags",
        "model_library_items",
        ["style_tags"],
        postgresql_using="gin",
    )

    op.create_table(
        "model_library_hidden_presets",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preset_id", sa.String(length=160), nullable=False),
        sa.Column(
            "hidden_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "preset_id",
            name="pk_model_library_hidden_presets",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_library_hidden_presets")
    op.drop_index(
        "ix_model_library_items_style_tags",
        table_name="model_library_items",
    )
    op.drop_index(
        "ix_model_library_items_image",
        table_name="model_library_items",
    )
    op.drop_index(
        "ix_model_library_items_user_created",
        table_name="model_library_items",
    )
    op.drop_index(
        "ix_model_library_items_user_source",
        table_name="model_library_items",
    )
    op.drop_index(
        "ix_model_library_items_user_age",
        table_name="model_library_items",
    )
    op.drop_table("model_library_items")
