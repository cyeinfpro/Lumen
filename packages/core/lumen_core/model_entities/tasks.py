"""Generation, completion, and video task persistence entities."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import cached_property
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..constants import DEFAULT_CHAT_MODEL
from ..model_base import Base, TimestampMixin, new_uuid7
from ..queue_metadata import completion_queue_metadata, generation_queue_metadata
from ..sqltypes import JsonType, StringListType
from .accounts import UserApiCredential

# ---------- Tasks (generation / completion) ----------


class Generation(Base, TimestampMixin):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_gen_user_idemp"),
        Index("ix_gen_user_status_created", "user_id", "status", "created_at"),
        Index("ix_generations_user_created", "user_id", "created_at"),
        Index(
            "ix_generations_user_message_created",
            "user_id",
            "message_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_gen_queued_created",
            "created_at",
            "id",
            postgresql_where=text("status = 'queued'"),
            sqlite_where=text("status = 'queued'"),
        ),
        Index(
            "ix_generations_active_updated",
            "status",
            "updated_at",
            "id",
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # generate/edit
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_CHAT_MODEL
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    size_requested: Mapped[str] = mapped_column(String(32), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    # PG ARRAY 字面量必须是 ARRAY[]::type 或 '{}' 文本字面量，不能是裸字符串 "{}"（被当作非法字符串字面量）。
    # requires alembic migration to alter server_default to ARRAY[]::varchar[]
    input_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    primary_input_image_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    # 局部 inpaint mask 引用（PostMessageIn.mask_image_id）。指向 images.id；
    # 不加 FK 约束，与 input_image_ids 保持一致：图片记录可能后续被软删，
    # 但 generation 行需要保留历史记录。worker 读到该字段后从存储拉 mask PNG。
    mask_image_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    user_api_credential_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    upstream_supplier_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billing_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    upstream_pixels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # review #23：方便 worker / route 通过 generation 反查 BYOK 凭证；
    # 没有 back_populates，单向即可（避免 UserApiCredential 上挂海量任务列表）。
    user_api_credential: Mapped["UserApiCredential | None"] = relationship(
        "UserApiCredential",
        foreign_keys="Generation.user_api_credential_id",
        lazy="raise",
    )

    @property
    def parent_generation_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("parent_generation_id")
        return value if isinstance(value, str) and value else None

    @property
    def diagnostics(self) -> dict[str, Any]:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        raw = request.get("generation_diagnostics")
        if isinstance(raw, dict):
            return raw
        out: dict[str, Any] = {}
        for key in (
            "revised_prompt",
            "requested_params",
            "effective_params",
            "provider_attempts",
            "provider",
            "actual_provider",
            "upstream_route",
            "actual_route",
            "actual_endpoint",
            "proxy_name",
            "proxy_enabled",
            "duration_ms",
            "upstream_duration_ms",
            "failover",
            "failover_count",
            "debug_id",
            "trace_id",
            "request_id",
            "safe_error_summary",
            "upstream_error_summary",
            "error_summary",
        ):
            if key in request:
                out[key] = request[key]
        return out

    @property
    def revised_prompt(self) -> str | None:
        value = self.diagnostics.get("revised_prompt")
        return value if isinstance(value, str) and value else None

    @property
    def requested_params(self) -> dict[str, Any] | None:
        value = self.diagnostics.get("requested_params")
        return value if isinstance(value, dict) else None

    @property
    def effective_params(self) -> dict[str, Any] | None:
        value = self.diagnostics.get("effective_params")
        return value if isinstance(value, dict) else None

    @property
    def provider_attempts(self) -> list[dict[str, Any]]:
        value = self.diagnostics.get("provider_attempts")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @property
    def source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source")
        return value if isinstance(value, str) and value else None

    @property
    def action_source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("action_source")
        return value if isinstance(value, str) and value else None

    @property
    def trace_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("trace_id") or self.diagnostics.get("trace_id")
        return value if isinstance(value, str) and value else None

    @property
    def attachment_roles(self) -> list[dict[str, Any]]:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("attachment_roles")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @property
    def source_image_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source_image_id") or request.get("primary_input_image_id")
        return (
            value if isinstance(value, str) and value else self.primary_input_image_id
        )

    @cached_property
    def queue_metadata(self) -> dict[str, Any]:
        return generation_queue_metadata(
            upstream_request=self.upstream_request,
            action=self.action,
            size_requested=self.size_requested,
            mask_image_id=self.mask_image_id,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            upstream_pixels=self.upstream_pixels,
            now=datetime.now(timezone.utc),
        )

    @property
    def queue_lane(self) -> str | None:
        value = self.queue_metadata.get("queue_lane")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_type(self) -> str | None:
        value = self.queue_metadata.get("workflow_type")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_step_key(self) -> str | None:
        value = self.queue_metadata.get("workflow_step_key")
        return value if isinstance(value, str) and value else None

    @property
    def pixel_count(self) -> int | None:
        value = self.queue_metadata.get("pixel_count")
        return value if isinstance(value, int) and value >= 0 else None

    @property
    def size_bucket(self) -> str | None:
        value = self.queue_metadata.get("size_bucket")
        return value if isinstance(value, str) and value else None

    @property
    def cost_class(self) -> str | None:
        value = self.queue_metadata.get("cost_class")
        return value if isinstance(value, str) and value else None

    @property
    def queue_wait_ms(self) -> int | None:
        value = self.queue_metadata.get("queue_wait_ms")
        return value if isinstance(value, int) and value >= 0 else None


class Completion(Base, TimestampMixin):
    __tablename__ = "completions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_comp_user_idemp"),
        Index("ix_completions_user_status_created", "user_id", "status", "created_at"),
        Index(
            "ix_completions_user_message_created",
            "user_id",
            "message_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_completions_active_updated",
            "status",
            "updated_at",
            "id",
            postgresql_where=text("status IN ('queued', 'streaming')"),
            sqlite_where=text("status IN ('queued', 'streaming')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_CHAT_MODEL
    )
    # 见 Generation.input_image_ids 注释。
    # requires alembic migration to alter server_default to ARRAY[]::varchar[]
    input_image_ids: Mapped[list[str]] = mapped_column(
        StringListType(36),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::varchar[]"),
    )
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    user_api_credential_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    upstream_supplier_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_5m_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_1h_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    image_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # review #23：与 Generation.user_api_credential 对称，方便从 completion 反查
    # BYOK 凭证；单向，避免 UserApiCredential 挂海量历史任务列表。
    user_api_credential: Mapped["UserApiCredential | None"] = relationship(
        "UserApiCredential",
        foreign_keys="Completion.user_api_credential_id",
        lazy="raise",
    )

    @property
    def source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("source")
        return value if isinstance(value, str) and value else None

    @property
    def action_source(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("action_source")
        return value if isinstance(value, str) and value else None

    @property
    def trace_id(self) -> str | None:
        request = (
            self.upstream_request if isinstance(self.upstream_request, dict) else {}
        )
        value = request.get("trace_id")
        return value if isinstance(value, str) and value else None

    @property
    def queue_metadata(self) -> dict[str, Any]:
        return completion_queue_metadata(
            upstream_request=self.upstream_request,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            now=datetime.now(timezone.utc),
        )

    @property
    def queue_lane(self) -> str | None:
        value = self.queue_metadata.get("queue_lane")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_type(self) -> str | None:
        value = self.queue_metadata.get("workflow_type")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_step_key(self) -> str | None:
        value = self.queue_metadata.get("workflow_step_key")
        return value if isinstance(value, str) and value else None

    @property
    def pixel_count(self) -> int | None:
        value = self.queue_metadata.get("pixel_count")
        return value if isinstance(value, int) else None

    @property
    def size_bucket(self) -> str | None:
        value = self.queue_metadata.get("size_bucket")
        return value if isinstance(value, str) and value else None

    @property
    def cost_class(self) -> str | None:
        value = self.queue_metadata.get("cost_class")
        return value if isinstance(value, str) and value else None

    @property
    def queue_wait_ms(self) -> int | None:
        value = self.queue_metadata.get("queue_wait_ms")
        return value if isinstance(value, int) else None


class VideoGeneration(Base, TimestampMixin):
    __tablename__ = "video_generations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_video_gen_user_idemp"),
        Index("ix_video_gen_user_status_created", "user_id", "status", "created_at"),
        Index("ix_video_gen_status_next_poll", "status", "next_poll_at"),
        Index(
            "uq_video_gen_provider_task",
            "provider_kind",
            "provider_name",
            "provider_task_id",
            unique=True,
            postgresql_where=text("provider_task_id IS NOT NULL"),
            sqlite_where=text("provider_task_id IS NOT NULL"),
        ),
        CheckConstraint(
            "duration_s = -1 OR (duration_s >= 3 AND duration_s <= 15)",
            name="ck_video_gen_duration_positive",
        ),
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_video_gen_progress_pct",
        ),
        CheckConstraint(
            "est_cost_micro >= 0",
            name="ck_video_gen_est_cost_nonnegative",
        ),
        CheckConstraint(
            "est_token_upper >= 0",
            name="ck_video_gen_est_tokens_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_image_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    input_image_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generate_audio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    watermark: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    upstream_request: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    upstream_response: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType(), nullable=True
    )
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JsonType(), nullable=False, default=dict, server_default="{}"
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    progress_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    submission_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    poll_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submit_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    provider_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    est_token_upper: Mapped[int] = mapped_column(BigInteger, nullable=False)
    est_cost_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billed_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_cost_micro: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "Generation",
    "Completion",
    "VideoGeneration",
]
