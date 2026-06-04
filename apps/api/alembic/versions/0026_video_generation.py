"""Video generation schema, pricing seeds, and settings.

Revision ID: 0026_video_generation
Revises: 0025_users_active_email_unique
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0026_video_generation"
down_revision: str | None = "0025_users_active_email_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFAULT_VIDEO_HOLD_ESTIMATES = """
{
  "seedance-2.0": {
    "t2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000},
    "i2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000}
  },
  "seedance-2.0-fast": {
    "t2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000},
    "i2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000}
  }
}
""".strip()


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "video_generations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("provider_kind", sa.String(length=32), nullable=True),
        sa.Column("provider_task_id", sa.String(length=128), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "input_image_id",
            sa.String(length=36),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("input_image_storage_key", sa.Text(), nullable=True),
        sa.Column("input_image_sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=True),
        sa.Column(
            "generate_audio",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column(
            "watermark", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("upstream_request", _json_type(), nullable=True),
        sa.Column("upstream_response", _json_type(), nullable=True),
        sa.Column(
            "diagnostics", _json_type(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="queued"
        ),
        sa.Column(
            "progress_stage",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("est_token_upper", sa.BigInteger(), nullable=False),
        sa.Column("est_cost_micro", sa.BigInteger(), nullable=False),
        sa.Column("billed_tokens", sa.BigInteger(), nullable=True),
        sa.Column("billed_cost_micro", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
            "user_id", "idempotency_key", name="uq_video_gen_user_idemp"
        ),
        sa.CheckConstraint("duration_s > 0", name="ck_video_gen_duration_positive"),
        sa.CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_video_gen_progress_pct",
        ),
        sa.CheckConstraint(
            "est_cost_micro >= 0", name="ck_video_gen_est_cost_nonnegative"
        ),
        sa.CheckConstraint(
            "est_token_upper >= 0",
            name="ck_video_gen_est_tokens_nonnegative",
        ),
    )
    op.create_index(
        "ix_video_gen_user_status_created",
        "video_generations",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_video_gen_status_next_poll",
        "video_generations",
        ["status", "next_poll_at"],
    )
    op.create_index(
        "uq_video_gen_provider_task",
        "video_generations",
        ["provider_kind", "provider_name", "provider_task_id"],
        unique=True,
        postgresql_where=sa.text("provider_task_id IS NOT NULL"),
        sqlite_where=sa.text("provider_task_id IS NOT NULL"),
    )

    op.create_table(
        "videos",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_generation_id",
            sa.String(length=36),
            sa.ForeignKey("video_generations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("poster_storage_key", sa.Text(), nullable=True),
        sa.Column(
            "mime", sa.String(length=64), nullable=False, server_default="video/mp4"
        ),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("etag", sa.String(length=96), nullable=False),
        sa.Column(
            "has_audio", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "faststart", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "visibility", sa.String(length=16), nullable=False, server_default="private"
        ),
        sa.Column(
            "metadata_jsonb",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("storage_key", name="uq_videos_storage_key"),
        sa.UniqueConstraint("poster_storage_key", name="uq_videos_poster_storage_key"),
    )
    op.create_index(
        "ix_videos_user_alive_created",
        "videos",
        ["user_id", "deleted_at", "created_at"],
    )

    op.execute(
        """
        INSERT INTO pricing_rules
          (id, scope, key, variant, unit, price_micro, enabled, note)
        VALUES
          ('00000000-0000-7000-8000-000000000026', 'video', 'seedance-2.0', 't2v', 'per_mtoken', 20000000, true, '默认视频价格；需按火山最新价格复核'),
          ('00000000-0000-7000-8000-000000000027', 'video', 'seedance-2.0', 'i2v', 'per_mtoken', 20000000, true, '默认视频价格；需按火山最新价格复核'),
          ('00000000-0000-7000-8000-000000000028', 'video', 'seedance-2.0-fast', 't2v', 'per_mtoken', 15000000, true, '默认视频价格；需按火山最新价格复核'),
          ('00000000-0000-7000-8000-000000000029', 'video', 'seedance-2.0-fast', 'i2v', 'per_mtoken', 15000000, true, '默认视频价格；需按火山最新价格复核')
        ON CONFLICT (scope, key, variant, unit) DO NOTHING
        """
    )
    op.get_bind().execute(
        sa.text(
            """
        INSERT INTO system_settings (id, key, value)
        VALUES
          ('00000000-0000-7000-8000-00000000002a', 'video.enabled', '0'),
          ('00000000-0000-7000-8000-00000000002b', 'video.token_hold_estimates', :estimates)
        ON CONFLICT (key) DO NOTHING
        """
        ),
        {"estimates": _DEFAULT_VIDEO_HOLD_ESTIMATES},
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM system_settings WHERE key IN ('video.enabled', 'video.token_hold_estimates')"
    )
    op.execute("DELETE FROM pricing_rules WHERE scope = 'video'")
    op.drop_index("ix_videos_user_alive_created", table_name="videos")
    op.drop_table("videos")
    op.drop_index("uq_video_gen_provider_task", table_name="video_generations")
    op.drop_index("ix_video_gen_status_next_poll", table_name="video_generations")
    op.drop_index("ix_video_gen_user_status_created", table_name="video_generations")
    op.drop_table("video_generations")
