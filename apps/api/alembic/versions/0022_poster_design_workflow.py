"""Poster design workflow: style library + masters + renders.

Adds four tables that back the poster design workflow:

- ``poster_style_items`` — saved/favorited/generated visual styles per user.
  Mirrors ``model_library_items`` (visual styles instead of human models).
- ``poster_style_hidden_presets`` — per-user hidden preset list.
- ``poster_masters`` — square master candidates produced for a workflow run.
- ``poster_renders`` — final renders at target aspect ratios.

Revision ID: 0022_poster_design_workflow
Revises: 0021_byok_user_api_credentials
Create Date: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0022_poster_design_workflow"
down_revision: str | None = "0021_byok_user_api_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------- poster_style_items ----------
    op.create_table(
        "poster_style_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "cover_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sample_image_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="user_favorites",
        ),
        sa.Column("mood", sa.String(length=128), nullable=True),
        sa.Column("prompt_template", sa.Text(), nullable=True),
        sa.Column(
            "palette",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "recommended_aspects",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "style_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("library_folder", sa.String(length=64), nullable=True),
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
        "ix_poster_style_items_user_category",
        "poster_style_items",
        ["user_id", "category"],
    )
    op.create_index(
        "ix_poster_style_items_user_source",
        "poster_style_items",
        ["user_id", "source"],
    )
    op.create_index(
        "ix_poster_style_items_user_created",
        "poster_style_items",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_poster_style_items_cover_image",
        "poster_style_items",
        ["cover_image_id"],
    )
    op.create_index(
        "ix_poster_style_items_style_tags",
        "poster_style_items",
        ["style_tags"],
        postgresql_using="gin",
        postgresql_ops={"style_tags": "jsonb_path_ops"},
    )

    # ---------- poster_style_hidden_presets ----------
    op.create_table(
        "poster_style_hidden_presets",
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
            name="pk_poster_style_hidden_presets",
        ),
    )

    # ---------- poster_masters ----------
    op.create_table(
        "poster_masters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "style_summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "task_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="draft"
        ),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "workflow_run_id", "candidate_index", name="uq_poster_masters_run_index"
        ),
    )
    op.create_index(
        "ix_poster_masters_run_status",
        "poster_masters",
        ["workflow_run_id", "status"],
    )

    # ---------- poster_renders ----------
    op.create_table(
        "poster_renders",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "master_id",
            sa.String(length=36),
            sa.ForeignKey("poster_masters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("size", sa.String(length=16), nullable=False),
        sa.Column(
            "image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "task_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="draft"
        ),
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
        sa.UniqueConstraint(
            "workflow_run_id", "aspect_ratio", name="uq_poster_renders_run_aspect"
        ),
    )
    op.create_index(
        "ix_poster_renders_run_status",
        "poster_renders",
        ["workflow_run_id", "status"],
    )
    op.create_index(
        "ix_poster_renders_master",
        "poster_renders",
        ["master_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_poster_renders_master", table_name="poster_renders")
    op.drop_index("ix_poster_renders_run_status", table_name="poster_renders")
    op.drop_table("poster_renders")

    op.drop_index("ix_poster_masters_run_status", table_name="poster_masters")
    op.drop_table("poster_masters")

    op.drop_table("poster_style_hidden_presets")

    op.drop_index(
        "ix_poster_style_items_style_tags",
        table_name="poster_style_items",
    )
    op.drop_index(
        "ix_poster_style_items_cover_image",
        table_name="poster_style_items",
    )
    op.drop_index(
        "ix_poster_style_items_user_created",
        table_name="poster_style_items",
    )
    op.drop_index(
        "ix_poster_style_items_user_source",
        table_name="poster_style_items",
    )
    op.drop_index(
        "ix_poster_style_items_user_category",
        table_name="poster_style_items",
    )
    op.drop_table("poster_style_items")
