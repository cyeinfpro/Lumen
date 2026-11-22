"""Private voice catalog and durable synthesis revisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .model_base import Base, TimestampMixin, new_uuid7


class VoiceProfile(TimestampMixin, Base):
    __tablename__ = "voice_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_voice_profile_owner_id"),
        UniqueConstraint("user_id", "client_key", name="uq_voice_profile_client"),
        UniqueConstraint("provider_prefix", name="uq_voice_provider_prefix"),
        CheckConstraint(
            "state IN ('queued','enrolling','polling','unknown','ready','failed','revoking','revoked')",
            name="ck_voice_profile_state",
        ),
        Index("ix_voice_profile_owner_created", "user_id", "created_at"),
        Index("ix_voice_profile_due", "state", "due_at"),
        Index("ix_voice_profile_draft_expiry", "draft_expires_at", "saved_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    source_key: Mapped[str | None] = mapped_column(String(200))
    consent_kind: Mapped[str] = mapped_column(String(24))
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    client_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(64))
    provider_prefix: Mapped[str] = mapped_column(String(10))
    provider_voice_id: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enrollment_sent: Mapped[bool] = mapped_column(default=False, server_default="false")


class SpeechJob(TimestampMixin, Base):
    __tablename__ = "speech_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "voice_id"], ["voice_profiles.user_id", "voice_profiles.id"],
            name="fk_speech_jobs_owned_voice", ondelete="CASCADE",
        ),
        UniqueConstraint("user_id", "client_key", name="uq_speech_job_client"),
        CheckConstraint("kind IN ('preview','speech')", name="ck_speech_job_kind"),
        CheckConstraint(
            "state IN ('queued','running','unknown','ready','failed')",
            name="ck_speech_job_state",
        ),
        Index("ix_speech_job_owner_created", "user_id", "created_at"),
        Index("ix_speech_job_due", "state", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(36))
    voice_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))
    client_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    artifact_key: Mapped[str | None] = mapped_column(String(200))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    estimated_chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usage_chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))


class SpeechSegment(TimestampMixin, Base):
    __tablename__ = "speech_segments"
    __table_args__ = (
        UniqueConstraint("job_id", "segment_no", "revision", name="uq_speech_segment_revision"),
        CheckConstraint(
            "state IN ('queued','sending','download','ready','failed','unknown')",
            name="ck_speech_segment_state",
        ),
        CheckConstraint("segment_no >= 0 AND revision >= 1", name="ck_speech_segment_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    job_id: Mapped[str] = mapped_column(ForeignKey("speech_jobs.id", ondelete="CASCADE"))
    segment_no: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    artifact_key: Mapped[str | None] = mapped_column(String(200))
    usage_chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    result_url: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
