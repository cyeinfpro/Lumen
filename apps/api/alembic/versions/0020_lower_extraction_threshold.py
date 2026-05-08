"""Lower default extraction_threshold from 0.85 to 0.80.

让 confidence ≥ 0.80 的自动抽取候选直接入主表(原本 < 0.85 全进 staging
等用户接受). 用户反馈 staging 太啰嗦, 内测期希望"自动学得多一点", 大不了
事后 forget — forget 自带 +0.02 的自适应反馈, 阈值会自然往上爬到合理位置.

Revision ID: 0020_lower_extraction_threshold
Revises: 0019_embedding_to_text
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0020_lower_extraction_threshold"
down_revision: str | Sequence[str] | None = "0019_embedding_to_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 改默认值 (新建 user 用 0.80).
    op.alter_column(
        "users",
        "extraction_threshold",
        server_default=sa.text("0.80"),
        existing_type=sa.Float(),
        existing_nullable=False,
    )
    # 现存 user 仍是初始值 0.85 (内测期没人 forget 过) → 一起拉到 0.80.
    # 已经被 forget/pin 调整过的用户保持原值, 尊重 adaptive 反馈历史.
    op.execute(
        "UPDATE users SET extraction_threshold = 0.80 WHERE extraction_threshold = 0.85"
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "extraction_threshold",
        server_default=sa.text("0.85"),
        existing_type=sa.Float(),
        existing_nullable=False,
    )
    op.execute(
        "UPDATE users SET extraction_threshold = 0.85 WHERE extraction_threshold = 0.80"
    )
