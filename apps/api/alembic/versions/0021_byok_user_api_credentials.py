"""Add BYOK supplier templates and user API credentials.

Revision ID: 0021_byok_user_api_credentials
Revises: 0020_lower_extraction_threshold
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0021_byok_user_api_credentials"
down_revision: str | None = "0020_lower_extraction_threshold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # status enum：用 PG 原生 enum 类型而不是 CHECK，便于跨表/查询统一约束
    # （review #14）。SQLite 测试退化为 VARCHAR + CHECK 由 SA dialect 处理。
    # 用原生 DO $$ ... END$$ pg_type IF NOT EXISTS 守卫；SA 的 Enum.create
    # checkfirst=True 在 alembic op.get_bind() 上下文中并非总是稳定生效，
    # 直接走 DO 块更可靠（CI smoke 多次重启后 enum 残留 → DuplicateObject）。
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DO $$\n"
            "BEGIN\n"
            "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_api_credential_status') THEN\n"
            "    CREATE TYPE user_api_credential_status AS ENUM ('active', 'invalid', 'replaced', 'revoked');\n"
            "  END IF;\n"
            "END$$;"
        )
    else:
        # SQLite / 其他 dialect 走 SA 原生路径（VARCHAR + CHECK）
        status_enum = sa.Enum(
            "active",
            "invalid",
            "replaced",
            "revoked",
            name="user_api_credential_status",
        )
        status_enum.create(bind, checkfirst=True)

    op.create_table(
        "api_supplier_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "public_signup_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "user_bind_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "purposes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "validation_model",
            sa.String(length=64),
            nullable=False,
            server_default="gpt-5.4",
        ),
        sa.Column(
            "default_chat_model",
            sa.String(length=64),
            nullable=False,
            server_default="gpt-5.4",
        ),
        # review #12：image 任务用 default_chat_model 会错配 chat-only 上游。
        # 显式独立字段，nullable=True 因为很多 supplier 不支持图片。admin 显式配置。
        sa.Column("default_image_model", sa.String(length=128), nullable=True),
        sa.Column("fast_chat_model", sa.String(length=64), nullable=True),
        sa.Column(
            "validation_timeout_ms",
            sa.Integer(),
            nullable=False,
            server_default="15000",
        ),
        sa.Column("proxy_name", sa.String(length=120), nullable=True),
        sa.Column(
            "text_concurrency_per_key",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
        sa.Column(
            "image_concurrency_per_key",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "capabilities_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # review #5：slug 软删后允许复用，普通 UniqueConstraint 会阻塞。
        # 改为 partial unique index：deleted_at IS NULL 时唯一。
        sa.CheckConstraint(
            "validation_timeout_ms >= 1000 AND validation_timeout_ms <= 120000",
            name="ck_api_supplier_templates_validation_timeout",
        ),
        sa.CheckConstraint(
            "text_concurrency_per_key >= 1 AND text_concurrency_per_key <= 100",
            name="ck_api_supplier_templates_text_concurrency",
        ),
        sa.CheckConstraint(
            "image_concurrency_per_key >= 1 AND image_concurrency_per_key <= 32",
            name="ck_api_supplier_templates_image_concurrency",
        ),
    )
    op.create_index(
        "ix_api_supplier_templates_enabled",
        "api_supplier_templates",
        ["enabled", "deleted_at"],
    )
    # review #5：partial unique，仅在未软删行上唯一；软删后允许复用 slug。
    op.create_index(
        "uq_api_supplier_templates_slug_active",
        "api_supplier_templates",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "user_api_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # review #29：supplier 真删极少发生（admin UI 只软删），保留 RESTRICT
        # 以阻止 admin 误把活跃绑定的 supplier 物理删掉；软删走 deleted_at。
        sa.Column(
            "supplier_id",
            sa.String(length=36),
            sa.ForeignKey("api_supplier_templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_hint", sa.String(length=64), nullable=False),
        sa.Column(
            "encryption_key_version",
            sa.String(length=32),
            nullable=False,
            server_default="v1",
        ),
        # review #14：用 PG enum 而不是 CHECK；跨语言/工具识别更一致。
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "invalid",
                "replaced",
                "revoked",
                name="user_api_credential_status",
                create_type=False,
            ),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("rate_limited_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "capabilities_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_api_credentials_user_status",
        "user_api_credentials",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_user_api_credentials_supplier",
        "user_api_credentials",
        ["supplier_id"],
    )
    # review #2：key_hash 用于上游 401 反查 / dedup，普通 BTREE 索引。
    op.create_index(
        "ix_user_api_credentials_key_hash",
        "user_api_credentials",
        ["key_hash"],
    )
    # 设计 §5.2 + review #5：每个 user 同时只能有 1 条 active 凭证；软删后允许新增。
    op.create_index(
        "uq_user_api_credentials_one_active",
        "user_api_credentials",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )

    op.create_table(
        "pending_api_key_verifications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        # review #29：pending 行 TTL 短（默认 15 min），supplier 真删时
        # CASCADE 也只清几条临时数据，无业务影响。但 admin 路径只软删，
        # 因此实际 RESTRICT 更安全 —— 保持 RESTRICT 与 user_api_credentials 对齐。
        sa.Column(
            "supplier_id",
            sa.String(length=36),
            sa.ForeignKey("api_supplier_templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_hint", sa.String(length=64), nullable=False),
        sa.Column(
            "challenge_jsonb",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("ua_hash", sa.String(length=64), nullable=True),
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
            "token_hash",
            name="uq_pending_api_key_verifications_token_hash",
        ),
    )
    op.create_index(
        "ix_pending_api_key_verifications_expires",
        "pending_api_key_verifications",
        ["expires_at"],
    )
    op.create_index(
        "ix_pending_api_key_verifications_supplier",
        "pending_api_key_verifications",
        ["supplier_id"],
    )
    # review #2：上游 401 时按 key_hash 反查 pending 行用于解释错误。
    op.create_index(
        "ix_pending_api_key_verifications_key_hash",
        "pending_api_key_verifications",
        ["key_hash"],
    )
    # review #2：TTL cleanup 任务按 (expires_at, consumed_at) 扫描；复合索引
    # 覆盖「未消费且已过期」常用谓词。
    op.create_index(
        "ix_pending_api_key_verifications_cleanup",
        "pending_api_key_verifications",
        ["expires_at", "consumed_at"],
    )

    # review #29：generations / completions 的 BYOK 引用列。语义说明：
    # - 软删走 deleted_at（admin / user 删凭证时 status='revoked' + deleted_at=now）；
    #   软删行依然存在，FK 引用保持有效，历史任务行不会被破坏。
    # - 真删几乎不发生（DBA 手动），但若发生：SET NULL 让任务行 credential 字段
    #   降级为 NULL（从「该任务用过的 credential」可解释为「凭证已物理移除」），
    #   不连带删除任务行（任务行有自己的统计/审计价值）。
    op.add_column(
        "completions",
        sa.Column(
            "user_api_credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "completions",
        sa.Column(
            "upstream_supplier_id",
            sa.String(length=36),
            sa.ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "generations",
        sa.Column(
            "user_api_credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_api_credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "generations",
        sa.Column(
            "upstream_supplier_id",
            sa.String(length=36),
            sa.ForeignKey("api_supplier_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_completions_user_api_credential",
        "completions",
        ["user_api_credential_id"],
    )
    op.create_index(
        "ix_generations_user_api_credential",
        "generations",
        ["user_api_credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_generations_user_api_credential", table_name="generations")
    op.drop_index("ix_completions_user_api_credential", table_name="completions")
    op.drop_column("generations", "upstream_supplier_id")
    op.drop_column("generations", "user_api_credential_id")
    op.drop_column("completions", "upstream_supplier_id")
    op.drop_column("completions", "user_api_credential_id")

    op.drop_index(
        "ix_pending_api_key_verifications_cleanup",
        table_name="pending_api_key_verifications",
    )
    op.drop_index(
        "ix_pending_api_key_verifications_key_hash",
        table_name="pending_api_key_verifications",
    )
    op.drop_index(
        "ix_pending_api_key_verifications_supplier",
        table_name="pending_api_key_verifications",
    )
    op.drop_index(
        "ix_pending_api_key_verifications_expires",
        table_name="pending_api_key_verifications",
    )
    op.drop_table("pending_api_key_verifications")

    op.drop_index(
        "uq_user_api_credentials_one_active",
        table_name="user_api_credentials",
    )
    op.drop_index(
        "ix_user_api_credentials_key_hash",
        table_name="user_api_credentials",
    )
    op.drop_index(
        "ix_user_api_credentials_supplier",
        table_name="user_api_credentials",
    )
    op.drop_index(
        "ix_user_api_credentials_user_status",
        table_name="user_api_credentials",
    )
    op.drop_table("user_api_credentials")

    op.drop_index(
        "uq_api_supplier_templates_slug_active",
        table_name="api_supplier_templates",
    )
    op.drop_index(
        "ix_api_supplier_templates_enabled",
        table_name="api_supplier_templates",
    )
    op.drop_table("api_supplier_templates")

    # 最后清掉 enum 类型（要在引用它的表全部 drop 后才能 drop）。
    sa.Enum(
        "active",
        "invalid",
        "replaced",
        "revoked",
        name="user_api_credential_status",
    ).drop(op.get_bind(), checkfirst=True)

