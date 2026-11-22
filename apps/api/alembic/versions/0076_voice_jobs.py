"""Private Aliyun voice profiles and durable speech segment revisions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0076_voice_jobs"
down_revision = "0075_canvas_sidebar_idx_vacuum"
branch_labels = None
depends_on = None


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _lease_columns():
    return (
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
    )


def _base_columns(states: str, constraint: str):
    return (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("last_error_code", sa.String(80)),
        sa.CheckConstraint(f"state IN ({states})", name=constraint),
        *_timestamps(),
    )


def upgrade():
    op.create_table(
        "voice_profiles",
        *_base_columns("'queued','enrolling','polling','unknown','ready','failed','revoking','revoked'", "ck_voice_profile_state"),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("source_key", sa.String(200)),
        sa.Column("consent_kind", sa.String(24), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("provider_prefix", sa.String(10), nullable=False),
        sa.Column("provider_voice_id", sa.String(200)),
        sa.Column("saved_at", sa.DateTime(timezone=True)),
        sa.Column("draft_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enrollment_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_lease_columns(),
        sa.UniqueConstraint("user_id", "id", name="uq_voice_profile_owner_id"),
        sa.UniqueConstraint("user_id", "client_key", name="uq_voice_profile_client"),
        sa.UniqueConstraint("provider_prefix", name="uq_voice_provider_prefix"),
    )
    op.create_index("ix_voice_profile_owner_created", "voice_profiles", ["user_id", "created_at"])
    op.create_index("ix_voice_profile_due", "voice_profiles", ["state", "due_at"])
    op.create_index("ix_voice_profile_draft_expiry", "voice_profiles", ["draft_expires_at", "saved_at"])
    op.create_table(
        "speech_jobs",
        *_base_columns("'queued','running','unknown','ready','failed'", "ck_speech_job_state"),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("voice_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("client_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("artifact_key", sa.String(200)),
        sa.Column("estimated_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_chars", sa.Integer(), nullable=False, server_default="0"),
        *_lease_columns(),
        sa.ForeignKeyConstraint(
            ["user_id", "voice_id"], ["voice_profiles.user_id", "voice_profiles.id"],
            ondelete="CASCADE", name="fk_speech_jobs_owned_voice",
        ),
        sa.UniqueConstraint("user_id", "client_key", name="uq_speech_job_client"),
        sa.CheckConstraint("kind IN ('preview','speech')", name="ck_speech_job_kind"),
    )
    op.create_index("ix_speech_job_owner_created", "speech_jobs", ["user_id", "created_at"])
    op.create_index("ix_speech_job_due", "speech_jobs", ["state", "due_at"])
    op.create_table(
        "speech_segments",
        *_base_columns("'queued','sending','download','ready','failed','unknown'", "ck_speech_segment_state"),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("speech_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("segment_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("artifact_key", sa.String(200)),
        sa.Column("usage_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("result_url", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("job_id", "segment_no", "revision", name="uq_speech_segment_revision"),
        sa.CheckConstraint("segment_no >= 0 AND revision >= 1", name="ck_speech_segment_ordinal"),
    )


def downgrade():
    # Schema rollback is not a provider cleanup or retention mechanism.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM voice_profiles LIMIT 1")).scalar() is not None:
        raise RuntimeError("refusing to drop populated voice tables; preserve data and roll back application only")
    for name in ("speech_segments", "speech_jobs", "voice_profiles"):
        op.drop_table(name)
